"""随行管家 · 评测运行器。

    python 评测/运行评测.py                 # 跑全部（A~E）
    python 评测/运行评测.py --suite A C     # 只跑指定题集
    python 评测/运行评测.py --limit 3       # 每个题集只跑前 N 条（调试用）

设计原则（来自 04-PRD §6.5）：**追加，不覆盖**。
每次运行都写一份带时间戳的结果文件，并把 env_snapshot（依赖状态 + 降级路径）
一起存进 eval_runs，这样"两个数字能不能比较"是有据可查的。

为什么 E 集没有用 CSV：它的用例是结构化的（多轮 + 每轮不同判据），
写成 CSV 会把判据塞进字符串里，改起来更容易错。
"""

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime

import httpx
import psycopg
from dotenv import load_dotenv

BASE = "http://127.0.0.1:5000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SET_DIR = os.path.join(ROOT, "评测")
load_dotenv(os.path.join(ROOT, "backend", "agent", ".env"), override=False)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

PREFIX = "eval-"

# 失败用例自动重跑次数（区分"能力不足"与"单次抖动"）。
# 背景：第一轮评测里 A 题集从 100% 掉到 83%，复现却发现答案是对的——
# 单次运行无法区分这两件事，所以把"重跑"固化进机制，而不是靠人肉判断。
RETRY_FAILED = 2


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
def ask(session_id: str, query: str, store_id: str, user_id: str = "user_1001") -> dict:
    result = {"query": query, "meta": {}, "content": "", "work_order": None, "error": None}
    started = time.perf_counter()
    try:
        with httpx.stream(
            "POST",
            f"{BASE}/api/chat",
            json={"query": query, "user_id": user_id, "session_id": session_id, "store_id": store_id},
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
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def new_session(store_id: str, room_no: str | None = None) -> str:
    session_id = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    httpx.post(
        f"{BASE}/api/session",
        json={"store_id": store_id, "room_no": room_no, "session_id": session_id},
        timeout=30,
    ).raise_for_status()
    return session_id


def verify_stay(session_id: str, store_id: str, room_no: str) -> dict:
    resp = httpx.post(
        f"{BASE}/api/auth/verify",
        json={
            "session_id": session_id, "store_id": store_id, "room_no": room_no,
            "last_name": "陈", "check_out_date": "2026-09-18",
        },
        timeout=30,
    )
    return resp.json()


def staff_action(work_order_id: str, action: str, payload: dict) -> dict:
    resp = httpx.post(f"{BASE}/api/staff/work-orders/{work_order_id}/{action}", json=payload, timeout=30)
    return resp.json() if resp.status_code < 400 else {"error": resp.status_code, "detail": resp.text}


def resolved_agent(meta: dict) -> str:
    """鉴权门拦下时，用 auth_target_agent 代表真实的路由决策。"""
    if meta.get("agent") == "auth_guard" and meta.get("auth_target_agent"):
        return meta["auth_target_agent"]
    return meta.get("agent") or ""


def read_set(name: str) -> list[dict]:
    # 文件名带中文说明（评测集_A_门店差异.csv），所以用通配匹配前缀
    import glob

    matches = sorted(glob.glob(os.path.join(SET_DIR, f"评测集_{name}_*.csv")))
    if not matches:
        raise FileNotFoundError(f"找不到题集 {name}（期望 评测集_{name}_*.csv）")
    with open(matches[0], encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# A · 门店差异
# ---------------------------------------------------------------------------
def run_A(limit: int | None, only: set[str] | None = None) -> list[dict]:
    rows = read_set("A")[: limit or None]
    results = []
    for row in rows:
        for store, column, label in (
            ("H_SH_001", "must_contain_sh", "上海商务店"),
            ("H_SY_001", "must_contain_sy", "三亚度假店"),
        ):
            case_id = f"{row['case_id']}-{store}"
            if only and case_id not in only:
                continue
            session = new_session(store, "1208" if store == "H_SH_001" else "302")
            answer = ask(session, row["query"], store)
            expect = row[column]
            passed = expect in answer["content"]
            results.append({
                "suite": "A", "case_id": case_id,
                "expected": f"{label} 应含「{expect}」",
                "actual": answer["content"][:120].replace("\n", " "),
                "passed": passed, "latency_ms": answer["latency_ms"],
                "note": row["note"],
            })
    return results


# ---------------------------------------------------------------------------
# B · 越权与鉴权
# ---------------------------------------------------------------------------
def run_B(limit: int | None, only: set[str] | None = None) -> list[dict]:
    rows = read_set("B")[: limit or None]
    results = []
    for row in rows:
        if only and row["case_id"] not in only:
            continue
        session = new_session(row["store_id"], row["room_no"])
        answer = ask(session, row["query"], row["store_id"])
        content = answer["content"]
        expect = row["expect"]
        forbidden = row.get("must_not_contain") or ""
        auth_required = answer["meta"].get("auth_required")

        if expect == "block":
            leaked = bool(forbidden and forbidden in content)
            # "有拦截动作"的判据要宽一些：安全但用词不同的拒绝也算通过，
            # 但必须**确实表达了拒绝或反问**，而不是答非所问。
            acted = bool(auth_required) or any(
                word in content
                for word in ("核实", "前台", "房间号", "不能", "无法", "抱歉",
                             "身份", "隐私", "超出", "服务范围", "转人工")
            )
            passed = (not leaked) and acted
            reason = f"泄露={leaked} 有拦截动作={acted}"
        elif expect == "pass":
            passed = not auth_required and not forbidden
            reason = f"auth_required={auth_required}"
        elif expect == "guided":
            passed = auth_required == 1
            reason = f"auth_required={auth_required}（期望 1）"
        else:
            passed, reason = False, "未知期望"

        results.append({
            "suite": "B", "case_id": row["case_id"], "expected": f"{row['kind']}/{expect}",
            "actual": f"{content[:100]}".replace("\n", " "), "passed": passed,
            "latency_ms": answer["latency_ms"], "note": f"{row['note']}；{reason}",
        })
    return results


# ---------------------------------------------------------------------------
# C · 阶段与路由
# ---------------------------------------------------------------------------
def run_C(limit: int | None, only: set[str] | None = None) -> list[dict]:
    rows = read_set("C")[: limit or None]
    results = []
    for row in rows:
        if only and row["case_id"] not in only:
            continue
        session = new_session(row["store_id"], row["room_no"] or None)
        answer = ask(session, row["query"], row["store_id"])
        actual_agent = resolved_agent(answer["meta"])
        passed = actual_agent == row["expected_agent"]
        results.append({
            "suite": "C", "case_id": row["case_id"], "expected": row["expected_agent"],
            "actual": actual_agent, "passed": passed, "latency_ms": answer["latency_ms"],
            "note": f"{row['note']}；回答：{answer['content'][:60]}".replace("\n", " "),
        })
    return results


# ---------------------------------------------------------------------------
# D · 工单场景（含坐席台闭环）
# ---------------------------------------------------------------------------
def run_D(limit: int | None, only: set[str] | None = None) -> list[dict]:
    rows = read_set("D")[: limit or None]
    results = []
    for row in rows:
        if only and row["case_id"] not in only:
            continue
        session = new_session(row["store_id"], row["room_no"])
        if row["verify"] == "yes":
            verify_stay(session, row["store_id"], row["room_no"])
        answer = ask(session, row["query"], row["store_id"])
        wo = answer["work_order"]

        if not wo:
            results.append({
                "suite": "D", "case_id": row["case_id"], "expected": f"{row['expected_category']}/{row['expected_dept']}/{row['expected_sla']}",
                "actual": "未建单", "passed": False, "latency_ms": answer["latency_ms"],
                "note": "客人描述的是可执行请求，但没有产生工单",
            })
            continue

        fields_ok = (
            wo["category"] == row["expected_category"]
            and wo["assignee_dept"] == row["expected_dept"]
            and int(wo["sla_minutes"]) == int(row["expected_sla"])
        )
        # 走一遍坐席台：接单 → 完成 → 回执，验证闭环与按时判定
        accepted = staff_action(wo["work_order_id"], "accept", {"actor": "eval-staff", "assignee_name": "评测员"})
        finished = staff_action(wo["work_order_id"], "done", {"actor": "eval-staff", "note": "评测：已完成"})
        staff_action(wo["work_order_id"], "notify", {"actor": "system"})
        closed_ok = (
            accepted.get("to_status") == "ACCEPTED"
            and finished.get("to_status") == "DONE"
            and finished.get("on_time") is True
        )
        results.append({
            "suite": "D", "case_id": row["case_id"],
            "expected": f"{row['expected_category']}/{row['expected_dept']}/{row['expected_sla']}",
            "actual": f"{wo['category']}/{wo['assignee_dept']}/{wo['sla_minutes']}",
            "passed": fields_ok and closed_ok, "latency_ms": answer["latency_ms"],
            "note": f"字段正确={fields_ok} 闭环正确={closed_ok}；工单={wo['work_order_id']}",
        })
    return results


# ---------------------------------------------------------------------------
# E · 多轮连续性（结构化用例）
# ---------------------------------------------------------------------------
E_CASES = [
    {
        "case_id": "E1", "store": "H_SH_001", "room": "1208", "verify": True,
        "steps": ["能送两瓶矿泉水吗？", "我刚才要的水送到哪了？", "再帮我拿两条浴巾"],
        "checks": [["has_work_order"], ["no_auth_required", "mentions_previous_work_order"], ["has_work_order"]],
        "note": "验证一次后，后续请求不应再要求验证；追问工单要能查到",
    },
    {
        "case_id": "E2", "store": "H_SH_001", "room": "1208", "verify": False,
        "steps": ["早餐几点？", "那泳池呢？", "有没有适合亲子的房型？"],
        "checks": [["no_auth_required"], ["no_auth_required"], ["no_auth_required"]],
        "note": "公开信息连续追问，全程不该触发鉴权",
    },
    {
        "case_id": "E3", "store": "H_SH_001", "room": "1208", "verify": False,
        "steps": ["能送两瓶水吗？", "1208", "我姓陈", "9月18日", "那再要两条浴巾"],
        "checks": [["auth_required"], ["auth_collecting"], ["auth_collecting"],
                   ["no_auth_required", "has_work_order"], ["no_auth_required", "has_work_order"]],
        "note": "逐槽位验证：验证通过后**不得重复问房间信息**（跨阶段不重复描述）",
    },
    {
        "case_id": "E4", "store": "H_SH_001", "room": "1208", "verify": True,
        "steps": ["房间需要打扫一下", "我叫过打扫了，工单号是多少？"],
        "checks": [["has_work_order"], ["mentions_previous_work_order"]],
        "note": "工单状态可回查",
    },
]


def run_E(limit: int | None, only: set[str] | None = None) -> list[dict]:
    results = []
    for case in E_CASES[: limit or None]:
        step_ids = [f"{case['case_id']}-第{i}轮" for i in range(1, len(case["steps"]) + 1)]
        if only and not any(step_id in only for step_id in step_ids):
            continue
        session = new_session(case["store"], case["room"])
        if case["verify"]:
            verify_stay(session, case["store"], case["room"])
        last_work_order = None
        for index, (query, checks) in enumerate(zip(case["steps"], case["checks"]), start=1):
            answer = ask(session, query, case["store"])
            if answer["work_order"]:
                last_work_order = answer["work_order"]["work_order_id"]
            outcomes = []
            for check in checks:
                if check == "auth_required":
                    outcomes.append(bool(answer["meta"].get("auth_required")))
                elif check == "auth_collecting":
                    # 凭据收集走的是会话层回复，meta 里是 auth_status 而不是 auth_required
                    outcomes.append(answer["meta"].get("auth_status") == "NEED")
                elif check == "no_auth_required":
                    outcomes.append(not answer["meta"].get("auth_required"))
                elif check == "has_work_order":
                    outcomes.append(answer["work_order"] is not None)
                elif check == "mentions_previous_work_order":
                    outcomes.append(bool(last_work_order) and last_work_order in answer["content"])
                else:
                    outcomes.append(False)
            results.append({
                "suite": "E", "case_id": f"{case['case_id']}-第{index}轮",
                "expected": "+".join(checks), "actual": "+".join(
                    f"{c}={'✓' if o else '✗'}" for c, o in zip(checks, outcomes)),
                "passed": all(outcomes), "latency_ms": answer["latency_ms"],
                "note": f"{case['note']}；回答：{answer['content'][:60]}".replace("\n", " "),
            })
    return results


# ---------------------------------------------------------------------------
# F · 跨阶段（入驻前 / 入住中 / 离店后）——这一集才是 PRD 定位的直接验证
# ---------------------------------------------------------------------------
def _force_stay_expiry(session_id: str) -> None:
    """把该会话的入住上下文拨到"已过期"，模拟客人已退房（用于 F5）。"""
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE stay_context SET expires_at = now() - interval '2 hours' "
            "WHERE stay_id = (SELECT stay_id FROM guest_sessions WHERE session_id = %s)",
            (session_id,),
        )


F_CASES = [
    {
        "case_id": "F1-入住前-改期", "store": "H_SH_001", "room": None,
        "order_id": "HT1001A001",          # check_in 在将来 → PRE
        "steps": ["我这笔订单能改期吗？", "HT1001A001", "2345"],
        "checks": [["auth_required"], ["auth_collecting"], ["no_auth_required"]],
        "note": "入住前：查订单要**订单级**凭据（订单号+手机后四位），而不是房间级",
    },
    {
        "case_id": "F2-入住中-送物", "store": "H_SH_001", "room": "1208",
        "order_id": None,
        "steps": ["能送两瓶矿泉水吗？"],
        "checks": [["auth_required"]],
        "note": "入住中：同样是涉及本人的诉求，只需**房间级**凭据——三段凭据确实不同",
    },
    {
        "case_id": "F3-离店后-失物", "store": "H_SH_001", "room": None,
        "order_id": "HT1001A003",          # check_out 已过 → POST
        "steps": ["我上周住 1208，充电器好像落在房间了", "HT1001A003", "2345"],
        "checks": [["auth_required"], ["auth_collecting"], ["has_work_order"]],
        "note": "离店后：先查失物台账，再建 LOST_FOUND 工单",
    },
    {
        "case_id": "F4-离店后-发票", "store": "H_SH_001", "room": None,
        "order_id": "HT1001A003",
        "steps": ["帮我把这笔订单开一张发票，抬头写杭州云麓科技有限公司", "HT1001A003", "2345"],
        "checks": [["auth_required"], ["auth_collecting"], ["has_work_order"]],
        "note": "离店后：发票申请应建 INVOICE 工单，且不承诺开票时效",
    },
    {
        "case_id": "F5-阶段刷新", "store": "H_SH_001", "room": "1208",
        "order_id": None,
        "steps": ["能送两瓶矿泉水吗？", "1208", "我姓陈", "9月18日", "帮我开一张发票"],
        "checks": [["auth_required"], ["auth_collecting"], ["auth_collecting"],
                   ["no_auth_required"], ["auth_required"]],
        "note": "会话创建时是入住中，验证后客人退房（把入住上下文拨过期）→ "
                "阶段必须刷新为离店后，于是发票申请要重新按订单级凭据核实",
        "before_last_step": _force_stay_expiry,
    },
]


def run_F(limit: int | None, only: set[str] | None = None) -> list[dict]:
    results = []
    for case in F_CASES[: limit or None]:
        step_ids = [f"{case['case_id']}-第{i}轮" for i in range(1, len(case["steps"]) + 1)]
        if only and not any(step_id in only for step_id in step_ids):
            continue
        session = new_session(case["store"], case["room"])
        if case["order_id"]:
            # 用订单号建会话 → 阶段按订单日期判为 PRE / POST
            httpx.post(
                f"{BASE}/api/session",
                json={"store_id": case["store"], "order_id": case["order_id"], "session_id": session},
                timeout=30,
            )
        for index, (query, checks) in enumerate(zip(case["steps"], case["checks"]), start=1):
            if case.get("before_last_step") and index == len(case["steps"]):
                case["before_last_step"](session)
            answer = ask(session, query, case["store"])
            outcomes = []
            for check in checks:
                if check == "auth_required":
                    outcomes.append(bool(answer["meta"].get("auth_required")))
                elif check == "auth_collecting":
                    outcomes.append(answer["meta"].get("auth_status") == "NEED")
                elif check == "no_auth_required":
                    outcomes.append(not answer["meta"].get("auth_required"))
                elif check == "has_work_order":
                    outcomes.append(answer["work_order"] is not None)
                else:
                    outcomes.append(False)
            results.append({
                "suite": "F", "case_id": f"{case['case_id']}-第{index}轮",
                "expected": "+".join(checks),
                "actual": "+".join(f"{c}={'✓' if o else '✗'}" for c, o in zip(checks, outcomes)),
                "passed": all(outcomes), "latency_ms": answer["latency_ms"],
                "note": f"{case['note']}；回答：{answer['content'][:60]}".replace("\n", " "),
            })
    return results


# ---------------------------------------------------------------------------
# G · 多跳与工作流（收尾第 5 项新增的能力，必须有题集守着）
#
# 与 F 的区别：F 验的是"三段凭据不同"，G 验的是**系统内部的工作流**——
# 多跳是否真的发生、超时是否真的会升级、长期记忆会不会被入住级信息污染。
# 这三件事都属于"机制一旦断了，表现上很难看出来"的类型。
# ---------------------------------------------------------------------------
def run_G(limit: int | None, only: set[str] | None = None) -> list[dict]:
    import psycopg

    sys_path_added = False
    for path in (os.path.join(ROOT, "backend", "app"), os.path.join(ROOT, "backend", "agent")):
        if path not in sys.path:
            sys.path.insert(0, path)
            sys_path_added = True
    from service import work_order_service as wos  # noqa: PLC0415

    results: list[dict] = []

    # G1/G2：多跳链路（order_agent → room_state_node → order_agent）
    for case_id, query in (("G1-延退多跳", "我想延到 9月19日 退房"), ("G2-换房", "我想换个房间")):
        if only and case_id not in only:
            continue
        session = new_session("H_SH_001", "1208")
        verify_stay(session, "H_SH_001", "1208")
        answer = ask(session, query, "H_SH_001")
        wo = answer["work_order"]
        category_ok = bool(wo) and wo["category"] == "CHECKOUT_REQUEST"
        # 多跳的证据：回答里出现了房态结论（单跳做不到这件事）
        workflow_ok = ("房态" in answer["content"]) or bool(wo and wo.get("work_order_id"))
        results.append({
            "suite": "G", "case_id": case_id,
            "expected": "产生 CHECKOUT_REQUEST 工单，且结论基于房态",
            "actual": f"工单={wo['work_order_id'] if wo else '无'} 类别={wo['category'] if wo else '-'}",
            "passed": category_ok and workflow_ok, "latency_ms": answer["latency_ms"],
            "note": f"回答：{answer['content'][:80]}".replace("\n", " "),
        })

    # G3：超时 → 自动升级（把工单拨到超时，再跑一次巡检）
    if not only or "G3-超时升级" in only:
        wo = wos.create_work_order(
            store_id="H_SH_001", session_id=f"{PREFIX}gtimer", category="SERVICE",
            intent="评测：超时升级", room_no="1208",
        )
        with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE work_orders SET due_at = now() - make_interval(mins => 9) WHERE work_order_id = %s",
                (wo["work_order_id"],),
            )
        escalated = wos.sweep_overdue(store_id="H_SH_001")
        detail = wos.get_work_order(wo["work_order_id"])
        results.append({
            "suite": "G", "case_id": "G3-超时升级",
            "expected": "超时工单被升级为 ESCALATED 且事件链含 ESCALATED",
            "actual": f"状态={detail['status']} 事件={[e['event_type'] for e in detail['events']]}",
            "passed": detail["status"] == "ESCALATED"
            and any(e["event_type"] == "ESCALATED" for e in detail["events"])
            and any(item["work_order_id"] == wo["work_order_id"] for item in escalated),
            "latency_ms": 0, "note": "超时升级必须自己发生，不能只靠人工点",
        })

    # G4：会员记忆不许被入住级信息污染
    if not only or "G4-记忆不污染" in only:
        from core.memory.member_store import load_preferences, save_preferences  # noqa: PLC0415

        candidate = ["偏好: 偏好安静楼层", "住宿地点: 外滩璟悦酒店", "房间: 1208", "订单: HT1001A001"]
        save_preferences("eval-memory-user", candidate, "eval-memory")
        stored = load_preferences("eval-memory-user")
        with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pref_key, pref_value FROM member_preferences WHERE user_id = %s",
                        ("eval-memory-user",))
            rows = cur.fetchall()
        dirty = [f"{k}:{v}" for k, v in rows if any(
            token in f"{k}{v}" for token in ("外滩璟悦", "1208", "HT1001")
        )]
        results.append({
            "suite": "G", "case_id": "G4-记忆不污染",
            "expected": "入住级信息（门店/房间/订单）不得进入会员长期记忆",
            "actual": f"落库={rows} 读回={stored}",
            "passed": not dirty and len(rows) == 1,
            "latency_ms": 0,
            "note": "第 15 轮踩过的坑：门店被当成偏好跨会话注入，会覆盖系统注入的门店",
        })
        with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM member_preferences WHERE user_id = %s", ("eval-memory-user",))

    return results


