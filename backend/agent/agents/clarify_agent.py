"""澄清节点：意图不确定时不硬答，先做**选项式澄清**。

为什么不开放式提问：
开放式提问（"请问您想咨询什么？"）的回复率低，用户容易直接离开；
选项式澄清把候选意图直接摆出来，用户回一个序号就行，
而用户的选择本身就是一次**高置信度的意图信号**，可以直接拿去路由。
"""

from typing import Any

from langchain_core.messages import AIMessage

from core.workflow.state import AgentState

DEFAULT_QUESTION = """为了给您准确的答案，想先确认一下您要办的是哪件事：

1️⃣ 查询我的订单（状态、金额、入住日期）
2️⃣ 了解退改与取消的政策规则
3️⃣ 推荐酒店与房型
4️⃣ 其他（请补充一句描述）

回复序号即可，我立刻为您处理。"""


class ClarifyAgentNode:
    """把"我需要澄清"变成一句可回答的话。"""

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        metadata = state.get("metadata", {})
        question = metadata.get("clarify_question") or DEFAULT_QUESTION
        print(f"❓ [Clarify] 意图不确定，转选项式澄清（原因：{metadata.get('route_reason', '未说明')}）")
        return {
            "messages": [AIMessage(content=question)],
            "next_agent": "",
            "metadata": {**metadata, "clarified": True},
        }
