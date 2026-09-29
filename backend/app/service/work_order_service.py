"""工单域：本项目的心脏（05-TDD §5.4）。

它存在的理由：住中服务的价值不是"答得对"，而是"事办了"。
所以这里的每个设计都服务于同一件事——**让"办没办成"变成可计算的数字**。

三条硬约束（违反了就等于把工单退回成聊天记录）：
1. **类别决定部门与 SLA**：`sla_minutes` / `assignee_dept` / `priority` 一律由
   本模块按 `category` 映射得出，**不接受调用方（包括大模型）传参**——
   否则模型可以给自己发一个 24 小时的 SLA。
2. **每一次流转都落 `work_order_events`**：没有事件链，
   "未接单 / 未完成 / 未回执"这三类失败就无法归因。
3. **安全类允许"先派单、后验证"**：漏水漏电这类场景，延误的代价高于误派。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_FILE = os.path.join(BACKEND, "agent", ".env")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ENV_FILE, override=False)

if sys.path and os.path.join(BACKEND, "agent") not in sys.path:
    sys.path.insert(0, os.path.join(BACKEND, "agent"))


# ---------------------------------------------------------------------------
# 类别 → 部门 / SLA / 优先级（唯一事实来源）
# ---------------------------------------------------------------------------
CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "SERVICE": {
        "dept": "HOUSEKEEPING", "priority": "P2", "sla_setting": "sla_service_minutes",
        "label": "客房服务",
    },
    "MAINTENANCE": {
        "dept": "ENGINEERING", "priority": "P1", "sla_setting": "sla_maintenance_minutes",
        "label": "工程报修",
    },
    "MAINTENANCE_SAFETY": {
        "dept": "ENGINEERING", "priority": "P0", "sla_setting": "sla_safety_minutes",
        "label": "安全类报修（漏水/漏电/燃气/门锁）",
        "also_notify": "DUTY_MANAGER",
        "allow_unverified": True,   # 先派单、后验证
    },
    "COMPLAINT": {
        "dept": "DUTY_MANAGER", "priority": "P1", "sla_setting": "sla_service_minutes",
        "label": "投诉（双通道：派人 + 人工关注）",
        "also_notify": "FRONT_DESK",
        "mark_needs_human": True,
    },
    "ESCALATION": {
        # 转人工类升级：人身安全、隐私事件、明确要求人工、法律/媒体威胁
        "dept": "DUTY_MANAGER", "priority": "P0", "sla_setting": "sla_safety_minutes",
        "label": "人工升级",
        "also_notify": "FRONT_DESK",
        "mark_needs_human": True,
    },
    "CHECKOUT_REQUEST": {
        "dept": "FRONT_DESK", "priority": "P2", "sla_setting": "sla_maintenance_minutes",
        "label": "延退 / 换房申请",
    },
    "INVOICE": {
        "dept": "FRONT_DESK", "priority": "P2", "sla_setting": None, "sla_fixed_minutes": 1440,
        "label": "发票申请",
    },
    "LOST_FOUND": {
        "dept": "HOUSEKEEPING", "priority": "P2", "sla_setting": None, "sla_fixed_minutes": 240,
        "label": "失物招领",
    },
}

DEFAULT_RULE = {
    "dept": "FRONT_DESK", "priority": "P2", "sla_setting": "sla_service_minutes",
    "label": "未分类请求",
}

# 状态机：合法流转（其他一律拒绝）
TRANSITIONS: dict[str, dict[str, str]] = {
    "assign":   {"from": {"CREATED", "ESCALATED", "FAILED"}, "to": "ASSIGNED"},
    "accept":   {"from": {"ASSIGNED"}, "to": "ACCEPTED"},
    "done":     {"from": {"ACCEPTED"}, "to": "DONE"},
    "fail":     {"from": {"ACCEPTED"}, "to": "FAILED"},
    "escalate": {"from": {"CREATED", "ASSIGNED", "ACCEPTED"}, "to": "ESCALATED"},
    "cancel":   {"from": {"CREATED", "ASSIGNED"}, "to": "CANCELLED"},
}


def _sla_minutes(rule: dict[str, Any]) -> int:
    if rule.get("sla_fixed_minutes"):
        return int(rule["sla_fixed_minutes"])
    try:
        from config import get_settings

        return int(getattr(get_settings(), rule["sla_setting"]))
    except Exception:  # noqa: BLE001  （脱离 agent 环境单独跑时用兜底值）
        return {"sla_service_minutes": 20, "sla_maintenance_minutes": 30, "sla_safety_minutes": 10}.get(
            rule.get("sla_setting", "sla_service_minutes"), 20
        )


def _connect():
    import psycopg

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("未配置 POSTGRES_DSN")
    return psycopg.connect(dsn, autocommit=True)


def _log(message: str) -> None:
    """安全日志：写 stderr、不含 emoji、吞掉编码异常。

    为什么这么谨慎（都是实测踩过的）：
      1. MCP server 是 stdio 子进程，它的 **stdout 是协议通道**——往里 print 会污染协议；
      2. Windows 下子进程默认输出编码是 GBK，打印 emoji（如 U+26A0 ⚠）会抛
         UnicodeEncodeError，把整个工具调用带崩（表现为"客人看到已建单、实际没建单"）；
      3. 日志再重要也不能影响业务链路，所以这里必须吞异常。
    """
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _new_id() -> str:
    return f"WO{datetime.now().strftime('%y%m%d')}{uuid.uuid4().hex[:6].upper()}"


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------
def create_work_order(
    *,
    store_id: str,
    session_id: str,
    category: str,
    intent: str = "",
    room_no: str | None = None,
    stay_id: str | None = None,
    order_id: str | None = None,
    payload: dict[str, Any] | None = None,
    guest_verified: bool = True,
    actor: str = "system",
) -> dict[str, Any]:
    """创建工单并自动派到默认部门。

    注意：这里**不接受** sla_minutes / assignee_dept / priority 参数——
    它们由 category 决定（见模块 docstring 第 1 条）。
    """
    rule = CATEGORY_RULES.get(category, DEFAULT_RULE)
    sla = _sla_minutes(rule)
    now = datetime.now(timezone.utc)
    due_at = now + timedelta(minutes=sla)
    work_order_id = _new_id()

    # 注意区分两件事（这里踩过一次）：
    #   allow_unverified —— 规则层面的"**允许**在未验证时派单"（安全类专属）
    #   guest_verified   —— 事实层面的"**这位客人是否真的通过了身份验证**"
    # 之前把两者用 or 连起来，导致安全类工单一律被记成"已验证"，
    # 直接污染了 P3 的鉴权度量。允许 ≠ 已验证。
    verified = bool(guest_verified)
    if not verified and not rule.get("allow_unverified"):
        # 非安全类工单理论上不该走到这里（鉴权门会先拦），留一条兜底日志便于排查
        _log(f"[work-order] 非安全类工单未经身份验证：category={category}")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_orders (
                work_order_id, store_id, room_no, stay_id, order_id, session_id,
                category, intent, payload, priority, status, assignee_dept,
                assignee_type, guest_verified, sla_minutes, due_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, 'ASSIGNED', %s,
                'HUMAN', %s, %s, %s
            )
            """,
            (
                work_order_id, store_id, room_no, stay_id, order_id, session_id,
                category, intent, json.dumps(payload or {}, ensure_ascii=False),
                rule["priority"], rule["dept"], verified, sla, due_at,
            ),
        )
        _add_event(cur, work_order_id, "CREATED", None, "CREATED", actor,
                   f"类别={category} 优先级={rule['priority']} SLA={sla}分钟")
        _add_event(cur, work_order_id, "ASSIGNED", "CREATED", "ASSIGNED", "system",
                   f"派单至 {rule['dept']}")

        # 投诉双通道：既派人，也把会话标记为需人工关注
        if rule.get("mark_needs_human") and session_id:
            cur.execute(
                "UPDATE guest_sessions SET needs_human = TRUE WHERE session_id = %s",
                (session_id,),
            )
        if rule.get("also_notify"):
            _add_event(cur, work_order_id, "NOTIFIED", None, None, "system",
                       f"同步通知 {rule['also_notify']}")

    return {
        "work_order_id": work_order_id,
        "category": category,
        "category_label": rule["label"],
        "priority": rule["priority"],
        "assignee_dept": rule["dept"],
        "assignee_type": "HUMAN",
        "sla_minutes": sla,
        "due_at": due_at.isoformat(),
        "status": "ASSIGNED",
        "guest_verified": verified,
    }