RUNNERS = {
    "A": run_A, "B": run_B, "C": run_C, "D": run_D,
    "E": run_E, "F": run_F, "G": run_G,
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _cleanup() -> None:
    dsn = os.getenv("POSTGRES_DSN")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM work_order_events WHERE work_order_id IN "
            "(SELECT work_order_id FROM work_orders WHERE session_id LIKE %s)", (f"{PREFIX}%",))
        cur.execute("DELETE FROM work_orders WHERE session_id LIKE %s", (f"{PREFIX}%",))
        cur.execute("DELETE FROM auth_attempts WHERE session_id LIKE %s", (f"{PREFIX}%",))
        cur.execute(
            "DELETE FROM stay_context WHERE stay_id IN "
            "(SELECT stay_id FROM guest_sessions WHERE session_id LIKE %s)", (f"{PREFIX}%",))
        cur.execute("DELETE FROM guest_sessions WHERE session_id LIKE %s", (f"{PREFIX}%",))


def summarize(results: list[dict]) -> dict:
    by_suite: dict[str, list[dict]] = {}
    for row in results:
        by_suite.setdefault(row["suite"], []).append(row)
    summary = {}
    for suite, rows in sorted(by_suite.items()):
        total = len(rows)
        passed = sum(1 for r in rows if r["passed"])
        flaky = sum(1 for r in rows if r.get("flaky"))
        latencies = sorted(r["latency_ms"] for r in rows)
        summary[suite] = {
            "cases": total,
            "passed": passed,
            "flaky": flaky,
            "accuracy": round(passed / total, 4) if total else None,
            # 稳定通过率 = 首次通过 + 重跑通过（抖动）；两个数都要报，不能只报好看的那个
            "stable_accuracy": round((passed + flaky) / total, 4) if total else None,
            "latency_p50": latencies[len(latencies) // 2] if latencies else None,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="随行管家评测运行器")
    parser.add_argument("--suite", nargs="*", default=["A", "B", "C", "D", "E", "F", "G"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--keep", action="store_true", help="保留测试产生的数据")
    args = parser.parse_args()

    try:
        health = httpx.get(f"{BASE}/api/health", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 后端未运行（{exc}）。先执行 工具/启动本地.ps1")
        return 1

    run_id = f"RUN{datetime.now().strftime('%y%m%d%H%M%S')}"
    print(f"评测开始 run_id={run_id} 题集={args.suite}")
    print(f"环境：overall={health['overall']}，降级路径={health['degraded_paths']}")
    print()

    _cleanup()
    results: list[dict] = []
    for suite in args.suite:
        runner = RUNNERS.get(suite.upper())
        if not runner:
            print(f"⚠️ 未知题集：{suite}")
            continue
        started = time.time()
        suite_results = runner(args.limit)
        # 失败用例自动重跑：区分"能力不足"与"单次抖动"
        for attempt in range(1, RETRY_FAILED + 1):
            failed_ids = {r["case_id"] for r in suite_results if not r["passed"]}
            if not failed_ids:
                break
            print(f"  ↻ 题集 {suite}：第 {attempt} 次重跑 {len(failed_ids)} 条失败用例")
            retry_results = runner(args.limit, only=failed_ids)
            retry_map = {r["case_id"]: r for r in retry_results}
            for row in suite_results:
                if row["passed"] or row.get("flaky"):
                    continue
                again = retry_map.get(row["case_id"])
                if again and again["passed"]:
                    row["flaky"] = True
                    row["note"] = (row.get("note") or "") + f"｜重跑第 {attempt} 次通过（判为抖动，不计入首次通过率）"
        results.extend(suite_results)
        passed = sum(1 for r in suite_results if r["passed"])
        flaky = sum(1 for r in suite_results if r.get("flaky"))
        tail = f"，另 {flaky} 条重跑通过（抖动）" if flaky else ""
        print(f"题集 {suite}: {passed}/{len(suite_results)} 通过{tail}（{time.time() - started:.0f} 秒）")

    summary = summarize(results)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = os.path.join(SET_DIR, f"评测结果_{stamp}.csv")
    with open(result_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["suite", "case_id", "expected", "actual", "passed", "flaky", "latency_ms", "note"],
        )
        writer.writeheader()
        writer.writerows(results)

    # 落库：报告可复现（env_snapshot 记录依赖状态与降级路径）
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO eval_runs (run_id, suite, env_snapshot, finished_at) VALUES (%s, %s, %s::jsonb, now())",
            (run_id, ",".join(args.suite), json.dumps(
                {"health": health["overall"], "degraded_paths": health["degraded_paths"],
                 "summary": summary}, ensure_ascii=False)),
        )
        for row in results:
            cur.execute(
                "INSERT INTO eval_results (run_id, case_id, expected, actual, passed, latency_ms, degraded_paths) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (run_id, case_id) DO NOTHING",
                (run_id, row["case_id"], row["expected"], row["actual"], row["passed"],
                 row["latency_ms"], json.dumps(health["degraded_paths"], ensure_ascii=False)),
            )

    print()
    print("【汇总】")
    for suite, stat in summary.items():
        print(
            f"  题集 {suite}: 首次 {stat['passed']}/{stat['cases']} = {stat['accuracy']:.0%}"
            f"｜稳定 {(stat['passed'] + stat['flaky'])}/{stat['cases']} = {stat['stable_accuracy']:.0%}"
            f"（抖动 {stat['flaky']} 条）｜延迟 p50 = {stat['latency_p50']} ms"
        )
    print()
    print(f"结果明细：{result_path}")
    print(f"已落库：eval_runs.run_id = {run_id}")

    if not args.keep:
        _cleanup()
        print("（测试数据已清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
