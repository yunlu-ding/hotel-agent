"""看一眼系统里现在有什么（工单 / 会话 / 门店事实 / 会员偏好）。

    python 工具/查看数据.py                 # 概览
    python 工具/查看数据.py --work-orders   # 工单明细（含 payload 与事件链）
    python 工具/查看数据.py --preferences   # 会员长期偏好

为什么要有它：演示或排障时被问最多的三句话是
「刚才那张单派给谁了」「这位客人住了哪间房」「系统记住了什么」。
直接在 psql 里敲这些查询既慢又容易看错列，所以固化成一个脚本。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
for path in (os.path.join(BACKEND, "agent"), os.path.join(BACKEND, "app")):
    if path not in sys.path:
        sys.path.insert(0, path)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def connect():
    import psycopg

    return psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True)


def overview() -> None:
    tables = (
        "hotels", "room_types", "orders", "users",
        "store_facts", "pms_room_state", "lost_found_items",
        "guest_sessions", "stay_context", "work_orders", "work_order_events",
        "auth_attempts", "member_preferences", "knowledge_chunks",
        "answer_log", "eval_runs",
    )
    with connect() as conn, conn.cursor() as cur:
        print("【数据概览】")
        for table in tables:
            cur.execute(f"select count(*) from {table}")
            print(f"  {table:20} {cur.fetchone()[0]:>6} 行")


def work_orders(limit: int) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT work_order_id, store_id, room_no, category, status, assignee_dept,
                   priority, sla_minutes, due_at, accepted_at, done_at, on_time,
                   guest_notified, guest_verified, intent, payload, created_at
              FROM work_orders ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        print(f"【工单明细】最近 {len(rows)} 张")
        for row in rows:
            item = dict(zip(cols, row))
            print(f"\n  {item['work_order_id']}  [{item['status']}] {item['category']} → {item['assignee_dept']}")
            print(f"    门店={item['store_id']} 房间={item['room_no']} 优先级={item['priority']} "
                  f"SLA={item['sla_minutes']}分钟 已验证={item['guest_verified']} 按时={item['on_time']} 已回执={item['guest_notified']}")
            print(f"    事由：{item['intent']}")
            payload = item["payload"] or {}
            if payload:
                print(f"    细节：{json.dumps(payload, ensure_ascii=False)[:260]}")
            cur.execute(
                "SELECT event_type, actor, note, created_at FROM work_order_events "
                "WHERE work_order_id = %s ORDER BY event_id",
                (item["work_order_id"],),
            )
            for event in cur.fetchall():
                print(f"      · {event[0]:<10} by {event[1] or '-':<12} {event[2] or ''}")


def preferences() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, pref_key, pref_value, source_session, updated_at "
            "FROM member_preferences ORDER BY user_id, updated_at DESC"
        )
        rows = cur.fetchall()
        print(f"【会员长期偏好】{len(rows)} 条（表：member_preferences，注意这里**不该**出现门店/房间/订单号）")
        for user_id, key, value, source, updated in rows:
            print(f"  {user_id} | {key}: {value} | 来源会话={source or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看系统数据")
    parser.add_argument("--work-orders", action="store_true", help="打印工单明细与事件链")
    parser.add_argument("--preferences", action="store_true", help="打印会员长期偏好")
    parser.add_argument("--cleanup-tests", action="store_true",
                        help="清理测试残留：门店为空的工单、dbg-/perf-/smoke- 前缀的会话与入住上下文")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.cleanup_tests:
        cleanup_tests()
        return 0
    if not (args.work_orders or args.preferences):
        overview()
        return 0
    if args.work_orders:
        work_orders(args.limit)
    if args.preferences:
        print()
        preferences()
    return 0


def cleanup_tests() -> None:
    """清理测试残留。

    为什么需要它：我用进程内脚本单独调图做调试时，没有带上门店上下文，
    结果库里留下了几张「门店=空」的工单——**脏数据看起来和正常工单一模一样**，
    这是最难发现的一类污染。建单工具现在已经会在缺上下文时拒绝，
    但历史数据要单独清。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM work_order_events WHERE work_order_id IN "
            "(SELECT work_order_id FROM work_orders WHERE store_id IS NULL OR store_id = '')"
        )
        cur.execute("DELETE FROM work_orders WHERE store_id IS NULL OR store_id = ''")
        print(f"  删除「门店为空」的工单：{cur.rowcount} 张")

        for prefix in ("dbg-%", "perf-%", "smoke-%"):
            cur.execute(
                "DELETE FROM work_order_events WHERE work_order_id IN "
                "(SELECT work_order_id FROM work_orders WHERE session_id LIKE %s)", (prefix,))
            cur.execute("DELETE FROM work_orders WHERE session_id LIKE %s", (prefix,))
            cur.execute(
                "DELETE FROM stay_context WHERE stay_id IN "
                "(SELECT stay_id FROM guest_sessions WHERE session_id LIKE %s)", (prefix,))
            cur.execute("DELETE FROM auth_attempts WHERE session_id LIKE %s", (prefix,))
            cur.execute("DELETE FROM guest_sessions WHERE session_id LIKE %s", (prefix,))
        print("  清理 dbg-/perf-/smoke- 会话及其上下文")

        cur.execute("DELETE FROM stay_context WHERE stay_id LIKE 'ST-DEBUG%'")
        cur.execute("SELECT count(*) FROM work_orders")
        orders = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM guest_sessions")
        sessions = cur.fetchone()[0]
        print(f"  清理后：工单 {orders} 张，会话 {sessions} 个")


if __name__ == "__main__":
    raise SystemExit(main())