def _add_event(cur, work_order_id: str, event_type: str, from_status, to_status, actor, note: str) -> None:
    cur.execute(
        """
        INSERT INTO work_order_events (work_order_id, event_type, from_status, to_status, actor, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (work_order_id, event_type, from_status, to_status, actor, note),
    )


# ---------------------------------------------------------------------------
# 流转
# ---------------------------------------------------------------------------
def transition(
    work_order_id: str,
    action: str,
    *,
    actor: str = "staff",
    note: str | None = None,
    assignee_name: str | None = None,
) -> dict[str, Any]:
    """执行一次状态流转。非法流转会被明确拒绝（而不是静默改状态）。"""
    rule = TRANSITIONS.get(action)
    if not rule:
        raise ValueError(f"未知动作：{action}（可用：{', '.join(TRANSITIONS)}）")

    now = datetime.now(timezone.utc)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, due_at, store_id, session_id, category FROM work_orders WHERE work_order_id = %s",
            (work_order_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"工单不存在：{work_order_id}")
        status, due_at, _store_id, _session_id, category = row

        if status not in rule["from"]:
            raise ValueError(f"非法流转：{status} --{action}--> {rule['to']}（允许的来源状态：{sorted(rule['from'])}）")

        target = rule["to"]
        updates = ["status = %s"]
        params: list[Any] = [target]

        if action == "accept":
            updates.append("accepted_at = %s")
            params.append(now)
        elif action == "done":
            updates.append("done_at = %s")
            params.append(now)
            updates.append("on_time = %s")
            params.append(bool(due_at and now <= due_at))
        elif action == "fail":
            updates.append("failed_reason = %s")
            params.append(note or "未说明原因")
            updates.append("on_time = FALSE")
        if assignee_name:
            updates.append("assignee_name = %s")
            params.append(assignee_name)
        if action == "assign":
            updates.append("accepted_at = NULL")

        params.append(work_order_id)
        cur.execute(f"UPDATE work_orders SET {', '.join(updates)} WHERE work_order_id = %s", params)
        # 事件类型统一用**目标状态**，而不是动作名：
        # 动作是 accept、状态是 ACCEPTED，两者混用会让事件链与状态机对不上，
        # 下游按事件类型统计时就会漏掉一半（实测踩过）。
        _add_event(cur, work_order_id, target, status, target, actor,
                   note or f"{status} -> {target}")

        cur.execute(
            "SELECT status, on_time, due_at, accepted_at, done_at FROM work_orders WHERE work_order_id = %s",
            (work_order_id,),
        )
        final = cur.fetchone()

    return {
        "work_order_id": work_order_id,
        "action": action,
        "from_status": status,
        "to_status": final[0],
        "on_time": final[1],
        "due_at": final[2].isoformat() if final[2] else None,
        "accepted_at": final[3].isoformat() if final[3] else None,
        "done_at": final[4].isoformat() if final[4] else None,
    }


def notify_guest(work_order_id: str, actor: str = "system") -> dict[str, Any]:
    """记录"已把结果回执给客人"。缺了这一步，工单就是 DONE 但客人不知道。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_orders SET guest_notified = TRUE WHERE work_order_id = %s RETURNING status",
            (work_order_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"工单不存在：{work_order_id}")
        _add_event(cur, work_order_id, "NOTIFIED", row[0], row[0], actor, "已向客人回执")
    return {"work_order_id": work_order_id, "guest_notified": True}


# ---------------------------------------------------------------------------
# 超时升级（P5：承诺必须有人兜底）
# ---------------------------------------------------------------------------
def sweep_overdue(store_id: str | None = None, actor: str = "scheduler") -> list[dict[str, Any]]:
    """把已超时且未完成的工单升级为 ESCALATED，并落到事件链。

    真实环境里这应该是定时任务；本项目由 工具/工单巡检.py 手动触发，
    这样评测时"超时"可以被确定性地复现，而不是等 20 分钟。
    """
    escaped: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    with _connect() as conn, conn.cursor() as cur:
        sql = """
            SELECT work_order_id, status, due_at, store_id, assignee_dept, category
              FROM work_orders
             WHERE status IN ('CREATED', 'ASSIGNED', 'ACCEPTED')
               AND due_at < %s
        """
        params: list[Any] = [now]
        if store_id:
            sql += " AND store_id = %s"
            params.append(store_id)
        sql += " ORDER BY due_at"
        cur.execute(sql, params)
        rows = cur.fetchall()

        for work_order_id, status, due_at, row_store, dept, category in rows:
            cur.execute(
                "UPDATE work_orders SET status = 'ESCALATED' WHERE work_order_id = %s",
                (work_order_id,),
            )
            overdue_min = int((now - due_at).total_seconds() // 60)
            _add_event(
                cur, work_order_id, "ESCALATED", status, "ESCALATED", actor,
                f"超时 {overdue_min} 分钟未完成，由 {dept} 升级至值班经理（类别={category}）",
            )
            escaped.append(
                {
                    "work_order_id": work_order_id,
                    "store_id": row_store,
                    "previous_status": status,
                    "overdue_minutes": overdue_min,
                    "assignee_dept": dept,
                }
            )
    return escaped


# ---------------------------------------------------------------------------
# 查询与统计
# ---------------------------------------------------------------------------
def get_work_order(work_order_id: str) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT work_order_id, store_id, room_no, category, status, priority,
                   assignee_dept, sla_minutes, due_at, accepted_at, done_at,
                   on_time, failed_reason, guest_notified, payload, created_at
              FROM work_orders WHERE work_order_id = %s
            """,
            (work_order_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        result = dict(zip(cols, row))

        cur.execute(
            """
            SELECT event_type, from_status, to_status, actor, note, created_at
              FROM work_order_events WHERE work_order_id = %s ORDER BY event_id
            """,
            (work_order_id,),
        )
        events = []
        for event in cur.fetchall():
            events.append(
                {
                    "event_type": event[0], "from": event[1], "to": event[2],
                    "actor": event[3], "note": event[4],
                    "at": event[5].isoformat() if event[5] else None,
                }
            )
        result["events"] = events
        return result


def list_work_orders(
    store_id: str | None = None, status: str | None = None,
    dept: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """工单池查询（坐席台与看板用）。"""
    sql = """
        SELECT work_order_id, store_id, room_no, category, priority, status,
               assignee_dept, sla_minutes, due_at, on_time, guest_notified, created_at
          FROM work_orders WHERE 1 = 1
    """
    params: list[Any] = []
    if store_id:
        sql += " AND store_id = %s"
        params.append(store_id)
    if status:
        sql += " AND status = %s"
        params.append(status)
    if dept:
        sql += " AND assignee_dept = %s"
        params.append(dept)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_for_session(session_id: str, since: datetime | None = None) -> dict[str, Any] | None:
    """取本次会话最近一张工单（可限定"本轮之后创建的"）。

    用途：Agent 只把文本交回主图，工单号必须由服务层回捞，
    才能把 `work_order` 事件推给前端、并写进埋点。
    """
    sql = (
        "SELECT work_order_id, category, status, assignee_dept, priority, "
        "       sla_minutes, due_at, room_no "
        "  FROM work_orders WHERE session_id = %s"
    )
    params: list[Any] = [session_id]
    if since is not None:
        sql += " AND created_at >= %s"
        params.append(since)
    sql += " ORDER BY created_at DESC LIMIT 1"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return None
        return {
            "work_order_id": row[0], "category": row[1], "status": row[2],
            "assignee_dept": row[3], "priority": row[4], "sla_minutes": row[5],
            "due_at": row[6].isoformat() if row[6] else None, "room_no": row[7],
        }


def failure_attribution(store_id: str | None = None, hours: int | None = None) -> dict[str, int]:
    """失败归因四类：未接单 / 未完成 / 已失败 / 未回执。

    "未建单"不在本函数里 —— 它属于**没有被记录下来的请求**，
    只能通过评测集（题集 D）对比"应该建单"与"实际建单"来算。
    """
    where = "WHERE 1 = 1"
    params: list[Any] = []
    if store_id:
        where += " AND store_id = %s"
        params.append(store_id)
    if hours:
        where += " AND created_at > now() - make_interval(hours => %s)"
        params.append(hours)

    sql = f"""
        SELECT
            count(*) FILTER (WHERE status = 'ESCALATED' AND accepted_at IS NULL)                    AS not_accepted,
            count(*) FILTER (WHERE status = 'ESCALATED' AND accepted_at IS NOT NULL AND done_at IS NULL) AS not_done,
            count(*) FILTER (WHERE status = 'FAILED')                                              AS failed,
            count(*) FILTER (WHERE status = 'DONE' AND guest_notified = FALSE)                     AS not_notified,
            count(*) FILTER (WHERE status = 'DONE')                                                AS done,
            count(*)                                                                               AS total
          FROM work_orders {where}
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        not_accepted, not_done, failed, not_notified, done, total = cur.fetchone()
    return {
        "未接单": not_accepted,
        "未完成": not_done,
        "已失败": failed,
        "已完成但未回执": not_notified,
        "已完成": done,
        "总数": total,
    }


def metrics(store_id: str | None = None, hours: int | None = None) -> dict[str, Any]:
    """主北极星与反向指标：闭环率 / 按时闭环率。目标值待第 11 轮基线后反填。"""
    where = "WHERE 1 = 1"
    params: list[Any] = []
    if store_id:
        where += " AND store_id = %s"
        params.append(store_id)
    if hours:
        where += " AND created_at > now() - make_interval(hours => %s)"
        params.append(hours)
    sql = f"""
        SELECT count(*),
               count(*) FILTER (WHERE status = 'DONE'),
               count(*) FILTER (WHERE status = 'DONE' AND on_time),
               count(*) FILTER (WHERE priority = 'P0')
          FROM work_orders {where}
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        total, done, on_time, safety = cur.fetchone()

    def rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    return {
        "工单总数": total,
        "安全类工单": safety,
        "闭环率": rate(done, total),
        "按时闭环率": rate(on_time, total),
        "失败归因": failure_attribution(store_id, hours),
    }
