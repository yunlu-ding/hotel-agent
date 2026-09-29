from typing import Optional

from pydantic import BaseModel


class SessionRequest(BaseModel):
    """扫码建立会话：房间二维码里带 store_id 与 room_no。"""

    store_id: str
    room_no: Optional[str] = None
    order_id: Optional[str] = None
    session_id: Optional[str] = None       # 不传则由服务端生成
    channel: Optional[str] = "qr_web"
    user_id: Optional[str] = None


class VerifyRequest(BaseModel):
    """一次性提交凭据（也支持逐槽位收集，见 /api/chat）。"""

    session_id: str
    store_id: Optional[str] = None
    room_no: Optional[str] = None
    last_name: Optional[str] = None
    check_out_date: Optional[str] = None
    order_id: Optional[str] = None
    phone_last4: Optional[str] = None
