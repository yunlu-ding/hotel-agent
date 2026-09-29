"""
 * 小滴课堂,愿景：让技术不再难学
 * @Remark 有问题联系我【xdclass68】
 * 源码-笔记-技术交流群,官网 https://xdclass.net
"""
import os
from typing import Dict, Any
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agents.intent_router import AGENTS, CLARIFY, HANDOFF, IntentRouter
from core.workflow.state import AgentState

class OrchestratorAgent:
    """
    中心路由节点 (Orchestrator/Router)
    负责分析用户意图，并将请求分发给相应的专门 Agent。

    路由分三步（对应"决策 1：规则初判 + 模型复核 + 低置信度转澄清"）：
      1. 规则初判：明确意图直接命中，不调用模型；
      2. 模型复核：规则未命中时由模型判断，并要求输出置信度与候选意图；
      3. 低置信度 → clarify_agent；安全/投诉硬触发 → handoff_agent。
    """
    def __init__(self):
        dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
        load_dotenv(dotenv_path)

        self.intent_router = IntentRouter()

        # 路由节点不需要复杂的工具，只需一个基础大模型来做分类决策
        self.llm = ChatOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            model=os.getenv("MODEL", "qwen-plus"),
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.1,
        )

    async def route(self, state: AgentState) -> Dict[str, Any]:
        """
        根据用户的最新输入，决定路由走向。
        """
        # 获取最新的一条用户消息
        messages = state.get("messages", [])
        if not messages:
            last_message = ""
        else:
            # langgraph 内部有时候会把 tuple 转成实际的 BaseMessage 子类
            last_msg_obj = messages[-1]
            if isinstance(last_msg_obj, tuple):
                last_message = last_msg_obj[1]
            elif hasattr(last_msg_obj, "content"):
                last_message = last_msg_obj.content
            else:
                last_message = str(last_msg_obj)
        memory_context = state.get("memory_context", "")

        decision = await self.intent_router.decide(last_message, memory_context)
        metadata = dict(state.get("metadata", {}))
        metadata.update(
            {
                "route_source": decision.source,
                "route_confidence": decision.confidence,
                "route_reason": decision.reason,
                "route_candidates": decision.candidates,
                "service_tier": decision.tier,
                # 鉴权门要知道"这次本该由谁处理"，才能判断需要哪一级凭据
                "target_agent": decision.intent,
                # 需要多跳时把标记带进状态（见 05-TDD §5.8：v1 只保留延退/换房一条）
                "need_room_state": decision.workflow == "room_state",
            }
        )

        if decision.tier == "handoff":
            metadata["handoff_category"] = decision.handoff_category
            metadata["handoff_signals"] = decision.signals
            print(f"🚨 [Orchestrator] 命中硬触发（{decision.reason}），转人工并生成工单")
            return {"next_agent": HANDOFF, "metadata": metadata}

        if decision.tier in ("clarify", "boundary"):
            metadata["clarify_question"] = decision.clarify_question
            metadata["clarify_kind"] = decision.tier
            label = "无归属（超出服务范围）" if decision.tier == "boundary" else "选项式澄清"
            print(f"❓ [Orchestrator] {decision.reason} → 转{label}")
            return {"next_agent": CLARIFY, "metadata": metadata}

        if decision.guard:
            metadata["guard"] = decision.guard

        print(
            f"🧭 [Orchestrator] 路由至 {decision.intent}"
            f"（来源={decision.source} 置信度={decision.confidence:.2f} 档位={decision.tier}）"
        )
        return {"next_agent": decision.intent, "metadata": metadata}
