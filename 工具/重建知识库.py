"""重建知识库：把"平台级"知识升级为"集团 + 门店"两层，并落本地文本快照。

    python 工具/重建知识库.py --export     # 第一步：把旧集合里的语料导出到本地（语料目录已丢失）
    python 工具/重建知识库.py --ingest     # 第二步：按新 schema 入库 + 写本地快照

为什么要"导出"这一步：
  旧集合 `hotel_policy_docs` 里的 13 个切片，其来源目录
  `CloudAgent-酒店客服助手/数据/政策文档/` **已经不存在了**。
  语料只活在向量库里 = 不可维护、不可审查、换 embedding 模型就彻底丢。
  所以先把它们导出成真正的文件，再加门店知识，最后按新 schema 入库。

新 schema 的关键差异：多了 `store_id`（空字符串 = 集团级）。
检索时必须按 "store_id = 当前门店 OR store_id = ''" 过滤，
否则就会出现"把 A 店的规则答给 B 店客人"——这正是问题 P2 的核心。
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

CORPUS_DIR = os.path.join(BACKEND, "mock_data", "酒店知识")
LEGACY_COLLECTION = "hotel_policy_docs"
NEW_COLLECTION = os.getenv("MILVUS_POLICY_COLLECTION", "hotel_knowledge_v2")
EMBEDDING_DIM = 1536
EMBEDDING_MODEL = "text-embedding-v2"
POLICY_VERSION = os.getenv("POLICY_VERSION", "HOTEL-2026.1")
CHUNK_MAX_CHARS = 800

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def _client():
    from pymilvus import MilvusClient

    return MilvusClient(uri=os.getenv("MILVUS_URI"), token=os.getenv("MILVUS_API_KEY"))


def _embeddings():
    from langchain_community.embeddings import DashScopeEmbeddings

    return DashScopeEmbeddings(
        model=EMBEDDING_MODEL, dashscope_api_key=os.getenv("DASHSCOPE_API_KEY")
    )


# ---------------------------------------------------------------------------
# 第一步：导出旧语料
# ---------------------------------------------------------------------------
def export_legacy() -> int:
    client = _client()
    if not client.has_collection(LEGACY_COLLECTION):
        print(f"旧集合 {LEGACY_COLLECTION} 不存在，跳过导出")
        return 0

    rows = client.query(
        collection_name=LEGACY_COLLECTION, filter="", output_fields=["text", "source"], limit=1000
    )
    by_source: dict[str, list[str]] = {}
    for row in rows:
        path = row.get("source") or "未命名.md"
        by_source.setdefault(os.path.basename(path), []).append((row.get("text") or "").strip())

    target_dir = os.path.join(CORPUS_DIR, "集团")
    os.makedirs(target_dir, exist_ok=True)
    written = 0
    for filename, chunks in by_source.items():
        if not filename.endswith(".md"):
            filename += ".md"
        content = "\n\n".join(c for c in chunks if c)
        with open(os.path.join(target_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(f"# {os.path.splitext(filename)[0]}\n\n")
            handle.write(
                "> 本文件由 `工具/重建知识库.py --export` 从旧向量库导出。\n"
                "> 原始语料目录已丢失，导出后它才重新成为可维护的文件。\n\n"
            )
            handle.write(content + "\n")
        written += 1
        print(f"  导出 {filename}（{len(chunks)} 个切片 → {len(content)} 字）")
    print(f"\n共导出 {written} 份文件到 {target_dir}")
    return written


def export_cache() -> int:
    """把旧缓存条目导出成 JSON，供重建集合后重新灌入。

    为什么要导出：Zilliz 免费版**只有 5 个集合名额**（这个约束是踩出来的），
    而名额已经被占满——所以"给缓存加 store_id 字段"只能**同名重建**，
    不能新建一个 v2。重建前必须先把条目备份出来。
    """
    import json

    client = _client()
    legacy = "qa_semantic_cache"
    if not client.has_collection(legacy):
        print(f"缓存集合 {legacy} 不存在，跳过导出")
        return 0
    rows = client.query(
        collection_name=legacy, filter="",
        output_fields=["question", "answer", "scope", "user_id"], limit=1000,
    )
    target = os.path.join(CORPUS_DIR, "缓存条目备份.json")
    os.makedirs(CORPUS_DIR, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    print(f"已导出 {len(rows)} 条缓存到 {target}")
    return len(rows)


# ---------------------------------------------------------------------------
# 第二步：切分 + 入库
# ---------------------------------------------------------------------------
def _split_markdown(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[tuple[str, str]]:
    """按标题切分，超长再按段落切。返回 [(section, chunk_text)]。"""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title, current_lines = "正文", []
    for line in lines:
        if line.startswith("#"):
            if current_lines:
                sections.append((current_title, current_lines))
            current_title, current_lines = line.lstrip("#").strip(), []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines))

    chunks: list[tuple[str, str]] = []
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        if len(body) <= max_chars:
            chunks.append((title, body))
            continue
        buffer = ""
        for para in body.split("\n\n"):
            if len(buffer) + len(para) + 2 > max_chars and buffer:
                chunks.append((title, buffer.strip()))
                buffer = ""
            buffer += para + "\n\n"
        if buffer.strip():
            chunks.append((title, buffer.strip()))
    return chunks


def _collect_corpus() -> list[dict]:
    """扫描 mock_data/酒店知识/{集团,门店}/，返回待入库的切片。"""
    items: list[dict] = []
    for layer, store_id in (("集团", ""), ("门店", None)):
        layer_dir = os.path.join(CORPUS_DIR, layer)
        if not os.path.isdir(layer_dir):
            continue
        for filename in sorted(os.listdir(layer_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(layer_dir, filename)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            # 门店层：文件名即 store_id（H_SH_001.md → store_id=H_SH_001）
            file_store = store_id if store_id is not None else os.path.splitext(filename)[0]
            doc_id = f"{layer}-{os.path.splitext(filename)[0]}"
            for index, (section, chunk) in enumerate(_split_markdown(text)):
                items.append(
                    {
                        "chunk_id": f"{doc_id}#{index:03d}",
                        "doc_id": doc_id,
                        "store_id": file_store,
                        "source_file": path,
                        "section": section,
                        "content": chunk,
                    }
                )
    return items


def _ensure_collection(client) -> None:
    from pymilvus import DataType

    if client.has_collection(NEW_COLLECTION):
        return
    schema = client.create_schema()
    schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=128)
    schema.add_field("store_id", DataType.VARCHAR, max_length=32)      # "" = 集团级
    schema.add_field("doc_id", DataType.VARCHAR, max_length=128)
    schema.add_field("source_file", DataType.VARCHAR, max_length=512)
    schema.add_field("section", DataType.VARCHAR, max_length=256)
    schema.add_field("content", DataType.VARCHAR, max_length=8192)
    schema.add_field("policy_version", DataType.VARCHAR, max_length=64)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM)
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding", index_type="IVF_FLAT", metric_type="COSINE", params={"nlist": 128}
    )
    client.create_collection(
        collection_name=NEW_COLLECTION, schema=schema, index_params=index_params
    )
    print(f"已创建集合 {NEW_COLLECTION}（字段含 store_id）")


def ingest(rebuild: bool) -> int:
    items = _collect_corpus()
    if not items:
        print(f"❌ 语料目录为空：{CORPUS_DIR}")
        print("   先执行 python 工具/重建知识库.py --export 导出旧语料")
        return 1

    client = _client()
    if rebuild and client.has_collection(NEW_COLLECTION):
        client.drop_collection(NEW_COLLECTION)
        print(f"已删除旧集合 {NEW_COLLECTION}（--rebuild）")
    _ensure_collection(client)

    embedder = _embeddings()
    print(f"待入库切片：{len(items)}（集团 {sum(1 for i in items if not i['store_id'])}，"
          f"门店 {sum(1 for i in items if i['store_id'])}）")

    written = 0
    for start in range(0, len(items), 10):
        batch = items[start:start + 10]
        vectors = embedder.embed_documents([item["content"] for item in batch])
        rows = [
            {
                "chunk_id": item["chunk_id"],
                "store_id": item["store_id"],
                "doc_id": item["doc_id"],
                "source_file": item["source_file"],
                "section": item["section"],
                "content": item["content"],
                "policy_version": POLICY_VERSION,
                "embedding": vector,
            }
            for item, vector in zip(batch, vectors)
        ]
        client.insert(collection_name=NEW_COLLECTION, data=rows)
        written += len(rows)
        print(f"  已写入 {written}/{len(items)}")
        time.sleep(0.2)

    # 本地文本快照：断网时降级为关键词检索用（03 变动 4 的保险 ②）
    import psycopg

    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge_chunks")
        for item in items:
            cur.execute(
                """
                INSERT INTO knowledge_chunks
                    (chunk_id, store_id, doc_id, source_file, section, content, policy_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE
                    SET content = EXCLUDED.content, section = EXCLUDED.section,
                        store_id = EXCLUDED.store_id, updated_at = now()
                """,
                (
                    item["chunk_id"], item["store_id"] or None, item["doc_id"],
                    item["source_file"], item["section"], item["content"], POLICY_VERSION,
                ),
            )

    stats = client.get_collection_stats(collection_name=NEW_COLLECTION)
    print(f"\n集合 {NEW_COLLECTION} 现有 {stats.get('row_count')} 条；本地快照 {len(items)} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重建酒店知识库（集团 + 门店两层）")
    parser.add_argument("--export", action="store_true", help="从旧集合导出语料到本地")
    parser.add_argument("--export-cache", action="store_true", help="导出旧缓存条目（重建集合前必备份）")
    parser.add_argument("--ingest", action="store_true", help="把本地语料按新 schema 入库")
    parser.add_argument("--rebuild", action="store_true", help="入库前先删掉已有集合")
    args = parser.parse_args()

    if not args.export and not args.ingest and not args.export_cache:
        parser.print_help()
        return 1
    if args.export:
        print("【第一步】导出旧语料")
        export_legacy()
    if args.export_cache:
        print("\n【导出缓存】")
        export_cache()
    if args.ingest:
        print("\n【第二步】按新 schema 入库")
        return ingest(args.rebuild)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
