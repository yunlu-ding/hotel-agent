"""后台定时任务：让"承诺的兜底"自己发生。

两个任务，都是 04-PRD 里明确承诺过、但此前只有函数没有触发的：

  1. **超时工单升级**（`sweep_overdue`）：超过承诺时限还没完成的工单 → ESCALATED + 通知值班经理。
     没有它，"承诺时限"只是写在回答里的一句话。
  2. **退房即失效**（`expire_checkouts`）：清掉已过期的入住上下文与其上的身份授权。
     没有它，客人退房后房间级凭据仍然有效——这是隐私红线（03 §3.2）。

为什么用进程内 asyncio 任务而不是独立的调度器：
  本地演示形态只需要"服务在跑，兜底就在跑"。任务本身是幂等的，
  重复执行只会把同样的工单再扫一遍（不会重复升级，因为状态已经不是待处理）。
  真实上线应换成任务计划程序或分布式调度——届时这两个函数可以原样复用。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

_task: asyncio.Task | None = None
_state: dict[str, Any] = {"running": False, "last_run_at": None, "last_result": None, "runs": 0}


def interval_seconds() -> int:
    try:
        return max(10, int(os.getenv("SWEEP_INTERVAL_SECONDS", "60")))
    except Exception:  # noqa: BLE001
        return 60


def status() -> dict[str, Any]:
    return {**_state, "interval_seconds": interval_seconds()}


async def _run_once() -> dict[str, Any]:
    from service import session_service, work_order_service

    escalated = await asyncio.to_thread(work_order_service.sweep_overdue)
    expired = await asyncio.to_thread(session_service.expire_checkouts)
    return {"escalated": len(escalated or []), "expired_stays": expired}


async def _loop() -> None:
    _state["running"] = True
    print(f"⏱️ [scheduler] 已启动：每 {interval_seconds()} 秒巡检一次（超时升级 + 退房清理）")
    while True:
        try:
            await asyncio.sleep(interval_seconds())
            result = await _run_once()
            _state.update(last_run_at=time.strftime("%Y-%m-%d %H:%M:%S"), last_result=result)
            _state["runs"] = int(_state.get("runs") or 0) + 1
            if result["escalated"] or result["expired_stays"]:
                print(
                    f"⏱️ [scheduler] 超时升级 {result['escalated']} 张工单；"
                    f"清理过期入住上下文 {result['expired_stays']} 条"
                )
        except asyncio.CancelledError:
            print("⏱️ [scheduler] 已停止")
            _state["running"] = False
            raise
        except Exception as exc:  # noqa: BLE001
            # 定时任务出错不能把服务带崩，也不能静默——打出来，下一轮继续
            print(f"⚠️ [scheduler] 巡检失败（下一轮重试）：{type(exc).__name__}: {exc}")


def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    _state["running"] = False
