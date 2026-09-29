"""鉴权门：业务 Agent 之前的最后一道闸。

为什么把鉴权做成**独立节点**，而不是继续放在工具层（参考项目的做法）：
参考项目用工具层的身份注入挡住了"查别人的数据"，但它挡不住两件事——
  ① 信息引导类问题（"302 房间的客人叫什么"）；
  ② **度量**：客人问不出自己房间的信息，往往不是安全问题，而是引导流程有问题。
     做成独立节点，才能统计"鉴权拒绝率"与"升级成功率"这两个数字。

本节点的分工（刻意做窄）：
  · 它只回答一个问题——**这次请求需要哪一级凭据，当前会话有没有**；
  · 凭据的收集过程（逐槽位问房间号 → 姓氏 → 离店日）交给 app 侧的会话服务，
    因为那需要跨轮持久化状态，属于会话层职责，不属于图内的一次推理。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from core.auth_policy import AUTH_LEVEL_LABELS, required_level_for
from core.workflow.state import AgentState

# 安全类故障信号：**唯一允许"先派单、后验证"的例外**
# 依据：漏水/漏电/燃气这类情形，延误的代价高于误派的代价。
from agents.intent_router import MAINTENANCE_SAFETY_SIGNALS


def _last_user_message(state: AgentState) -> str:
    for message in reversed(state.get("messages", [])):
        content = getattr(message, "content", None)
        if content is None and isinstance(message, tuple) and len(message) > 1:
            content = message[1]
        if content:
            return str(content)
    return ""


class AuthGuardNode:
    """放行 / 拦截。拦截时只给一句中性说明，具体问什么由会话服务决定。"""

    MAX_IN_TURN_REJECTIONS = 1  # 预留：同一轮内不允许反复重试

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        metadata = dict(state.get("metadata", {}))
        target = metadata.get("target_agent") or state.get("next_agent") or ""
        required = required_level_for(target, metadata.get("stage"))
        current = int(metadata.get("auth_level") or 0)

        metadata["required_auth"] = required
        metadata["auth_passed"] = required <= current

        if required <= current:
            print(
                f"🔓 [AuthGuard] 放行 {target}（需要 {AUTH_LEVEL_LABELS.get(required)}，"
                f"当前 {AUTH_LEVEL_LABELS.get(current)}）"
            )
            return {"next_agent": target, "metadata": metadata}

        # 例外：安全类报修允许"先派单、后验证"。
        # 理由：客人说"房间漏水了"，他先要的是**有人来**，而不是先证明自己是谁。
        query = _last_user_message(state)
        if target == "maintenance_agent" and any(
            word in query for word in MAINTENANCE_SAFETY_SIGNALS
        ):
            print("🚨 [AuthGuard] 安全类故障：跳过鉴权，先派单（guest_verified=False）")
            metadata["auth_passed"] = False
            metadata["safety_override"] = True
            return {"next_agent": target, "metadata": metadata}

        print(
            f"🔒 [AuthGuard] 拦截 {target}：需要 {AUTH_LEVEL_LABELS.get(required)}，"
            f"当前 {AUTH_LEVEL_LABELS.get(current)} → 转凭据收集"
        )
        metadata["auth_required"] = required
        metadata["auth_target_agent"] = target
        return {
            "messages": [
                AIMessage(
                    content="为了您的账户与房间信息安全，这一步需要先确认一下您的住客身份，我马上带您完成。"
                )
            ],
            "next_agent": "",
            "metadata": metadata,
        }


def build_auth_guard() -> AuthGuardNode:
    return AuthGuardNode()
