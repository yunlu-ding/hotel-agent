"""鉴权策略：**唯一事实来源**（05-TDD §5.6 权限矩阵）。

放在 agent 包而不是 app 包，是因为它同时被两边使用：
  · agent 侧：`auth_guard` 节点用它决定"这次请求要不要拦"；
  · app 侧：会话与鉴权服务用它决定"该收哪一级凭据"。
如果两处各写一份，就会出现"门卫按 A 标准拦人、服务按 B 标准发证"的漏洞。
"""

from __future__ import annotations

# 意图/Agent → 所需凭据等级
AUTH_REQUIRED_BY_AGENT: dict[str, int] = {
    "policy_agent": 0,          # 门店事实与集团政策是公开信息
    "recommendation_agent": 0,  # 房型推荐也是公开信息
    "room_service_agent": 1,    # 住中服务：房间级
    "maintenance_agent": 1,     # 报修：房间级
    "order_agent": 2,           # 订单与账务：订单级
    "departure_agent": 2,       # 发票/失物：订单级
    "clarify_agent": 0,
    "handoff_agent": 0,         # 求助不做鉴权拦截：情绪激动时验证身份会火上浇油
}

AUTH_LEVEL_LABELS = {0: "公开信息", 1: "房间级", 2: "订单级"}

# 连续失败达到这个次数就转前台（防房号枚举）
MAX_AUTH_ATTEMPTS = 3


def required_level_for(agent: str | None, stage: str | None = None) -> int:
    """所需凭据等级。

    一条重要的下调规则：**入住中的客人查自己的订单，房间级验证就够了**。
    因为"能说出房号+姓氏+离店日"已经证明了他就是这间房的住客，
    再让他翻订单号查手机号后四位，是典型的过度鉴权（会直接把客人挡在门外）。
    离开入住中（入住前 / 离店后），才需要订单级凭据。
    """
    base = AUTH_REQUIRED_BY_AGENT.get(agent or "", 0)
    if base == 2 and stage == "IN":
        return 1
    return base
