"""把预置问答灌入语义缓存（按新 schema：带 store_id）。

    python 工具/灌入缓存.py --rebuild     # 删掉旧集合、按新 schema 重建并灌入
    python 工具/灌入缓存.py              # 只灌入

为什么要单独一个脚本：缓存集合的 schema 变了（多一个 store_id），
而 Milvus 不支持改 schema，只能重建；重建前先用
`工具/重建知识库.py --export-cache` 把旧条目备份成 JSON，
本脚本再把它灌回去（`store_id=""` 表示集团级，对所有门店可用）。

顺带说明这个缓存现在的定位（见 评测报告 §0.5 的观察）：
它**只服务预置的高频政策问答**，对话产生的答案仍然只进候选池、不自动回写。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
APP_DIR = os.path.join(BACKEND, "app")
for path in (APP_DIR, os.path.join(BACKEND, "agent")):
    if path not in sys.path:
        sys.path.insert(0, path)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

BACKUP = os.path.join(BACKEND, "mock_data", "酒店知识", "缓存条目备份.json")

# 门店级缓存样例：这类答案只对该门店的客人可见（用来验证门店隔离）
STORE_ENTRIES = [
    ("洗衣服务怎么算？", "外滩璟悦酒店提供当日洗衣：10:00 前送洗，当日 20:00 可取。", "H_SH_001"),
    ("洗衣服务怎么算？", "本店没有当日洗衣服务，隔日 18:00 可取。", "H_SY_001"),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="灌入语义缓存")
    parser.add_argument("--rebuild", action="store_true", help="先按新 schema 重建集合")
    args = parser.parse_args()

    from infra.cache import semantic_cache

    await semantic_cache.initialize()
    if not semantic_cache.available:
        print("❌ 缓存不可用（检查 MILVUS_URI / API Key 与网络）")
        return 1
    if args.rebuild:
        semantic_cache.recreate_collection()

    if os.path.exists(BACKUP):
        with open(BACKUP, encoding="utf-8") as handle:
            entries = json.load(handle)
        for entry in entries:
            await semantic_cache.set_cache(
                entry["question"], entry["answer"],
                user_id=None, scope="public", store_id="",
            )
        print(f"已灌入集团级预置问答 {len(entries)} 条")
    else:
        print(f"⚠️ 没有找到备份文件 {BACKUP}（跳过集团级条目）")

    for question, answer, store_id in STORE_ENTRIES:
        await semantic_cache.set_cache(question, answer, user_id=None, scope="public", store_id=store_id)
    print(f"已灌入门店级样例 {len(STORE_ENTRIES)} 条（用于验证门店隔离）")
    print(f"\n当前集合：{semantic_cache._client.describe_collection('qa_semantic_cache')['fields'][0]['name']} 起")
    print(f"门店缓存阈值/模式：{semantic_cache.mode} / {semantic_cache.threshold}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
