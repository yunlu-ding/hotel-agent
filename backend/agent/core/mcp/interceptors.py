"""MCP 工具调用拦截器：把运行时上下文强制注入工具参数。

这是本项目安全模型的**关键一环**（沿用参考项目的 `ToolCallInterceptor` 思路）：
工具签名里的 `store_id / session_id / room_no / stay_id / user_id` 全部标 `[系统注入]`，
模型传什么都会被这里**覆盖**。

为什么必须这样：如果让模型自己填门店号，它会编出一个"看起来合理"的值，
而 store_id 错了，答案会**以正确的方式答错**——把 A 店的规则给 B 店客人。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult, ToolCallInterceptor

from core.runtime import current_context

# 上下文中可供注入的参数名
INJECTED_KEYS = ("user_id", "store_id", "session_id", "room_no", "stay_id", "order_id")

# 每个工具**接受哪些注入参数**（必须显式声明）。
#
# 为什么不能"全都注入"：拦截器拿不到工具签名（MCPToolCallRequest 只有 name/args/runtime），
# 而把 store_id 塞进一个没有该形参的工具（如 query_user_orders）会直接抛 TypeError。
# 未声明的工具按最小集（user_id）处理，并打一次警告——默认不能是"宽松注入"。
INJECTABLE_BY_TOOL: dict[str, tuple[str, ...]] = {
    "query_user_orders": ("user_id",),
    "query_order_detail": ("user_id",),
    "search_hotel_catalog": ("user_id",),
    "create_work_order": ("user_id", "store_id", "session_id", "room_no", "stay_id", "order_id"),
    "query_work_order": ("store_id",),
    "query_session_work_orders": ("session_id",),
    "query_lost_found": ("store_id",),
    "query_room_state": ("store_id",),
    "create_invoice_request": ("user_id",),
}
DEFAULT_INJECTABLE = ("user_id",)

_WARNED: set[str] = set()


class RuntimeContextInjector(ToolCallInterceptor):
    """从"图调用的 config"里取上下文，覆盖模型给的参数。"""

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
    ) -> MCPToolCallResult:
        context: dict[str, Any] = {}

        # 1) 优先从 LangGraph 的 runtime config 取（与参考项目一致）
        config = getattr(request.runtime, "config", None)
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
            if isinstance(configurable, dict):
                context.update({k: v for k, v in configurable.items() if v})

        # 2) 兜底：调用方没有通过 config 传时，用进程内上下文
        for key, value in current_context().items():
            context.setdefault(key, value)

        keys = INJECTABLE_BY_TOOL.get(request.name)
        if keys is None:
            keys = DEFAULT_INJECTABLE
            if request.name not in _WARNED:
                _WARNED.add(request.name)
                print(f"⚠️ [安全拦截] 工具 {request.name} 未声明可注入参数，按最小集 {keys} 处理")

        injected = {k: context[k] for k in keys if context.get(k)}
        if not injected:
            return await handler(request)

        args = dict(request.args or {})
        overwritten = [k for k in injected if k in args and args[k] != injected[k]]
        args.update(injected)
        print(
            f"🔒 [安全拦截] 注入 {list(injected)} 到工具 {request.name}"
            + (f"（覆盖模型值：{overwritten}）" if overwritten else "")
        )
        return await handler(request.override(args=args))
