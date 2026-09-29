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
from agents.order_agent import UserIdInjector

class FinOpsAgentNode:
    """
    FinOps Agent：成本优化与架构诊断专家。
    负责分析用户的资源监控数据，判断是否存在资源浪费，并给出降本增效的建议。
    """
    def __init__(self):
        dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
        load_dotenv(dotenv_path)

        self.llm = ChatOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            model=os.getenv("MODEL", "qwen-plus"),
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.1, 
        )
        
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'mcp_servers.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            self.servers_config = json.load(f)

    async def _ensure_tools(self):
        pass

    async def __call__(self, state: AgentState) -> Dict[str, Any]:
        config = {"configurable": {"user_id": state.get("user_id", "unknown")}}

        client = MultiServerMCPClient(
            connections=self.servers_config.get("mcpServers", {}),
            tool_interceptors=[UserIdInjector()]
        )
        all_tools = await client.get_tools()
        target_tools = ["query_user_orders", "analyze_order_cost"]
        tools = [t for t in all_tools if t.name in target_tools]
        
        system_prompt = f"""你是酒店预订平台的【出行成本优化专家】。
你刚刚接手了上一个 Agent（订单查询）传递过来的上下文。

你的任务：
1. 先读上下文，找出用户想优化的**订单号 (order_id)**。
2. 如果上下文里没有订单号，调用 `query_user_orders` 拿到该用户的订单列表，
   优先选择状态为 PAID 且尚未入住的订单继续分析；有多笔时先列清单，请用户指定。
3. 调用 `analyze_order_cost` 拿到该订单的对比数据与可用优惠券试算结果。
4. 以"帮用户省钱"的口吻给出建议，但必须遵守下面的边界：
   - 只报工具返回的真实数字（实付、每晚均价、同城同星级均价、券能省多少），
     **严禁编造节省金额或折扣**；
   - 只有在 `in_free_cancel_window = true` 时，才可以提"取消后重新下单用券"这条路，
     并且必须同时提示风险：重新下单可能遇到涨价或库存不足；
   - 如果已经超过免费取消期，明确说明取消会产生费用，**不要建议取消重下单**；
   - 优惠券与会员权益的叠加规则以政策为准，拿不准就说"需要人工核实"。
5. 结尾给出一句免责提示：优惠以实际下单时的价格与库存为准。

注意：系统会自动注入 user_id，调用工具时传占位符 "auto" 即可。
- 严禁出现"工具不可用/接口坏了/系统异常"等内部表述，对用户只给业务友好表达。
"""
        inner_agent = create_react_agent(
            model=self.llm,
            tools=tools,
            prompt=system_prompt
        )
        
        print("💡 [FinOpsAgent] 正在接手订单上下文，生成出行成本优化建议...")
        
        result = await inner_agent.ainvoke(
            {"messages": state["messages"]}, 
            config=config
        )
        
        final_message = result["messages"][-1]
        
        # 执行完毕后，把 next_agent 清空，代表流程彻底结束
        return {"messages": [final_message], "next_agent": ""}
