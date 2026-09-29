"""政策与门店知识的检索工具（RAG）。

与参考项目版本的三个关键差异：

1. **检索带门店过滤**：过滤条件是 `store_id = 当前门店 OR store_id = ''（集团级）`。
   没有这一条，就会出现"把 A 店的规则答给 B 店客人"——问题 P2 的核心，
   而且它错得**很像对的**（答案流利、有条理，只有住在那家店的人知道错）。
2. **不依赖 langchain_milvus 包装类**：集合是我们自己建的 schema
   （content / embedding / store_id …），包装类默认字段名对不上；
   直接用 MilvusClient，字段与过滤都写明白。
3. **断网降级**：向量库不可用时退回**本地文本快照的关键词检索**（`knowledge_chunks` 表），
   并在结果里明确标注"降级"，让评测与用户都看得见（对应问题 P7）。
"""

from __future__ import annotations

import os
import re

import psycopg
from dotenv import load_dotenv
from langchain_core.tools import tool

from core.runtime import current_store_id

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

COLLECTION = os.getenv("MILVUS_POLICY_COLLECTION", "hotel_policy_docs")
EMBEDDING_MODEL = "text-embedding-v2"
TOP_K = 4

_client = None
_embedder = None


def _get_clients():
    """惰性初始化：向量库不可用时这里就抛错，由调用方走降级路径。"""
    global _client, _embedder
    if _client is None:
        from pymilvus import MilvusClient
        from langchain_community.embeddings import DashScopeEmbeddings

        _client = MilvusClient(uri=os.getenv("MILVUS_URI"), token=os.getenv("MILVUS_API_KEY"))
        _embedder = DashScopeEmbeddings(
            model=EMBEDDING_MODEL, dashscope_api_key=os.getenv("DASHSCOPE_API_KEY")
        )
    return _client, _embedder


def _store_filter(store_id: str) -> str:
    """集团级（store_id=''）+ 本门店的切片都算命中；其他门店一律排除。"""
    safe = (store_id or "").replace('"', "")
    return f'store_id == "" or store_id == "{safe}"'


def _vector_search(query: str, store_id: str) -> list[dict]:
    client, embedder = _get_clients()
    vector = embedder.embed_query(query)
    results = client.search(
        collection_name=COLLECTION,
        data=[vector],
        filter=_store_filter(store_id),
        limit=TOP_K,
        output_fields=["content", "source_file", "section", "store_id", "doc_id"],
    )
    hits = []
    for hit in (results[0] if results else []):
        entity = hit.get("entity", {})
        hits.append(
            {
                "content": entity.get("content", ""),
                "source": os.path.basename(entity.get("source_file", "") or ""),
                "section": entity.get("section", ""),
                "store_id": entity.get("store_id", ""),
                "score": round(float(hit.get("distance", 0.0)), 4),
                "mode": "vector",
            }
        )
    return hits


def _keyword_search(query: str, store_id: str, limit: int = TOP_K) -> list[dict]:
    """降级路径：本地快照上的关键词检索（不需要 embedding，也就不需要网络）。"""
    terms = [t for t in re.split(r"[\s，。？！、,?.!]+", query) if len(t) >= 2][:8]
    if not terms:
        return []

    conditions = " + ".join(["CASE WHEN content ILIKE %s THEN 1 ELSE 0 END"] * len(terms))
    likes = [f"%{term}%" for term in terms]
    sql = f"""
        SELECT content, source_file, section, store_id, ({conditions}) AS hits
          FROM knowledge_chunks
         WHERE (store_id IS NULL OR store_id = %s) AND ({conditions}) > 0
         ORDER BY hits DESC, updated_at DESC
         LIMIT %s
    """
    params = likes + [store_id or ""] + likes + [limit]
    try:
        with psycopg.connect(os.getenv("POSTGRES_DSN"), connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "content": row[0],
            "source": os.path.basename(row[1] or ""),
            "section": row[2],
            "store_id": row[3] or "",
            "score": row[4],
            "mode": "keyword",
        }
        for row in rows
    ]


@tool
def query_policy_docs(query: str) -> str:
    """检索**集团政策**与**本门店说明**（退改规则、发票规则、会员权益、入住说明、
    门店位置交通、设施与服务、本店特别说明等）。

    适用场景：规则、条款、流程、门店设施与服务细节等**文字说明类**问题。
    不适用：早餐时间、WiFi 密码、泳池开放时间这类**结构化门店事实**——那些请用
    `query_store_facts`（答案更准，而且改了会立即生效）。

    Args:
        query: 客人的问题原文（不需要改写；系统会自动限定在当前门店范围内检索）。
    """
    store_id = current_store_id()
    print(f"[vector_tool] 解析到的门店 = {store_id!r}", flush=True)
    hits: list[dict] = []
    degraded = ""
    try:
        hits = _vector_search(query, store_id)
    except Exception as exc:  # noqa: BLE001
        degraded = f"向量检索不可用（{type(exc).__name__}），已降级为关键词检索"
        print(f"[vector_tool] {degraded}")

    if not hits:
        keyword_hits = _keyword_search(query, store_id)
        if keyword_hits:
            hits = keyword_hits
            degraded = degraded or "向量检索无结果，已使用关键词检索"

    if not hits:
        return "没有检索到相关文档。请如实告知客人该问题在知识库中没有依据，不要凭印象作答。"

    lines = []
    if degraded:
        lines.append(f"⚠️ 检索降级提示：{degraded}")
    for index, hit in enumerate(hits, start=1):
        scope = "集团政策" if not hit["store_id"] else f"门店 {hit['store_id']}"
        lines.append(
            f"[{index}] 来源：{hit['source']}（{scope}｜{hit['section'] or '正文'}）\n{hit['content']}"
        )
    return "\n\n".join(lines)
