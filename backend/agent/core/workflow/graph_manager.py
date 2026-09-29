import os
import sys
from pathlib import Path
# 这里没有 sys.path.insert！我们期望调用者 (main.py) 正确设置 sys.path。

import asyncio
from typing import Literal

from langgraph.graph import StateGraph, START, END
from core.workflow.state import AgentState
from agents.orchestrator import OrchestratorAgent
from agents.clarify_agent import ClarifyAgentNode
from agents.handoff_agent import HandoffAgentNode
from agents.policy_agent import PolicyAgentNode
from agents.order_agent import OrderAgentNode
from agents.recommendation_agent import RecommendationAgent
from agents.auth_guard import AuthGuardNode
from agents.room_state_agent import RoomStateNode
from agents.work_order_agents import (
    DepartureAgentNode,
    MaintenanceAgentNode,
    RoomServiceAgentNode,
)

class AgentGraphManager:
    """
    负责组装 LangGraph 多 Agent 编排。

    节点分三层：
      1. 控制层：orchestrator（路由）→ auth_guard（鉴权门）
      2. 业务层：policy / order / recommendation / room_service / maintenance / departure
      3. 出口层：clarify（澄清）/ handoff（转人工）

    已摘除（代码保留、不接入图）：promotion_agent（B 端分销）、finops_agent（比价省钱）——
    它们服务的是推广者与比价场景，不属于住客客服，见 00 §5.4。
    """
    def __init__(self):
        self.orchestrator = OrchestratorAgent()
        self.clarify_node = ClarifyAgentNode()
        self.handoff_node = HandoffAgentNode()
        self.policy_node = PolicyAgentNode()
        self.order_node = OrderAgentNode()
        self.recommendation_node = RecommendationAgent()
        # 阶段二新增：鉴权门 + 住中两个服务 Agent（交付物是工单，不是回答）
        self.auth_guard_node = AuthGuardNode()
        self.room_service_node = RoomServiceAgentNode()
        self.maintenance_node = MaintenanceAgentNode()
        self.departure_node = DepartureAgentNode()
        self.room_state_node = RoomStateNode()

    def _route_condition(self, state: AgentState) -> str:
        """根据 Orchestrator 的决策决定走向哪个 Agent 节点。"""
        return state.get("next_agent", "policy_agent")
    
    def _clarify_or_handoff_condition(self, state: AgentState) -> str:
        """澄清与转人工属于"非业务出口"，走完即结束。"""
        return state.get("next_agent") or END

    def _auth_route(self, state: AgentState) -> str:
        """鉴权门之后的走向：放行则回到目标 Agent，被拦则结束本节（由会话层收集凭据）。"""
        return state.get("next_agent") or END

    def _need_room_state(self, state: AgentState) -> str:
        """order_agent 之后是否要跳去查房态。

        三个条件同时满足才跳：① 本轮被标记需要房态；② 还没取到；③ 跳数未超上限。
        第 ③ 条是防环——多跳链路最大的风险不是查不到，而是**转不出来**。
        """
        metadata = state.get("metadata", {})
        if not metadata.get("need_room_state"):
            return END
        if metadata.get("room_state") or metadata.get("room_state_error"):
            return END
        hop_max = int(os.getenv("HOP_MAX", "2"))
        if int(metadata.get("hop_count") or 0) >= hop_max:
            print(f"⚠️ [Graph] 已达多跳上限 {hop_max}，停止跳转")
            return END
        return "room_state_node"

    def build_graph(self) -> StateGraph:
        """构建状态图"""
        builder = StateGraph(AgentState)

        # 1. 添加节点
        builder.add_node("orchestrator", self.orchestrator.route)
        builder.add_node("clarify_agent", self.clarify_node)
        builder.add_node("handoff_agent", self.handoff_node)
        builder.add_node("policy_agent", self.policy_node)
        builder.add_node("order_agent", self.order_node)
        builder.add_node("recommendation_agent", self.recommendation_node)
        builder.add_node("room_service_agent", self.room_service_node)
        builder.add_node("maintenance_agent", self.maintenance_node)
        builder.add_node("departure_agent", self.departure_node)
        builder.add_node("auth_guard", self.auth_guard_node)
        builder.add_node("room_state_node", self.room_state_node)

        # 2. 定义边
        builder.add_edge(START, "orchestrator")

        # Orchestrator 之后：业务 Agent 一律先过鉴权门；澄清与转人工直接走（不做鉴权拦截）
        builder.add_conditional_edges(
            "orchestrator",
            self._route_condition,
            {
                "policy_agent": "auth_guard",
                "order_agent": "auth_guard",
                "recommendation_agent": "auth_guard",
                "room_service_agent": "auth_guard",
                "maintenance_agent": "auth_guard",
                "departure_agent": "auth_guard",
                # 规则硬触发 → 直接转人工；置信度不足 → 先澄清再路由
                "handoff_agent": "handoff_agent",
                "clarify_agent": "clarify_agent",
            }
        )

        # 鉴权门之后的走向（放行 = 回到目标 Agent；拦截 = 结束本节，交给会话层收凭据）
        builder.add_conditional_edges(
            "auth_guard",
            self._auth_route,
            {
                "policy_agent": "policy_agent",
                "order_agent": "order_agent",
                "recommendation_agent": "recommendation_agent",
                "room_service_agent": "room_service_agent",
                "maintenance_agent": "maintenance_agent",
                "departure_agent": "departure_agent",
                END: END,
            },
        )

        # 3. 多跳链路：order_agent ⇄ room_state_node（v1 唯一一条，见 05-TDD §5.8）
        #    条件：需要房态、且还没取到 → 去查房态；查完回到 order_agent 出结论与建单
        builder.add_conditional_edges(
            "order_agent",
            self._need_room_state,
            {"room_state_node": "room_state_node", END: END},
        )
        builder.add_edge("room_state_node", "order_agent")

        # 4. 其余子 Agent 执行完毕后，流程结束
        builder.add_edge("policy_agent", END)
        builder.add_edge("recommendation_agent", END)
        builder.add_edge("clarify_agent", END)
        builder.add_edge("handoff_agent", END)
        builder.add_edge("room_service_agent", END)
        builder.add_edge("maintenance_agent", END)
        builder.add_edge("departure_agent", END)

        return builder.compile()

async def test_graph():
    manager = AgentGraphManager()
    graph = manager.build_graph()

    print("🚀 正在启动云平台智能客服系统 (Multi-Agent 编排模式)...")
    print("="*60)
    
    # 模拟第一轮对话
    state: AgentState = {
        "messages": [("user", "什么是VPC？")],
        "user_id": "user_1001",
        "session_id": "test_session_1",
        "memory_context": "",
        "next_agent": "",
        "metadata": {}
    }
    print(f"👤 用户: {state['messages'][0][1]}")
    
    result = await graph.ainvoke(state)
    print(f"🤖 AI: {result['messages'][-1].content}\n")

    # 模拟第二轮对话，测试路由
    state["messages"] = result["messages"]
    state["messages"].append(("user", "那帮我查一下我最近买了哪些机器？"))
    
    print(f"👤 用户: {state['messages'][-1][1]}")
    result = await graph.ainvoke(state)
    print(f"🤖 AI: {result['messages'][-1].content}\n")

if __name__ == "__main__":
    asyncio.run(test_graph())
