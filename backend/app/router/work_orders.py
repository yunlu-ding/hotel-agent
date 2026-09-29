"""坐席台接口：工单池 + 状态流转 + 会话时间线。

设计意图：AI 不是替代一线员工，而是**不让他们被重复的事打扰**。
所以坐席台要回答两个问题：
  1) 现在有哪些事等着人办、哪件快超时？（工单池按 due_at 排序）
  2) 客人刚才说了什么？（会话时间线，人工接管时不用让客人再讲一遍）
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from service import work_order_service as wos

router = APIRouter()


class TransitionRequest(BaseModel):
    actor: str = "staff"
    note: Optional[str] = None
    assignee_name: Optional[str] = None


@router.get("/staff/work-orders")
async def list_work_orders(
    store_id: Optional[str] = None,
    status: Optional[str] = None,
    dept: Optional[str] = None,
    limit: int = 50,
):
    rows = await asyncio.to_thread(wos.list_work_orders, store_id, status, dept, limit)
    return {"count": len(rows), "items": rows}


@router.get("/staff/work-orders/{work_order_id}")
async def get_work_order(work_order_id: str):
    detail = await asyncio.to_thread(wos.get_work_order, work_order_id)
    if not detail:
        raise HTTPException(status_code=404, detail="工单不存在")
    return detail


@router.post("/staff/work-orders/{work_order_id}/accept")
async def accept(work_order_id: str, body: TransitionRequest):
    return await _transition(work_order_id, "accept", body)


@router.post("/staff/work-orders/{work_order_id}/done")
async def done(work_order_id: str, body: TransitionRequest):
    return await _transition(work_order_id, "done", body)


@router.post("/staff/work-orders/{work_order_id}/fail")
async def fail(work_order_id: str, body: TransitionRequest):
    if not body.note:
        raise HTTPException(status_code=400, detail="失败必须填写原因（失败原因本身就是评测数据）")
    return await _transition(work_order_id, "fail", body)


@router.post("/staff/work-orders/{work_order_id}/escalate")
async def escalate(work_order_id: str, body: TransitionRequest):
    return await _transition(work_order_id, "escalate", body)


@router.post("/staff/work-orders/{work_order_id}/notify")
async def notify(work_order_id: str, body: TransitionRequest):
    """记录"已给客人回执"。DONE 但没有回执，在统计里算一类失败。"""
    try:
        return await asyncio.to_thread(wos.notify_guest, work_order_id, body.actor)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _transition(work_order_id: str, action: str, body: TransitionRequest):
    try:
        return await asyncio.to_thread(
            wos.transition,
            work_order_id,
            action,
            actor=body.actor,
            note=body.note,
            assignee_name=body.assignee_name,
        )
    except ValueError as exc:
        # 非法流转（例如 DONE 之后再 accept）返回 409：这是业务冲突，不是请求格式错误
        raise HTTPException(status_code=409, detail=str(exc)) from exc
