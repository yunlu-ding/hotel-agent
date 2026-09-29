"""会话上下文与分级鉴权（05-TDD §5.1 阶段识别 / §5.2 鉴权门）。

两条设计要点：

1. **阶段是会话级的，动作是意图级的**（00 §4.5）。
   住中的客人问"下个月有什么房型"，照常走推荐（L0），只是不能改这张订单。
   所以阶段只决定"身份与可用凭据"，不决定"能问什么"。

2. **房号是弱凭据**。邻居可能知道你的房号，所以房间级授权要配姓氏与离店日；
   而且失败话术**不区分**"房号不存在"与"姓氏不匹配"——否则就成了房号枚举器。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import date, datetime, timezone
from typing import Any

from dotenv import load_dotenv

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DIR = os.path.join(BACKEND, "agent")
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

load_dotenv(os.path.join(AGENT_DIR, ".env"), override=False)

# 鉴权策略只有一份实现（agent 侧），这里复用；详见 core/auth_policy.py 的说明
from core.auth_policy import AUTH_REQUIRED_BY_AGENT, MAX_AUTH_ATTEMPTS  # noqa: E402,F401

# 逐槽位收集凭据：一次问一件事，解析才可靠，也才知道卡在哪一步
AUTH_SLOTS = ("room_no", "last_name", "check_out_date")
SLOT_PROMPTS = {
    "room_no": "好的，为了确认您的住客身份，请告诉我**房间号**（例如 1208）。",
    "last_name": "谢谢，请告诉我**入住人姓氏**（只填姓即可，例如：陈）。",
    "check_out_date": "最后确认一下，您的**离店日期**是哪天？（例如 9月18日 或 2026-09-18）",
}

# 订单级（L2）：用在"客人不在住"的场景（离店后查发票/失物、入住前查订单）
ORDER_SLOTS = ("order_id", "phone_last4")
ORDER_SLOT_PROMPTS = {
    "order_id": "请提供**订单号**（形如 HT1001A001，可在预订确认短信或 App 里找到）。",
    "phone_last4": "再确认一下，**下单手机号的后四位**是多少？",
}
SLOTS_BY_LEVEL = {1: AUTH_SLOTS, 2: ORDER_SLOTS}
PROMPTS_BY_LEVEL = {1: SLOT_PROMPTS, 2: ORDER_SLOT_PROMPTS}

# 统一失败话术：**故意不区分**"不存在"与"不匹配"
UNIFIED_FAIL_MESSAGE = (
    "我这边没能匹配到入住信息。麻烦您核对一下房间号、入住人姓氏和离店日期，"
    "或者直接联系前台/总机，我也可以帮您转过去。"
)


def _connect():
    import psycopg

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("未配置 POSTGRES_DSN")
    return psycopg.connect(dsn, autocommit=True)


def _hash(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def get_or_create_session(
    session_id: str,
    *,
    store_id: str,
    room_no: str | None = None,
    order_id: str | None = None,
    channel: str = "qr_web",
) -> dict[str, Any]:
    """读取会话；不存在则按扫码带入的信息创建（阶段在此时判定一次）。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, store_id, room_no, order_id, stay_id, stage, auth_level, needs_human "
            "FROM guest_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE guest_sessions SET last_active_at = now() WHERE session_id = %s",
                (session_id,),
            )
            return _row_to_session(row)

        stage = detect_stage(store_id=store_id, room_no=room_no, order_id=order_id)
        cur.execute(
            """
            INSERT INTO guest_sessions (session_id, channel, store_id, room_no, order_id, stage)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (session_id, channel, store_id, room_no, order_id, stage),
        )
        return {
            "session_id": session_id, "store_id": store_id, "room_no": room_no,
            "order_id": order_id, "stay_id": None, "stage": stage,
            "auth_level": 0, "needs_human": False,
        }


def _row_to_session(row) -> dict[str, Any]:
    return {
        "session_id": row[0], "store_id": row[1], "room_no": row[2], "order_id": row[3],
        "stay_id": row[4], "stage": row[5], "auth_level": row[6], "needs_human": row[7],
    }


# ---------------------------------------------------------------------------
# 阶段识别（优先级：上下文 > 数据 > 语言）
# ---------------------------------------------------------------------------
def detect_stage(
    *, store_id: str, room_no: str | None = None, order_id: str | None = None,
    today: date | None = None,
) -> str:
    """判定客人处在旅程哪一段：PRE（入住前）/ IN（入住中）/ POST（离店后）/ UNKNOWN。"""
    today = today or datetime.now().date()
    with _connect() as conn, conn.cursor() as cur:
        # 信号 2：数据（订单日期）
        if order_id:
            cur.execute("SELECT check_in, check_out FROM orders WHERE order_id = %s", (order_id,))
            row = cur.fetchone()
            if row:
                check_in, check_out = row[0], row[1]
                if today < check_in:
                    return "PRE"
                if check_in <= today <= check_out:
                    return "IN"
                return "POST"

        # 信号 1：上下文（房号 → 查 PMS 房态镜像）
        if room_no:
            if not store_id:
                return "UNKNOWN"
            cur.execute(
                "SELECT room_status, check_in_date, check_out_date FROM pms_room_state "
                "WHERE store_id = %s AND room_no = %s",
                (store_id, room_no),
            )
            row = cur.fetchone()
            if row and row[1] and row[2]:
                check_in, check_out = row[1], row[2]
                if today < check_in:
                    return "PRE"
                if check_in <= today <= check_out:
                    return "IN"
                return "POST"
            # 有房号但查不到 → 可能是脏房号，不猜
            return "UNKNOWN"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
def _attempts_failed(session_id: str) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM auth_attempts WHERE session_id = %s AND result = 'FAIL'",
            (session_id,),
        )
        return int(cur.fetchone()[0])


def _record_attempt(session_id: str, credential_type: str, fingerprint: str,
                    result: str, reason: str | None = None) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auth_attempts (session_id, credential_type, submitted_hash, result, fail_reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, credential_type, fingerprint[:64], result, reason),
        )


def verify_stay(
    *, session_id: str, store_id: str, room_no: str, last_name: str,
    check_out_date: date | str, grace_hours: int | None = None,
) -> dict[str, Any]:
    """房间级鉴权：房号 + 姓氏 + 离店日。

    三要素都对齐才通过；失败返回**统一话术**并计数，连续失败过多则建议转前台。
    """
    if isinstance(check_out_date, str):
        check_out_date = date.fromisoformat(check_out_date[:10])
    fingerprint = _hash(room_no, last_name)

    if _attempts_failed(session_id) >= MAX_AUTH_ATTEMPTS:
        return {
            "result": "LOCKED",
            "message": "已经连续几次没能匹配上，我帮您转前台核实吧，避免耽误您时间。",
            "auth_level": 0,
        }

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT guest_last_name, check_in_date, check_out_date, room_status "
            "FROM pms_room_state WHERE store_id = %s AND room_no = %s",
            (store_id, room_no.strip()),
        )
        row = cur.fetchone()

    ok = bool(
        row
        and row[0]
        and str(row[0]).strip() == last_name.strip()
        and row[2] == check_out_date
    )
    if not ok:
        reason = "no_match"
        _record_attempt(session_id, "ROOM", fingerprint, "FAIL", reason)
        failures = _attempts_failed(session_id)
        # 到上限就**当次**转前台，不要让客人再试第 4 次
        if failures >= MAX_AUTH_ATTEMPTS:
            return {
                "result": "LOCKED",
                "message": "已经连续几次没能匹配上，我帮您转前台核实吧，避免耽误您时间。",
                "auth_level": 0,
            }
        remaining = MAX_AUTH_ATTEMPTS - failures
        return {
            "result": "FAIL",
            "message": UNIFIED_FAIL_MESSAGE,
            "remaining_attempts": max(remaining, 0),
            "auth_level": 0,
        }

    if grace_hours is None:
        try:
            from config import get_settings

            grace_hours = get_settings().stay_grace_hours
        except Exception:  # noqa: BLE001
            grace_hours = 2

    stay_id = f"ST{datetime.now().strftime('%y%m%d')}{uuid.uuid4().hex[:6].upper()}"
    expires_at = datetime.combine(row[2], datetime.min.time(), tzinfo=timezone.utc) + \
        __import__("datetime").timedelta(hours=grace_hours)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stay_context (stay_id, store_id, room_no, guest_last_name,
                                      check_in_date, check_out_date, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stay_id) DO NOTHING
            """,
            (stay_id, store_id, room_no.strip(), last_name.strip(), row[1], row[2], expires_at),
        )
        # 房间级通过 → 升级会话；若正处入住中，阶段也一并确认
        cur.execute(
            """
            UPDATE guest_sessions
               SET stay_id = %s, auth_level = GREATEST(auth_level, 1),
                   auth_verified_at = now(),
                   stage = CASE WHEN stage = 'UNKNOWN' THEN 'IN' ELSE stage END,
                   last_active_at = now()
             WHERE session_id = %s
            """,
            (stay_id, session_id),
        )
    _record_attempt(session_id, "ROOM", fingerprint, "PASS")
    return {
        "result": "PASS",
        "stay_id": stay_id,
        "auth_level": 1,
        "room_no": room_no.strip(),
        "check_out_date": row[2].isoformat(),
        "message": f"已确认您是 {room_no.strip()} 房的住客（离店日 {row[2].isoformat()}）。",
    }


