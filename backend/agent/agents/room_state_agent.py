"""`room_state_node`：多跳链路的第二跳（v1 唯一一条，见 05-TDD §5.8）。

它**不是**一个"专家 Agent"，而是一个**确定性步骤**：
不入模、不做判断，只做一件事——把房态取回来写进状态，然后把接力棒交回订单 Agent。

为什么要有这样一个节点，而不是让订单 Agent 自己调工具：
  1. 房态属于**门店数据**，订单 Agent 的权限域是"这位客人的订单"，
     两件事混在一个提示词里，模型很容易用订单里的信息去推断房态（而它推不出来）；
  2. 图上明明白白一条边，跨 Agent 协同才是可观测、可评测的
     （评测里直接断言"这一轮经过了 room_state_node"）。

防环：每次经过把 `hop_count` 加一，超过 HOP_MAX（默认 2）就不再跳，
避免"订单 → 房态 → 订单 → 房态…"这种死循环。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from core.mcp.registry import tools_by_name
from core.runtime import current_context
from core.workflow.state import AgentState


class RoomStateNode:
    """取房态 → 写状态 → 回到订单 Agent。"""

    async def __call__(self, state: AgentState) -> Dict[str, Any]:
        metadata = dict(state.get("metadata", {}))
        hops = int(metadata.get("hop_count") or 0) + 1
        metadata["hop_count"] = hops

        room_no = metadata.get("room_no") or ""
        target_date = metadata.get("requested_date") or ""
        print(f"🔁 [RoomState] 第 {hops} 跳：查房态（房间={room_no or '未提供'} 目标日={target_date or '次日'}）")

        tools = await tools_by_name(("query_room_state",))
        if not tools:
            metadata["room_state_error"] = "房态工具不可用"
            return {"next_agent": "order_agent", "metadata": metadata}

        try:
            raw = await tools[0].ainvoke({"room_no": room_no, "target_date": target_date})
            payload = _as_payload(raw)
            if isinstance(payload, dict) and payload.get("status") == "success":
                metadata["room_state"] = payload.get("data", {})
                print(
                    "✅ [RoomState] 结论："
                    + str(metadata["room_state"].get("conclusion", ""))[:60]
                )
            else:
                metadata["room_state_error"] = str(payload)[:200]
        except Exception as exc:  # noqa: BLE001
            # 房态查不到不能让整条链路断掉：降级为"需前台确认"，并留下原因
            metadata["room_state_error"] = f"{type(exc).__name__}: {exc}"
            print(f"⚠️ [RoomState] 查询失败（降级为需前台确认）：{exc}")

        # 交回订单 Agent 做最终决策与建单；它看到 room_state 后不会再触发这一跳
        metadata["need_room_state"] = False
        return {"next_agent": "order_agent", "metadata": metadata}


def _as_payload(raw: Any) -> dict:
    """把 MCP 工具的返回值规整成 dict。

    **这里踩过一次**：通过 langchain-mcp-adapters 调用 MCP 工具时，
    `.ainvoke()` 返回的不是字符串，而是**内容块列表**：
        [{"type": "text", "text": "{\\"status\\": \\"success\\", ...}"}]
    我原先把非字符串一律当成错误，结果"房态明明查到了却被记成查询失败"，
    而且模型只会回一句"视当日房态而定"——看起来很像它偷懒，其实是我们的解析错了。
    """
    if isinstance(raw, list):
        raw = "".join(
            block.get("text", "") for block in raw if isinstance(block, dict)
        )
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"status": "error", "message": raw[:200]}
    return raw if isinstance(raw, dict) else {"status": "error", "message": str(raw)[:200]}


def build_room_state_node() -> RoomStateNode:
    return RoomStateNode()
