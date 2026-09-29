"""门店规则变更：改事实 + 让该门店的缓存失效。

    python 工具/门店变更.py --store H_SH_001 --key breakfast_time --value "06:30-10:30（每日）"

为什么需要一个"变更入口"而不是直接 UPDATE 数据库：
  门店改了规则，如果只改数据不清缓存，客人会继续拿到**旧答案**——
  而且因为缓存是"秒回"，客人会更相信那个错答案（03 变动 4 里点过的坑）。
  所以把"改数据 + 清缓存"做成一个原子操作，谁改都不会漏掉第二步。
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
for path in (os.path.join(BACKEND, "app"), os.path.join(BACKEND, "agent")):
    if path not in sys.path:
        sys.path.insert(0, path)

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="门店规则变更（改事实 + 清缓存）")
    parser.add_argument("--store", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--value", required=True)
    parser.add_argument("--operator", default="store_manager")
    parser.add_argument("--no-invalidate", action="store_true", help="只改数据，不清缓存（用于对照实验）")
    args = parser.parse_args()

    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO store_facts (store_id, fact_key, fact_value, updated_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (store_id, fact_key) DO UPDATE
                SET fact_value = EXCLUDED.fact_value,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now(),
                    active = TRUE
            """,
            (args.store, args.key, args.value, args.operator),
        )
    print(f"✅ 已更新门店事实：{args.store} / {args.key} = {args.value}")

    if args.no_invalidate:
        print("⚠️ 按参数要求跳过了缓存失效（这会让客人继续拿到旧答案——仅用于对照实验）")
        return 0

    import asyncio

    from infra.cache import semantic_cache

    async def invalidate() -> int:
        await semantic_cache.initialize()
        return semantic_cache.invalidate_store(args.store, reason=f"{args.key} 变更")

    removed = asyncio.run(invalidate())
    print(f"🧹 已清理该门店公共缓存 {removed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
