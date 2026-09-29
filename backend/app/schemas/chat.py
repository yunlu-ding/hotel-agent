from pydantic import BaseModel
from typing import Optional

class ChatRequest(BaseModel):
    query: str
    user_id: Optional[str] = "user_1001"
    session_id: Optional[str] = "default_session"
    # 客人所在门店。真实场景由扫码链接带入（房间二维码里带 store_id），
    # 这里显式暴露是为了本地验收与评测能指定门店——它决定"看到哪个店的数据"。
    store_id: Optional[str] = None
    # 房间号：同样由扫码带入。注意它是**弱凭据**，只用于定位与阶段判定，
    # 真正的身份确认要走 /api/auth/verify（房号 + 姓氏 + 离店日）。
    room_no: Optional[str] = None

class ChatResponse(BaseModel):
    status: str
    reply: str
    user_id: str
    session_id: str
