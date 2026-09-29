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
from langchain_mcp_adapters.client import MultiServerMCPClient
from typing import Dict, Any

from core.workflow.state import AgentState
from agents.order_agent import UserIdInjector # 复用安全拦截器，防止越权刷单

class PromotionAgentNode:
    """
    推广 Agent：负责处理用户的产品分享、返佣、活动查询和推广物料获取请求。
    所有的工具调用都通过 FastMCP 服务从后端营销系统中获取。
    """
    def __init__(self):
        dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
        load_dotenv(dotenv_path)

        self.llm = ChatOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            model=os.getenv("MODEL", "qwen-plus"),
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.3, # 营销话术可以稍微有一点创造性
        )
        
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'mcp_servers.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            self.servers_config = json.load(f)

    async def _ensure_tools(self):
        pass

    async def __call__(self, state: AgentState) -> Dict[str, Any]:
        config = {"configurable": {"user_id": state.get("user_id", "unknown")}}
        
        # 每次调用重新初始化 MCP 客户端，避免使用不支持的 async with
        client = MultiServerMCPClient(
            connections=self.servers_config.get("mcpServers", {}),
            tool_interceptors=[UserIdInjector()]
        )
        all_tools = await client.get_tools()
        target_tools = ["get_promotable_products", "get_promotion_materials", "generate_ai_poster"]
        tools = [t for t in all_tools if t.name in target_tools]

        memory_context = state.get("memory_context", "")
        
        system_prompt = f"""你是酒店预订平台的【分销推广Agent】。
你的任务是帮助想分享酒店、赚取佣金的用户，拿到专属推广链接与海报。

工作流程：
1. **意图：随便看看/列出可推广的活动**：当用户只说"我想推广酒店""有哪些能赚佣金的""我要做分销"时，
   必须**先调用 `get_promotable_products`** 获取可推广活动列表，按序号展示给用户，
   **此时不要调用取链接和生图的工具**，等用户选择。
2. **意图：明确选择活动**：用户选择后（如"推第 2 个""三亚那个亲子套餐"），用列表里的 `product_id` 调
   `get_promotion_materials` 获取专属链接与卖点。
3. **处理未找到的情况**：如果返回 `not_found`，不要捏造活动，如实告知并推荐列表里的通用活动（如 `P_ALL_000`）。
4. **生成海报**：拿到链接后，再调用 `generate_ai_poster`，根据酒店卖点自己构思一段提示词
   （例如"三亚亚龙湾海景度假酒店，蓝天白云，亲子氛围，竖版推广海报，高级质感"）。
   生图需要几十秒，调用前先告诉用户"正在生成海报，请稍等"。

注意：
- 系统会自动注入用户的 user_id，你在调用 `get_promotion_materials` 时，user_id 参数写个占位符如 "auto" 即可。
- 最终的回答必须包含：
  1. 热情的开场白，提一下该活动对应的酒店与返佣比例。
  2. 酒店核心卖点包装。
  3. 明确的专属推广链接。
  4. 使用 Markdown 图片语法展示通过 `generate_ai_poster` 生成的海报 URL（如 `![专属海报](URL)`）。

【系统提供的用户记忆/背景上下文】:
{memory_context if memory_context else "暂无背景上下文。"}
"""
        inner_agent = create_react_agent(
            model=self.llm,
            tools=tools,
            prompt=system_prompt
        )
        
        print("📢 [PromotionAgent] 正在生成营销与推广物料...")
        
        result = await inner_agent.ainvoke(
            {"messages": state["messages"]}, 
            config=config
        )
        
        final_message = result["messages"][-1]
        return {"messages": [final_message]}
