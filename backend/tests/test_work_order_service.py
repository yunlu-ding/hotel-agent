"""工单域自测（可直接运行，不依赖 pytest）。

    python backend/tests/test_work_order_service.py

覆盖 8 件事：
    1. 类别决定部门与 SLA（模型无法自己定 SLA）
    2. 安全类允许"先派单、后验证"
    3. 正常闭环 accept → done，且 on_time 计算正确
    4. 非法流转被明确拒绝
    5. 失败 → 重试
    6. 超时 → 自动升级 + 事件链
    7. 完成回执（DONE 但未回执 = 一类失败）
    8. 投诉双通道：既派单，又把会话标记为需人工关注

测试数据用 session_id 前缀 `selftest-` 标记，运行前后都会清理，
保证不污染阶段四的评测与看板。
"""

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BACKEND, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

from service import work_order_service as wos  # noqa: E402

PREFIX = "selftest-"
STORE = "H_SH_001"
passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        print(f"  ❌ {label} {detail}")


def _cleanup() -> None:
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM work_order_events WHERE work_order_id IN "
            "(SELECT work_order_id FROM work_orders WHERE session_id LIKE %s)",
            (f"{PREFIX}%",),
        )
        cur.execute("DELETE FROM work_orders WHERE session_id LIKE %s", (f"{PREFIX}%",))
        cur.execute("DELETE FROM guest_sessions WHERE session_id LIKE %s", (f"{PREFIX}%",))


def _make_session(session_id: str) -> None:
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO guest_sessions (session_id, store_id, room_no, stage, auth_level)
            VALUES (%s, %s, '1208', 'IN', 1)
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session_id, STORE),
        )


def _session_needs_human(session_id: str) -> bool:
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT needs_human FROM guest_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
        return bool(row and row[0])


def _force_overdue(work_order_id: str, minutes: int = 5) -> None:
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_orders SET due_at = now() - make_interval(mins => %s) WHERE work_order_id = %s",
            (minutes, work_order_id),
        )


def main() -> int:
    _cleanup()
    session = f"{PREFIX}{os.getpid()}"
    _make_session(session)

    print("【1】类别决定部门与 SLA（不接受调用方传参）")
    water = wos.create_work_order(
        store_id=STORE, session_id=session, room_no="1208",
        category="SERVICE", intent="送两瓶矿泉水", payload={"item": "矿泉水", "qty": 2},
    )
    check("送物 → 客房部", water["assignee_dept"] == "HOUSEKEEPING", water["assignee_dept"])
    check("送物 → SLA 20 分钟", water["sla_minutes"] == 20, str(water["sla_minutes"]))
    check("送物 → 优先级 P2", water["priority"] == "P2", water["priority"])
    check("送物 → 初始状态 ASSIGNED", water["status"] == "ASSIGNED", water["status"])

    print("\n【2】安全类报修：先派单、后验证")
    leak = wos.create_work_order(
        store_id=STORE, session_id=session, room_no="1208",
        category="MAINTENANCE_SAFETY", intent="卫生间漏水",
        payload={"fault": "卫生间地面持续渗水"}, guest_verified=False,
    )
    check("安全类 → 工程部", leak["assignee_dept"] == "ENGINEERING", leak["assignee_dept"])
    check("安全类 → SLA 10 分钟", leak["sla_minutes"] == 10, str(leak["sla_minutes"]))
    check("安全类 → 优先级 P0", leak["priority"] == "P0", leak["priority"])
    check("未验证也允许先派单（建单成功）", leak["status"] == "ASSIGNED", leak["status"])
    check("但事实记录为未经验证（允许 ≠ 已验证）", leak["guest_verified"] is False, str(leak["guest_verified"]))
    events = [e["event_type"] for e in wos.get_work_order(leak["work_order_id"])["events"]]
    check("已同步通知值班经理", "NOTIFIED" in events, str(events))

    print("\n【3】正常闭环：accept → done，on_time 正确")
    wos.transition(water["work_order_id"], "accept", actor="housekeeping-01", assignee_name="张阿姨")
    done = wos.transition(water["work_order_id"], "done", actor="housekeeping-01", note="已送达")
    check("完成状态 DONE", done["to_status"] == "DONE", done["to_status"])
    check("按时完成 on_time=True", done["on_time"] is True, str(done["on_time"]))

    print("\n【4】非法流转被拒绝")
    try:
        wos.transition(water["work_order_id"], "accept", actor="x")
        check("DONE 之后不能再 accept", False, "竟然成功了")
    except ValueError as exc:
        check("DONE 之后不能再 accept", "非法流转" in str(exc), str(exc))

    print("\n【5】失败 → 重试")
    failed_order = wos.create_work_order(
        store_id=STORE, session_id=session, room_no="1208",
        category="SERVICE", intent="多要一床被子", payload={"item": "被子", "qty": 1},
    )
    wos.transition(failed_order["work_order_id"], "accept", actor="housekeeping-02")
    fail = wos.transition(failed_order["work_order_id"], "fail", actor="housekeeping-02",
                          note="布草间缺货")
    check("失败状态 FAILED", fail["to_status"] == "FAILED", fail["to_status"])
    check("失败原因已记录",
          wos.get_work_order(failed_order["work_order_id"])["failed_reason"] == "布草间缺货")
    retry = wos.transition(failed_order["work_order_id"], "assign", actor="duty-manager")
    check("失败后可重新派单", retry["to_status"] == "ASSIGNED", retry["to_status"])

    print("\n【6】超时 → 自动升级 + 事件链")
    _force_overdue(leak["work_order_id"], minutes=7)
    escaped = wos.sweep_overdue(store_id=STORE)
    check("超时工单被扫出", any(e["work_order_id"] == leak["work_order_id"] for e in escaped),
          str([e["work_order_id"] for e in escaped]))
    detail = wos.get_work_order(leak["work_order_id"])
    check("状态升级为 ESCALATED", detail["status"] == "ESCALATED", detail["status"])
    check("升级事件落在事件链",
          any(e["event_type"] == "ESCALATED" for e in detail["events"]))

    print("\n【7】完成回执（DONE 但未回执算一类失败）")
    before = wos.failure_attribution(store_id=STORE)["已完成但未回执"]
    wos.notify_guest(water["work_order_id"])
    after = wos.failure_attribution(store_id=STORE)["已完成但未回执"]
    check("回执后计数下降", after < before, f"{before} -> {after}")

    print("\n【8】投诉双通道：派单 + 会话标记需人工关注")
    complaint = wos.create_work_order(
        store_id=STORE, session_id=session, room_no="1208",
        category="COMPLAINT", intent="隔壁房间太吵", payload={"detail": "凌晨仍很吵"},
    )
    check("投诉 → 值班经理", complaint["assignee_dept"] == "DUTY_MANAGER", complaint["assignee_dept"])
    check("会话被标记 needs_human", _session_needs_human(session))
    check("投诉也建了工单（不是只转人工）", complaint["status"] == "ASSIGNED", complaint["status"])

    print("\n【统计口径】")
    print(f"  失败归因：{wos.failure_attribution(store_id=STORE)}")
    print(f"  指标    ：{wos.metrics(store_id=STORE)}")

    _cleanup()
    print(f"\n结果：通过 {passed} 项，失败 {failed} 项（测试数据已清理）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
