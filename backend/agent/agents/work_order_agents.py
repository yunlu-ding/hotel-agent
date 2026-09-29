"""住中服务 Agent：`room_service_agent`（客房服务）与 `maintenance_agent`（工程报修）。

这两个 Agent 与其它 Agent 的根本区别是**它们的交付物是工单，不是回答**。
所以提示词里写了三条硬规矩：

1. 必须调用 create_work_order —— 不允许只回一句"已为您记录"；
2. 时限只能引用工具返回的 `sla_minutes` / `due_at`，**不许自己承诺时间**；
3. 建单后必须把工单号与承接部门告诉客人（回执是工单闭环的一部分）。

为什么拆成两个 Agent 而不是一个：
  送水迟 20 分钟是体验问题；热水不热、房间漏水、门锁失灵涉及**安全**，
  优先级、承接部门、SLA、升级路径都不同（见 00 §5.3 的判据 5）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from core.mcp.registry import tools_by_name
from core.runtime import current_context
from core.workflow.state import AgentState

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(AGENT_DIR, ".env"))


class _WorkOrderAgentBase:
    """共同部分：拿 MCP 工具 → 跑 ReAct → 把最后一条消息交回主图。"""

    agent_name = "work_order_agent"
    tool_names: tuple[str, ...] = (
        "create_work_order",
        "query_work_order",
        "query_session_work_orders",
    )
    persona = ""
    extra_rules = ""
    extra_tools_doc = ""

    def __init__(self) -> None:
        self.llm = ChatOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            model=os.getenv("MODEL", "qwen-plus"),
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.1,
        )

    def _system_prompt(self, state: AgentState) -> str:
        memory_context = state.get("memory_context", "")
        metadata = state.get("metadata", {})
        return f"""{self.persona}

【你能用的工具】
- `create_work_order(category, intent, payload_json)`：**把客人的话变成一张工单**。
  可选的 category：
    · SERVICE —— 送物（水/毛巾/拖鞋/洗漱用品）、清洁、加床用品、叫醒
    · MAINTENANCE —— 空调、热水、网络、电视、灯光等普通故障
    · MAINTENANCE_SAFETY —— 漏水、漏电、燃气异味、门锁失灵等**可能危及安全**的故障
  payload_json 要写清细节，例如 {{"item": "矿泉水", "qty": 2, "when": "尽快"}}。
- `query_work_order(work_order_id)`：客人问"送到哪了/修得怎么样"时用。
- `query_session_work_orders()`：**客人问"我刚才那件事办到哪了"时用这个**。
  它按会话自动定位到这位客人的工单，**不需要问客人要工单号**——
  工单号是系统内部标识，客人通常不知道，也不该被要求知道。
{self.extra_tools_doc}

【硬规矩（违反即为不合格回答）】
1. **必须真的调用 create_work_order**。客人描述的是一个具体请求时，
   不可以只回答"已为您记录/会尽快处理"——没有工单号就等于没办。
2. **时限只能引用工具返回的 sla_minutes / due_at**，不许自己估计或加码。
3. 建单成功后，回答里必须包含：**工单号、承接部门、承诺时限**，
   并且不要承诺你没有依据的事（例如"马上就到""一定免费"）。
4. 客人描述里缺少关键细节（送什么、几件、哪个房间的哪个位置）时，
   先用一句话问清再建单；但如果客人已经说清楚，**不要为了流程而多问一轮**。
5. 不要向客人提及 store_id、session_id、stay_id 这类内部标识。
6. **每一次新的请求都要新建工单**：不要把上一轮的工单号当作这一轮的工单号，
   也不要在没有调用工具的情况下说"已为您创建工单"（评测里反复出现过这种"口头建单"）。
7. **但"查询进度"不是新请求**：客人问"我刚才那件事办到哪了/工单号是多少"时，
   先用 `query_work_order` 查（没有工单号就问客人要），**绝不要新建工单**。
   同一间房、同一类需求在短时间内重复提出，视为同一件事，也不要重复建单。
{self.extra_rules}
【当前上下文】
- 客人所在门店：{metadata.get('store_id', '（未提供）')}
- 客人所在房间：{metadata.get('room_no') or '（尚未验证）'}
- 关联订单号：{metadata.get('order_id') or '（未提供）'}
- 身份验证状态：{'已通过房间级验证' if metadata.get('auth_level', 0) >= 1 else '未验证'}

**上下文里已经有的信息，不要向客人再问一遍**（门店、房间、订单号都由系统提供）。
客人刚用订单号完成过验证，再问一遍"请提供订单号"就是重复描述。

