"""会话与鉴权接口。

为什么把它们单独拆出来，而不是都塞进 /api/chat：
  · 会话建立是**扫码那一下**发生的事（带入 store_id / room_no），和对话不是同一时刻；
  · 鉴权是可以被单独调用的能力（前端表单、坐席台核实、评测脚本都会用）。
两件事共用一条隐式的"对话里逐槽位收集"，但接口边界要分开。
"""

import asyncio
import uuid

from fastapi import APIRouter, HTTPException

from schemas.session import SessionRequest, VerifyRequest
from service import session_service

router = APIRouter()


@router.post("/session")
async def create_session(request: SessionRequest):
    session_id = request.session_id or f"S{uuid.uuid4().hex[:16]}"
    info = await asyncio.to_thread(
        session_service.get_or_create_session,
        session_id,
        store_id=request.store_id,
        room_no=request.room_no,
        order_id=request.order_id,
        channel=request.channel or "qr_web",
    )
    return {
        "session_id": info["session_id"],
        "store_id": info["store_id"],
        "room_no": info["room_no"],
        "stage": info["stage"],
        "auth_level": info["auth_level"],
        "need_auth": info["auth_level"] == 0,
        "note": "扫码只代表定位到门店与房间，不代表身份已验证；房间级授权需要房号+姓氏+离店日。",
    }


@router.post("/auth/verify")
async def verify(request: VerifyRequest):
    """一次性提交凭据。

    房间级：room_no + last_name + check_out_date
    订单级：order_id + phone_last4
    """
    if request.room_no and request.last_name and request.check_out_date:
        store = request.store_id
        if not store:
            raise HTTPException(status_code=400, detail="房间级验证需要 store_id")
        result = await asyncio.to_thread(
            session_service.verify_stay,
            session_id=request.session_id,
            store_id=store,
            room_no=request.room_no,
            last_name=request.last_name,
            check_out_date=request.check_out_date,
        )
        return result

    if request.order_id and request.phone_last4:
        return await asyncio.to_thread(
            session_service.verify_order,
            session_id=request.session_id,
            order_id=request.order_id,
            phone_last4=request.phone_last4,
        )

    raise HTTPException(
        status_code=400,
        detail="请提供完整凭据：房间级（room_no + last_name + check_out_date）或订单级（order_id + phone_last4）",
    )


@router.get("/auth/stats")
async def auth_stats(store_id: str | None = None):
    """鉴权口径：通过率必须与误挡率一起看（05-TDD §5.2）。"""
    return await asyncio.to_thread(session_service.auth_stats, store_id)
