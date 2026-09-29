"""端到端验收：从"扫码进房"走到"送水工单闭环"。

    python 工具/端到端测试.py            # 需要后端已在 :5000 运行

三个场景：
    A 已入住客人送物：问"送两瓶水" → 被要求验证 → 逐槽位报凭据 → 建单 + 承诺时限
    B 安全类报修：**未验证也先派单**（漏水/漏电这类，延误的代价高于误派）
    C 门店差异：同一句话在两家店得到不同答案

为什么必须做端到端而不是只测单元：单元测试证明"函数是对的"，
端到端才能证明**串起来之后客人真的能办成事**——中间任何一环断了（鉴权、
路由、工具注入、MCP 子进程、SSE 事件）都会在这里暴露。
"""

import argparse
import json
import os
import sys
import time

import httpx
import psycopg
from dotenv import load_dotenv

BASE = "http://127.0.0.1:5000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, "backend", "agent", ".env"), override=False)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

PREFIX = "e2e-"
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


def ask(session_id: str, query: str, store_id: str, user_id: str = "user_1001") -> dict:
    """发一轮对话，收集 SSE 事件。"""
    result = {"meta": {}, "content": "", "work_order": None, "elapsed_ms": None}
    started = time.perf_counter()
    with httpx.stream(
        "POST",
        f"{BASE}/api/chat",
        json={
            "query": query,
            "user_id": user_id,
            "session_id": session_id,
            "store_id": store_id,
        },
        timeout=300,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            if "meta" in payload:
                result["meta"] = payload["meta"]
            elif "content" in payload:
                result["content"] += payload["content"]
            elif "work_order" in payload:
                result["work_order"] = payload["work_order"]
            elif "done" in payload:
                result["elapsed_ms"] = payload.get("elapsed_ms")
    result["wall_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def new_session(store_id: str, room_no: str) -> str:
    resp = httpx.post(
        f"{BASE}/api/session",
        json={"store_id": store_id, "room_no": room_no, "session_id": f"{PREFIX}{int(time.time() * 1000)}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def db_query(sql: str, params: tuple = ()):
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        # DELETE/UPDATE 不产生结果集，直接 fetch 会报 "last operation didn't produce records"
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    db_query(
        "DELETE FROM work_order_events WHERE work_order_id IN "
        "(SELECT work_order_id FROM work_orders WHERE session_id LIKE %s)", (f"{PREFIX}%",))
    db_query("DELETE FROM work_orders WHERE session_id LIKE %s", (f"{PREFIX}%",))
    db_query("DELETE FROM auth_attempts WHERE session_id LIKE %s", (f"{PREFIX}%",))
    db_query(
        "DELETE FROM stay_context WHERE stay_id IN "
        "(SELECT stay_id FROM guest_sessions WHERE session_id LIKE %s)", (f"{PREFIX}%",))
    db_query("DELETE FROM guest_sessions WHERE session_id LIKE %s", (f"{PREFIX}%",))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留测试产生的工单（默认清理）")
    args = parser.parse_args()

    try:
        httpx.get(f"{BASE}/", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 后端未运行（{exc}）。先执行 工具/启动本地.ps1")
        return 1

    cleanup()
    store = "H_SH_001"

    print("【场景 A】已入住客人送物：验证 → 建单")
    session = new_session(store, "1208")
    detail = httpx.post(
        f"{BASE}/api/session",
        json={"store_id": store, "room_no": "1208", "session_id": session},
        timeout=30,
    ).json()
    check("扫码后判定为入住中", detail["stage"] == "IN", str(detail))
    check("扫码不等于已验证", detail["auth_level"] == 0, str(detail["auth_level"]))

    turn1 = ask(session, "能送两瓶矿泉水吗？", store)
    check("要求送物时被鉴权门拦下", turn1["meta"].get("auth_required") == 1, str(turn1["meta"]))
    check("引导问房间号", "房间号" in turn1["content"], turn1["content"][:60])

    turn2 = ask(session, "1208", store)
    check("第 2 槽位问姓氏", "姓氏" in turn2["content"], turn2["content"][:60])
    turn3 = ask(session, "我姓陈", store)
    check("第 3 槽位问离店日", "离店" in turn3["content"], turn3["content"][:60])

    turn4 = ask(session, "9月18日", store)
    check("凭据正确 → 身份确认", "已确认" in turn4["content"], turn4["content"][:60])
    print(f"     ── 回答正文：{turn4['content'][:220].replace(chr(10), ' ')}")
    # 判据不用"回答里有没有某个词"（模型的措辞会变），而用**因果证据**：
    # 原问题被继续处理了 → 必然产生工单，且工单号必须出现在给客人的回答里。
    check(
        "接着答完原来的问题（不用客人重说）",
        turn4["work_order"] is not None
        and turn4["work_order"]["work_order_id"] in turn4["content"],
        turn4["content"][:120],
    )
    check("返回了工单事件", turn4["work_order"] is not None, str(turn4["meta"]))
    if turn4["work_order"]:
        wo = turn4["work_order"]
        check("工单派给客房部", wo["assignee_dept"] == "HOUSEKEEPING", wo["assignee_dept"])
        check("SLA 20 分钟（来自类别映射）", wo["sla_minutes"] == 20, str(wo["sla_minutes"]))
        rows = db_query(
            "SELECT status, guest_verified, priority FROM work_orders WHERE work_order_id = %s",
            (wo["work_order_id"],),
        )
        check("落库且状态 ASSIGNED", rows and rows[0][0] == "ASSIGNED", str(rows))
        check("已通过身份验证", rows and rows[0][1] is True, str(rows))

    print("\n【场景 B】安全类报修：未验证也先派单")
    session_b = new_session(store, "1208")
    turn_b = ask(session_b, "卫生间地漏一直往外冒水，地上都是水", store)
    print(f"     ── 回答正文：{turn_b['content'][:220].replace(chr(10), ' ')}")
    check("安全类故障未被鉴权挡住", turn_b["work_order"] is not None, str(turn_b["meta"]))
    if turn_b["work_order"]:
        wo_b = turn_b["work_order"]
        check("识别为安全类工单", wo_b["category"] == "MAINTENANCE_SAFETY", wo_b["category"])
        check("优先级 P0", wo_b["priority"] == "P0", wo_b["priority"])
        check("SLA 10 分钟", wo_b["sla_minutes"] == 10, str(wo_b["sla_minutes"]))
        rows = db_query(
            "SELECT guest_verified FROM work_orders WHERE work_order_id = %s",
            (wo_b["work_order_id"],),
        )
        check("标记为未经身份验证（可追溯）", rows and rows[0][0] is False, str(rows))

    print("\n【场景 C】门店差异：同一句话两家店")
    s_sh = new_session("H_SH_001", "1208")
    s_sy = new_session("H_SY_001", "302")
    turn_sh = ask(s_sh, "早餐是几点到几点？", "H_SH_001")
    turn_sy = ask(s_sy, "早餐是几点到几点？", "H_SY_001")
    check("上海店答出 10:00 结束", "10:00" in turn_sh["content"], turn_sh["content"][:80])
    check("三亚店答出 11:00 结束", "11:00" in turn_sy["content"], turn_sy["content"][:80])
    check("两家店答案不同", turn_sh["content"] != turn_sy["content"])

    print("\n【场景 D】坐席台闭环：接单 → 完成 → 回执 → 看板")
    if turn4["work_order"]:
        wo_id = turn4["work_order"]["work_order_id"]
        base = f"{BASE}/api/staff/work-orders/{wo_id}"

        pool = httpx.get(f"{BASE}/api/staff/work-orders", params={"store_id": store, "status": "ASSIGNED"}, timeout=30).json()
        check("工单出现在坐席台工单池", any(i["work_order_id"] == wo_id for i in pool["items"]), str(pool["count"]))

        accepted = httpx.post(f"{base}/accept", json={"actor": "housekeeping-01", "assignee_name": "张阿姨"}, timeout=30).json()
        check("接单 → ACCEPTED", accepted.get("to_status") == "ACCEPTED", str(accepted))

        finished = httpx.post(f"{base}/done", json={"actor": "housekeeping-01", "note": "已送达"}, timeout=30).json()
        check("完成 → DONE", finished.get("to_status") == "DONE", str(finished))
        check("按时完成 on_time=True", finished.get("on_time") is True, str(finished.get("on_time")))

        httpx.post(f"{base}/notify", json={"actor": "system"}, timeout=30)
        detail = httpx.get(f"{base}", timeout=30).json()
        check("已给客人回执", detail.get("guest_notified") is True, str(detail.get("guest_notified")))
        check("事件链完整（创建→派单→接单→完成→回执）",
              [e["event_type"] for e in detail["events"]] ==
              ["CREATED", "ASSIGNED", "ACCEPTED", "DONE", "NOTIFIED"],
              str([e["event_type"] for e in detail["events"]]))

        conflict = httpx.post(f"{base}/accept", json={"actor": "x"}, timeout=30)
        check("非法流转返回 409（不是静默改状态）", conflict.status_code == 409, str(conflict.status_code))

        board = httpx.get(f"{BASE}/api/metrics", params={"store_id": store}, timeout=30).json()
        check("看板能看到工单口径", "work_orders" in board and board["work_orders"].get("工单总数", 0) >= 1,
              str(board.get("work_orders")))

    print("\n【耗时】")
    for label, turn in (("送物首轮(含鉴权拦截)", turn1), ("验证通过并建单", turn4),
                        ("安全报修建单", turn_b)):
        print(f"  {label}: 服务端 {turn['elapsed_ms']} ms / 端到端 {turn['wall_ms']} ms")

    if not args.keep:
        cleanup()
        print("\n（测试数据已清理）")

    print(f"\n结果：通过 {passed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
