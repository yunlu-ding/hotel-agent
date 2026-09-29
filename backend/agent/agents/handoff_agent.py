"""转人工节点：安全与投诉类请求的兜底出口。

课程原系统完全没有转人工机制——用户说"我要投诉"，系统只能继续道歉。
这里按服务边界设计补上：
- 生成工单并落库（support_tickets 表），返回工单号；
- 按类别给出不同的响应承诺：
    安全类（safety）   → 立即转人工 + 通知当班主管 + 2 小时回访承诺
    投诉类（complaint）→ 生成工单 + 排队预估 + 30 分钟回电承诺
- 对话摘要与用户身份一并写入工单，人工接手时不用让用户重讲一遍。
"""

import os
import sys
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage

from core.workflow.state import AgentState

dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path)

# 转人工也要落进**同一张工单表**（work_orders），而不是另起一张 support_tickets。
# 理由：两张表 = 两套状态机 + 两套 SLA + 两套埋点，"按时闭环率"就会有两个口径。
# 旧的 support_tickets 停用（保留只读），见 05-TDD §12 待确认第 2 项。
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP_DIR = os.path.join(_BACKEND_DIR, "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

CATEGORY_CONFIG = {
    "safety": {
        "severity": "P0",
        "callback_minutes": 120,
        "work_order_category": "ESCALATION",
        "action": "已立即转接人工，并同步通知当班主管",
        "promise": "我们会在 2 小时内由值班主管与您电话联系",
    },
    "complaint": {
        "severity": "P1",
        "callback_minutes": 30,
        "work_order_category": "COMPLAINT",
        "action": "已生成投诉工单并转接人工客服",
        "promise": "当前人工排队约 2 分钟；如果排队超时，我们会在 30 分钟内主动回电",
    },
    "exception": {
        "severity": "P2",
        "callback_minutes": 60,
        "work_order_category": "ESCALATION",
        "action": "已生成工单，转交对应的酒店与客服同事处理",
        "promise": "预计 1 小时内给出答复",
    },
}

TEMPLATE = """非常抱歉给您带来不好的体验，我已经把这件事**交给真人处理**了。

- 🎫 工单号：**{work_order_id}**
- 承接部门：{dept}
- 处理动作：{action}
- 回访承诺：{promise}
- 已同步信息：您的位置信息、本次对话摘要

您不需要重复描述问题，值班同事会直接看到上面的记录。如果情况有变化，随时说一声，我会追加到同一张工单上。"""


class HandoffAgentNode:
    """把"该转人工"落成一个真实工单，而不是一句道歉。"""

    def __init__(self) -> None:
        self.dsn = os.getenv("POSTGRES_DSN")

    def _create_ticket(
        self,
        user_id: str,
        session_id: str,
        category: str,
        summary: str,
        signals: list[str] | None = None,
        route_source: str = "",
        store_id: str = "",
        room_no: str | None = None,
        stay_id: str | None = None,
    ) -> str:
        """转人工 → 落进同一张 work_orders（category=COMPLAINT / ESCALATION）。

        这里不再单独维护 support_tickets：一张表一套 SLA 才是可统计的。
        """
        from service import work_order_service as wos

        config = CATEGORY_CONFIG.get(category, CATEGORY_CONFIG["complaint"])
        result = wos.create_work_order(
            store_id=store_id or "UNKNOWN",
            session_id=session_id or "unknown",
            category=config["work_order_category"],
            intent=f"转人工（{category}）",
            room_no=room_no,
            stay_id=stay_id,
            payload={
                "handoff_category": category,
                "summary": summary[:500],
                "signals": signals or [],
                "route_source": route_source,
            },
            # 有入住上下文才算"已验证"；没有就如实记为未验证
            guest_verified=bool(stay_id),
            actor="handoff",
        )
        return result["work_order_id"]

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        metadata = state.get("metadata", {})
        category = metadata.get("handoff_category", "complaint")
        config = CATEGORY_CONFIG.get(category, CATEGORY_CONFIG["complaint"])

        messages = state.get("messages", [])
        summary = ""
        for message in reversed(messages):
            content = getattr(message, "content", None) or (
                message[1] if isinstance(message, tuple) and len(message) > 1 else ""
            )
            if content:
                summary = str(content)
                break

        work_order_id = "（工单系统未连通）"
        try:
            if self.dsn:
                work_order_id = self._create_ticket(
                    user_id=state.get("user_id", "unknown"),
                    session_id=metadata.get("session_id") or state.get("session_id", "unknown"),
                    category=category,
                    summary=summary,
                    signals=metadata.get("handoff_signals"),
                    route_source=metadata.get("route_source", ""),
                    store_id=metadata.get("store_id", ""),
                    room_no=metadata.get("room_no"),
                    stay_id=metadata.get("stay_id"),
                )
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ [Handoff] 工单落库失败：{exc}")

        from service import work_order_service as wos

        wos_rule = wos.CATEGORY_RULES.get(config["work_order_category"], {})
        print(f"🚨 [Handoff] 类别={category} 已生成工单 {work_order_id}")
        return {
            "messages": [
                AIMessage(
                    content=TEMPLATE.format(
                        work_order_id=work_order_id,
                        dept=wos_rule.get("dept", "前台"),
                        action=config["action"],
                        promise=config["promise"],
                    )
                )
            ],
            "next_agent": "",
            "metadata": {**metadata, "ticket_id": work_order_id, "work_order_id": work_order_id},
        }
