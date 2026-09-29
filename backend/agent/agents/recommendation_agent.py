"""
 * 小滴课堂,愿景：让技术不再难学
 * @Remark 有问题联系我【xdclass68】
 * 源码-笔记-技术交流群,官网 https://xdclass.net
"""
import os
import json
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from core.workflow.state import AgentState
from typing import Dict, Any
from langchain_mcp_adapters.client import MultiServerMCPClient
from agents.order_agent import UserIdInjector
from tools.vector_tool import query_policy_docs

class RecommendationAgent:
    """
    智能推荐 Agent：负责根据用户的业务需求（类型、预算、并发等）进行云产品选型与推荐。
    它会调用向量数据库了解产品特性，并结合 MCP 获取真实可用的商品列表。
    """
    def __init__(self):
        dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
        load_dotenv(dotenv_path)

        self.llm = ChatOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            model=os.getenv("MODEL", "qwen-plus"),
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.3, # 推荐场景需要一点灵活性，但不宜过高
        )
        
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'mcp_servers.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            self.servers_config = json.load(f)

    async def __call__(self, state: AgentState) -> Dict[str, Any]:
        memory_context = state.get("memory_context", "")
        config = {"configurable": {"user_id": state.get("user_id", "unknown")}}
        
        # 获取 MCP 工具（用于拉取商品库）
        client = MultiServerMCPClient(
            connections=self.servers_config.get("mcpServers", {}),
            tool_interceptors=[UserIdInjector()]
        )
        all_tools = await client.get_tools()
        # 用 search_hotel_catalog 拉真实在售房型，再用 get_promotion_materials 拿专属链接
        target_tools = ["search_hotel_catalog", "get_promotion_materials"]
        mcp_tools = [t for t in all_tools if t.name in target_tools]
        
        # 组合向量工具与 MCP 工具
        tools = [query_policy_docs] + mcp_tools

        system_prompt = f"""你是酒店预订平台的【酒店推荐Agent】，像一位熟悉各地的旅行顾问。
你的任务是根据用户的需求（城市、日期、预算、出行人群、偏好），推荐最合适的酒店与房型。

【工作流程】
1. 先分析用户需求：城市、入住/离店日期、人数、预算、特殊要求（亲子、海景、可取消、近地铁等）。
   如果信息不足（比如没说城市或日期），先向用户确认，不要凭空猜一个城市。
2. (必须) 调用 `search_hotel_catalog` 获取**真实在售**的酒店与房型，参数用你抽取出的城市/关键词/价格上限。
3. 需要解释政策或权益（能不能取消、会员权益、加床规则）时，调用 `query_policy_docs` 检索政策文档。
4. 精选 1-3 个最合适的房型，每个都说明推荐理由（满足哪条需求、价格是多少、取消政策是什么）。
5. 在推荐结论中，针对最推荐的活动调用 `get_promotion_materials` 拿专属链接并附在回复末尾。

【回答要求】
- 语气专业热情，但不要过度营销。
- 必须包含真实存在的酒店名与房型名，以及每晚价格和取消政策。
- 必须条理清晰（使用列表、加粗）。
- 绝不推荐检索结果里不存在的酒店或房型，绝不编造价格。
- 每次回答结尾，只需列出实际获取到数据的来源，格式如下：
  答案来源：
  - 酒店目录：结构化查询
  （不要输出“未使用”的工具或“可信度”）

【系统提供的用户记忆/背景上下文】:
{memory_context if memory_context else "暂无背景上下文。"}
"""
        inner_agent = create_react_agent(
            model=self.llm,
            tools=tools,
            prompt=system_prompt
        )
        
        print("🔍 [RecommendationAgent] 正在进行智能产品选型与推荐...")
        
        result = await inner_agent.ainvoke(
            {"messages": state["messages"]},
            config=config
        )
        final_message = result["messages"][-1]
        return {"messages": [final_message]}