def current_auth_level(session_id: str) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT auth_level FROM guest_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def refresh_stage(session_id: str) -> dict[str, Any]:
    """把"已经过时"的会话阶段推进一格。

    为什么需要它：阶段是**会话创建时判定一次**的快照。如果客人在会话开着的时候退了房
    （或者会话跨了退房时间），这个会话会永远停在 `IN`——
    于是"离店后的事务"仍按房间级凭据放行，一次长会话就能绕过订单级验证。
    这不是理论问题：把入住上下文的过期时间拨到过去即可复现（见题集 F5）。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.stage, s.stay_id, c.expires_at
              FROM guest_sessions s
              LEFT JOIN stay_context c ON c.stay_id = s.stay_id
             WHERE s.session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"stage": "UNKNOWN", "refreshed": False}
        stage, stay_id, expires_at = row
        if stage == "IN" and stay_id and expires_at and expires_at < datetime.now(timezone.utc):
            cur.execute("UPDATE guest_sessions SET stage = 'POST' WHERE session_id = %s", (session_id,))
            return {"stage": "POST", "refreshed": True, "reason": "入住上下文已过期"}
    return {"stage": stage, "refreshed": False}


def auth_stats(store_id: str | None = None) -> dict[str, Any]:
    """鉴权口径：拦截率与误挡率必须一起看（05-TDD §5.2）。"""
    with _connect() as conn, conn.cursor() as cur:
        sql = """
            SELECT count(*) FILTER (WHERE result = 'PASS') AS passed,
                   count(*) FILTER (WHERE result = 'FAIL') AS failed,
                   count(*) AS total
              FROM auth_attempts
        """
        params: list[Any] = []
        if store_id:
            sql += " WHERE session_id IN (SELECT session_id FROM guest_sessions WHERE store_id = %s)"
            params.append(store_id)
        cur.execute(sql, params)
        passed, failed, total = cur.fetchone()
    return {
        "鉴权通过": passed,
        "鉴权失败": failed,
        "总数": total,
        "通过率": round(passed / total, 4) if total else None,
        "note": "失败率高不一定是攻击，也可能是引导没说清要报什么——这就是要把它做成独立节点的原因。",
    }


def expire_checkouts() -> int:
    """退房即失效（隐私红线，03 §3.2）。返回被清理的条数。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE stay_context
               SET guest_last_name = '', status = 'EXPIRED'
             WHERE status = 'IN_HOUSE' AND expires_at < now()
            """
        )
        expired = cur.rowcount
        cur.execute(
            """
            UPDATE guest_sessions
               SET auth_level = 0, stay_id = NULL
             WHERE stay_id IN (SELECT stay_id FROM stay_context WHERE status = 'EXPIRED')
            """
        )
    return expired


# ---------------------------------------------------------------------------
# 凭据收集（逐槽位引导）
#
# 为什么逐槽位问，而不是让客人一句话说完：
#   · 一句话解析（"1208 陈 9月18号"）靠正则很脆，靠模型又多一次不确定；
#   · 逐槽位问能**准确知道卡在哪一步**——这对度量"鉴权误挡率"是关键，
#     因为"客人说不清"和"我们的引导没说清"是两种完全不同的问题。
# ---------------------------------------------------------------------------
_NAME_STOPWORDS = {"我", "是", "姓", "的", "人", "先生", "女士", "你好", "谢谢", "房间", "入住"}
_ESCAPE_WORDS = ("人工", "前台", "转人工", "总机", "算了")


def _parse_room_no(text: str) -> str | None:
    import re

    match = re.search(r"\b(\d{3,4})\b", text) or re.search(r"(\d{3,4})", text)
    return match.group(1) if match else None


def _parse_last_name(text: str) -> str | None:
    import re

    cleaned = text.strip().strip("，。,.!！?？ ")
    match = re.search(r"(?:姓|叫|是)\s*([\u4e00-\u9fa5]{1,2})", cleaned)
    if match:
        return match.group(1)
    if re.fullmatch(r"[\u4e00-\u9fa5]{1,2}", cleaned) and cleaned not in _NAME_STOPWORDS:
        return cleaned
    tokens = [t for t in re.findall(r"[\u4e00-\u9fa5]{1,2}", cleaned) if t not in _NAME_STOPWORDS]
    return tokens[-1] if tokens else None


def _parse_date(text: str, today: date | None = None) -> date | None:
    import re
    from datetime import timedelta

    today = today or datetime.now().date()
    cleaned = text.strip()
    for word, delta in (("今天", 0), ("明天", 1), ("后天", 2)):
        if word in cleaned:
            return today + timedelta(days=delta)

    match = re.search(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", cleaned)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = re.search(r"(\d{1,2})\s*[-/月]\s*(\d{1,2})", cleaned)
    if match:
        return date(today.year, int(match.group(1)), int(match.group(2)))

    match = re.search(r"(\d{1,2})\s*[日号]", cleaned)
    if match:
        return date(today.year, today.month, int(match.group(1)))
    return None


def required_auth_for_agent(agent: str) -> int:
    return AUTH_REQUIRED_BY_AGENT.get(agent, 0)


def begin_auth(session_id: str, *, required_level: int, original_query: str) -> dict[str, Any]:
    """开始收集凭据。**等级决定问什么**：
        1（房间级）→ 房号 + 姓氏 + 离店日：客人在住，最自然的凭据是"我住哪间房"
        2（订单级）→ 订单号 + 手机后四位：客人不在住（离店后查发票/失物、入住前查订单）
    """
    level = max(1, min(int(required_level or 1), 2))
    slots = SLOTS_BY_LEVEL[level]
    prompts = PROMPTS_BY_LEVEL[level]
    state = {
        "level": level,
        "collected": {},
        "next_slot": slots[0],
        "original_query": original_query,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending(session_id, state)
    return {"status": "NEED", "slot": slots[0], "level": level, "prompt": prompts[slots[0]]}


def _save_pending(session_id: str, state: dict[str, Any] | None) -> None:
    import json

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE guest_sessions SET pending_auth = %s::jsonb, last_active_at = now() "
            "WHERE session_id = %s",
            (json.dumps(state, ensure_ascii=False) if state else None, session_id),
        )


def pending_auth_state(session_id: str) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pending_auth FROM guest_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def advance_auth(session_id: str, message: str, *, store_id: str) -> dict[str, Any]:
    """推进凭据收集。返回 status ∈ {NEED, OK, FAIL, LOCKED, ESCAPE, NONE}。"""
    state = pending_auth_state(session_id)
    if not state:
        return {"status": "NONE"}

    # 出路：客人在验证过程中可以直接要求转人工（不做鉴权拦截）
    if any(word in message for word in _ESCAPE_WORDS):
        _save_pending(session_id, None)
        return {"status": "ESCAPE", "message": "好的，我帮您转前台核实。"}

    level = max(1, min(int(state.get("level") or 1), 2))
    slots = SLOTS_BY_LEVEL[level]
    prompts = PROMPTS_BY_LEVEL[level]
    slot = state["next_slot"]
    value: Any = _parse_slot(slot, message)

    if value is None:
        return {
            "status": "NEED",
            "slot": slot,
            "level": level,
            "prompt": "抱歉，我没听清。" + prompts[slot],
            "retry": True,
        }

    state["collected"][slot] = str(value)
    index = slots.index(slot)
    if index + 1 < len(slots):
        state["next_slot"] = slots[index + 1]
        _save_pending(session_id, state)
        return {
            "status": "NEED",
            "slot": state["next_slot"],
            "level": level,
            "prompt": prompts[state["next_slot"]],
        }

    # 凭据收齐 → 真实校验
    if level == 1:
        result = verify_stay(
            session_id=session_id,
            store_id=store_id,
            room_no=state["collected"]["room_no"],
            last_name=state["collected"]["last_name"],
            check_out_date=state["collected"]["check_out_date"],
        )
    else:
        result = verify_order(
            session_id=session_id,
            order_id=state["collected"]["order_id"],
            phone_last4=state["collected"]["phone_last4"],
        )
    _save_pending(session_id, None)
    result["original_query"] = state.get("original_query", "")
    if result["result"] == "PASS":
        result["status"] = "OK"
    elif result["result"] == "LOCKED":
        result["status"] = "LOCKED"
    else:
        result["status"] = "FAIL"
        result["prompt"] = result["message"] + "\n\n我们可以再试一次——" + prompts[slots[0]]
    return result


def _parse_slot(slot: str, message: str) -> str | None:
    import re

    if slot == "room_no":
        return _parse_room_no(message)
    if slot == "last_name":
        return _parse_last_name(message)
    if slot == "check_out_date":
        parsed = _parse_date(message)
        return parsed.isoformat() if parsed else None
    if slot == "order_id":
        match = re.search(r"\b([A-Za-z]{2}\d{4}[A-Za-z]?\d{3})\b", message)
        return match.group(1).upper() if match else None
    if slot == "phone_last4":
        match = re.search(r"(\d{4})\b", message) or re.search(r"(\d{4})", message)
        return match.group(1) if match else None
    return None


def verify_order(*, session_id: str, order_id: str, phone_last4: str) -> dict[str, Any]:
    """订单级鉴权：订单号 + 下单手机号后四位。

    手机号**只用于后四位比对**，通过后不落明文（auth_attempts 里只存哈希）。
    """
    fingerprint = _hash(order_id, phone_last4)
    if _attempts_failed(session_id) >= MAX_AUTH_ATTEMPTS:
        return {
            "result": "LOCKED",
            "message": "已经连续几次没能匹配上，我帮您转前台核实吧，避免耽误您时间。",
            "auth_level": 0,
        }

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.order_id, o.user_id, o.check_in, o.check_out
              FROM orders o JOIN users u ON u.user_id = o.user_id
             WHERE upper(o.order_id) = upper(%s)
               AND right(coalesce(u.phone, ''), 4) = %s
            """,
            (order_id.strip(), phone_last4.strip()),
        )
        row = cur.fetchone()

    if not row:
        _record_attempt(session_id, "ORDER", fingerprint, "FAIL", "no_match")
        failures = _attempts_failed(session_id)
        if failures >= MAX_AUTH_ATTEMPTS:
            return {
                "result": "LOCKED",
                "message": "已经连续几次没能匹配上，我帮您转前台核实吧，避免耽误您时间。",
                "auth_level": 0,
            }
        return {
            "result": "FAIL",
            "message": "我这边没能匹配到这笔订单。麻烦您核对订单号与手机号后四位，或者我帮您转前台。",
            "remaining_attempts": max(MAX_AUTH_ATTEMPTS - failures, 0),
            "auth_level": 0,
        }

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE guest_sessions
               SET order_id = %s, auth_level = GREATEST(auth_level, 2),
                   auth_verified_at = now(), last_active_at = now()
             WHERE session_id = %s
            """,
            (row[0], session_id),
        )
    _record_attempt(session_id, "ORDER", fingerprint, "PASS")
    return {
        "result": "PASS",
        "auth_level": 2,
        "order_id": row[0],
        "message": f"已核对订单 {row[0]}（入住 {row[2]}，离店 {row[3]}）。",
    }
