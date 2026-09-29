"""MCP 客户端与工具的**进程级复用**。

为什么需要它（实测数据）：每次 `MultiServerMCPClient(...) + get_tools()` 要
**约 1.4 秒**——它会把 MCP server 作为 stdio 子进程拉起来并做一次工具发现。
参考项目的每个 Agent 都在 `__call__` 里新建一次，于是**每一轮对话都付这 1.4 秒**。

优化做法：把 client 与工具列表缓存在模块级，进程内只建一次。
拦截器本身是无状态的（它从 config / contextvars 取上下文），所以可以安全共享。

注意：工具列表是**共享对象**，使用者只能按名字过滤，不要修改它。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from core.mcp.interceptors import RuntimeContextInjector

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(AGENT_DIR, "config", "mcp_servers.json")

_TOOLS: list[BaseTool] | None = None
_LOCK = asyncio.Lock()
_BOOTSTRAP_MS: int | None = None


def _load_servers() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle).get("mcpServers", {})


async def get_mcp_tools() -> list[BaseTool]:
    """返回（并缓存）MCP 工具列表。首次调用会拉起子进程，之后复用。"""
    global _TOOLS, _BOOTSTRAP_MS
    if _TOOLS is not None:
        return _TOOLS
    async with _LOCK:
        if _TOOLS is None:
            import time

            started = time.perf_counter()
            client = MultiServerMCPClient(
                connections=_load_servers(),
                tool_interceptors=[RuntimeContextInjector()],
            )
            tools = await client.get_tools()
            _BOOTSTRAP_MS = int((time.perf_counter() - started) * 1000)
            _TOOLS = tools
            print(f"🔌 [MCP] 工具已就绪并缓存：{len(tools)} 个，首次建立耗时 {_BOOTSTRAP_MS} ms")
    return _TOOLS


async def tools_by_name(names: tuple[str, ...]) -> list[BaseTool]:
    """按名字取工具子集（不修改共享列表）。"""
    wanted = set(names)
    return [tool for tool in await get_mcp_tools() if tool.name in wanted]


def bootstrap_ms() -> int | None:
    """首次建立 MCP 连接的耗时（用于性能说明与自检展示）。"""
    return _BOOTSTRAP_MS
