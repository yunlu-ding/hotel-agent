"""请求级运行时上下文（身份注入的落点）。

为什么用 contextvars 而不是让模型传参数：
参考项目把身份保护放在**工具层**——工具签名里的 `user_id` 由系统注入，模型无法伪造。
本项目要注入的维度变多了（store_id / room_no / stay_id / session_id / auth_level），
如果让模型自己填，它会（并且已经在别的项目里）编造出"看起来合理"的门店号；
而 store_id 一旦错，答案就会**以正确的方式答错**——把 A 店的早餐时间给 B 店客人。

所以：**凡是决定"看到哪些数据"的参数，一律由上下文注入，不经过模型。**
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_RUNTIME: ContextVar[dict[str, Any]] = ContextVar("hotel_runtime_context", default={})


def set_runtime_context(**kwargs: Any) -> Token:
    """设置本次请求的上下文。返回 token，供 reset 使用。"""
    merged = dict(_RUNTIME.get())
    merged.update({k: v for k, v in kwargs.items() if v is not None})
    return _RUNTIME.set(merged)


def reset_runtime_context(token: Token) -> None:
    _RUNTIME.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_RUNTIME.get())


def current_store_id() -> str:
    """当前门店。缺失时回落默认门店，并**明确标记是回落的**，便于事后审计。"""
    ctx = _RUNTIME.get()
    if ctx.get("store_id"):
        return str(ctx["store_id"])
    try:
        from config import get_settings

        return get_settings().default_store_id
    except Exception:  # noqa: BLE001
        return "H_SH_001"
