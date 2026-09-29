import sys
import os

# 将 agent 目录加入 sys.path
AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
sys.path.insert(0, AGENT_DIR)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from router import chat, health, metrics, session, work_orders
from service import scheduler
from service.chat_service import init_agent_system

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化。
    # 注意：初始化失败**不能**让服务起不来——否则依赖不可用时连 /api/health 都打不开，
    # 而"依赖不可用"恰好是本项目要显式观测的情况（对应 02 问题 P7）。
    try:
        await init_agent_system()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ Agent 系统初始化失败（服务仍会启动，请查看 /api/health）：{exc}")
    # 后台定时任务：超时工单升级 + 退房即失效（见 service/scheduler.py 顶部说明）
    scheduler.start()
    yield
    await scheduler.stop()

app = FastAPI(title="随行管家 · 酒店住客全旅程智能服务台 API", lifespan=lifespan)

# 配置跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")
app.include_router(health.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(work_orders.router, prefix="/api")


@app.get("/")
async def root():
    """服务自述：一眼看清这是什么服务、有哪些入口。"""
    return {
        "service": "随行管家 · 酒店住客全旅程智能服务台",
        "stage": "阶段一 · 基线",
        "endpoints": [
            "GET  /api/health        依赖自检（?deep=1 做真实调用）",
            "POST /api/chat          SSE 流式问答",
            "GET  /api/metrics       指标看板",
            "GET  /api/docs          OpenAPI 文档",
        ],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app_main:app", host="0.0.0.0", port=5000, reload=True)
