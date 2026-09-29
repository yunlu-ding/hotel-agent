"""依赖自检接口。

用法：
    GET /api/health          浅检查（不产生外部调用费用）
    GET /api/health?deep=1   深检查（真实调用一次 embedding，验证 key 与网络真的可用）
"""

import asyncio

from fastapi import APIRouter

from infra.health import collect_health

router = APIRouter()


@router.get("/health")
async def health(deep: int = 0):
    return await asyncio.to_thread(collect_health, bool(deep))
