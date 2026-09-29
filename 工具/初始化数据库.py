"""随行管家 · 数据库初始化（幂等）。

用法（在项目根目录执行）：
    python 工具/初始化数据库.py            # 建表 + 灌门店事实与房态
    python 工具/初始化数据库.py --schema   # 只建表

为什么做成脚本而不是让人手打 psql：PG 在本机不会开机自启，
每次重启环境后都要确认一次"表和种子数据还在不在"。
脚本化之后，这件事变成一条命令，而且可以重复执行。
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
DB_DIR = os.path.join(BACKEND, "database")
ENV_FILE = os.path.join(BACKEND, "agent", ".env")

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ENV_FILE, override=False)


def _run_sql(cur, path: str, label: str) -> None:
    with open(path, "r", encoding="utf-8") as handle:
        sql = handle.read()
    cur.execute(sql)
    print(f"  ✅ {label}：{os.path.basename(path)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="随行管家数据库初始化")
    parser.add_argument("--schema", action="store_true", help="只建表，不灌种子数据")
    args = parser.parse_args()

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        print("❌ 未配置 POSTGRES_DSN")
        return 1

    import psycopg

    try:
        conn = psycopg.connect(dsn, connect_timeout=6, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 连不上 PostgreSQL（{exc}）")
        print("   PostgreSQL 不会开机自启，先执行：工具/启动本地.ps1")
        return 1

    print("【初始化数据库】", dsn.split("@")[-1])
    with conn:
        with conn.cursor() as cur:
            _run_sql(cur, os.path.join(DB_DIR, "schema.sql"), "建表")
            if not args.schema:
                _run_sql(cur, os.path.join(DB_DIR, "seed_hotel_ops.sql"), "种子数据")

            print()
            print("【当前状态】")
            for table in (
                "store_facts",
                "pms_room_state",
                "guest_sessions",
                "stay_context",
                "work_orders",
                "work_order_events",
                "auth_attempts",
                "member_preferences",
                "knowledge_chunks",
                "eval_runs",
            ):
                cur.execute(f"select count(*) from {table}")
                print(f"  {table:20} {cur.fetchone()[0]:>4} 行")

            cur.execute("select store_id, fact_key, fact_value from store_facts where fact_key = 'breakfast_time' order by store_id")
            print()
            print("【门店差异样例：早餐时间】")
            for store_id, _, value in cur.fetchall():
                print(f"  {store_id}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