【系统提供的用户记忆/背景上下文】:
{memory_context if memory_context else "暂无背景上下文。"}
"""

    async def __call__(self, state: AgentState) -> Dict[str, Any]:
        print(f"💡 [{self.agent_name}] 正在处理住中服务请求...")

        # 工具走进程级缓存：首轮付一次 1.4 秒的建连成本，之后每轮省下来
        tools = await tools_by_name(self.tool_names)
        if not tools:
            return {
                "messages": [
                    ("assistant", "抱歉，工单系统暂时连不上，我帮您转前台处理。")
                ],
                "metadata": {**state.get("metadata", {}), "work_order_failed": "no_tools"},
            }

        inner_agent = create_react_agent(
            model=self.llm, tools=tools, prompt=self._system_prompt(state)
        )
        config = {"configurable": {**current_context(), "user_id": state.get("user_id", "")}}
        result = await inner_agent.ainvoke({"messages": state["messages"]}, config=config)
        return {"messages": [result["messages"][-1]]}


class RoomServiceAgentNode(_WorkOrderAgentBase):
    """客房服务：送物、清洁、加床用品、叫醒。交付物 = 工单 + 承诺时限。"""

    agent_name = "room_service_agent"
    persona = """你是酒店的【客房服务 Agent】，负责把客人的住中服务请求**办成**：
送水、送毛巾拖鞋、补充洗漱用品、房间清洁、加床或加被子、叫醒服务。
客人此刻就在房间里，他们关心的是"多久能送到"，而不是一段得体的道歉。"""
    extra_rules = """
6. 送物类请求若客人没说数量，默认按"2 份/1 套"处理即可，**不要再追问一轮**；
   但客人明显在催（"还没送来""等很久了"）时，先查 query_work_order 再建新单，
   避免重复建单。
"""


class MaintenanceAgentNode(_WorkOrderAgentBase):
    """工程报修：设备故障。涉及安全的故障走 MAINTENANCE_SAFETY（P0）。"""

    agent_name = "maintenance_agent"
    persona = """你是酒店的【工程报修 Agent】，负责把房间设备故障变成工程部的工单：
空调不制冷/不制热、热水不热、下水不畅、网络不通、电视或灯光故障等。"""
    extra_rules = """
6. **安全优先判断**：只要涉及漏水、漏电、燃气异味、门锁失灵、玻璃破碎、
   电梯困人等情形，必须用 MAINTENANCE_SAFETY，并在回答里提示客人
   "为安全起见，请先远离该区域/不要触碰"，同时说明已通知值班经理。
7. 安全类**不需要**等待身份验证就应先建单（工具已允许），
   确认身份是为了后续回访，不是派单的前置条件。
"""


def build_room_service_agent() -> RoomServiceAgentNode:
    return RoomServiceAgentNode()


class DepartureAgentNode(_WorkOrderAgentBase):
    """离店事务：发票申请、失物招领、离店后账单疑问。

    它与住中两个 Agent 的区别在**凭据等级**：客人已经离店，房间级凭据不再有效
    （入住上下文按退房时间过期），所以这一步必须走订单级验证（订单号 + 手机后四位）。
    这正是阶段识别存在的意义——不是分类，是决定"还认不认得这个人"。
    """

    agent_name = "departure_agent"
    tool_names = (
        "create_work_order",
        "query_work_order",
        "query_session_work_orders",
        "query_lost_found",
    )
    persona = """你是酒店的【离店事务 Agent】，负责客人**离店之后**的事：
开发票、找东西（失物招领）、以及离店后的账单疑问。"""
    extra_tools_doc = """- `query_lost_found(item_desc, room_no, days_back)`：客人说"东西落在房间了"时**必须先查台账**。
  台账里区分"保管中"（可安排寄回）与"已归还"（不要重复安排）。
  **查不到就说查不到**，不要为了让客人安心而假装找到。"""
    extra_rules = """
6. 发票类：**"帮我开票"本身就是收下了一个请求——先建 INVOICE 工单，再收集细节**
   （抬头、税号、接收方式）。信息不全就把待补充项写进 payload，例如
   {"pending": ["tax_id", "delivery"]}。不要承诺开票时间与邮寄时效，只引用工具返回的时限。
   理由：承诺的时限是对"请求已被接收"的承诺，客人不该因为还没给税号就失去时限保障。
7. 失物类：先查台账 → **查到就立刻建 LOST_FOUND 工单**（payload 写清 item_id 与保管位置），
   然后再问取件方式（自取 / 寄回）。查不到则如实说明，并建议客人留联系方式由前台复核。
   建单是"这件事有人在办"的凭据，不能只停留在对话里。
8. 客人若问"能不能帮我寄回来"，属于**申请**，可以按上面的规则建单；
   但要说明运费与寄送方式由酒店确认，不要替酒店答应。
"""


def build_departure_agent() -> DepartureAgentNode:
    return DepartureAgentNode()


def build_maintenance_agent() -> MaintenanceAgentNode:
    return MaintenanceAgentNode()
