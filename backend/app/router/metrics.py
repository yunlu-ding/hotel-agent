"""只读指标接口：把 answer_log / support_tickets / cache_candidates 汇总给前端看板。

设计意图：产品经理拿出去演示时，能直接说"我不只有功能，还有数据"。
接口只读，不写任何业务数据。
"""

import asyncio
import os
import re

import psycopg
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException

from app_config.settings import settings

ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "agent", ".env"
)
load_dotenv(ENV_FILE, override=False)

router = APIRouter()


def _work_order_metrics(hours: int, store_id: str | None) -> dict:
    """住中闭环口径（主北极星）——单独算，因为它与对话日志是两条数据源。"""
    try:
        from service import work_order_service as wos

        return wos.metrics(store_id=store_id, hours=hours or None)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _auth_stats(store_id: str | None) -> dict:
    """鉴权口径：通过率与失败率必须一起看（失败率高可能是引导没说清）。"""
    try:
        from service import session_service as ss

        return ss.auth_stats(store_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _collect(hours: int, store_id: str | None = None) -> dict:
    where = f"created_at > now() - interval '{hours} hours'" if hours else "TRUE"
    if store_id:
        # where 是拼进 SQL 的，所以先做白名单校验（不要相信 query 参数）
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", store_id):
            raise HTTPException(status_code=400, detail="store_id 格式非法")
        where += f" AND store_id = '{store_id}'"
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM answer_log WHERE {where}")
        total = cur.fetchone()[0] or 0

        def count(condition: str) -> int:
            cur.execute(f"SELECT count(*) FROM answer_log WHERE {where} AND {condition}")
            return cur.fetchone()[0]

        result = {
            "hours": hours,
            "total": total,
            "cache_hits": count("cache_hit"),
            "clarify": count("tier = 'clarify'"),
            "boundary": count("tier = 'boundary'"),
            "handoff": count("tier = 'handoff'"),
            "sources": {},
            "routes": {},
            "latency_p50": None,
            "latency_p90": None,
            "tickets": {},
            "cache_candidates": [],
            # 住中闭环与鉴权：即使没有对话记录，也要能看到
            "work_orders": _work_order_metrics(hours, store_id),
            "auth": _auth_stats(store_id),
        }
        if not total:
            return result

        cur.execute(
            f"SELECT coalesce(route_source, '未知'), count(*) FROM answer_log WHERE {where} "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        result["sources"] = {key: value for key, value in cur.fetchall()}

        cur.execute(
            f"SELECT coalesce(route, '未知'), count(*) FROM answer_log WHERE {where} "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        result["routes"] = {key: value for key, value in cur.fetchall()}

        cur.execute(
            "SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms), "
            f"percentile_disc(0.9) WITHIN GROUP (ORDER BY latency_ms) FROM answer_log WHERE {where}"
        )
        result["latency_p50"], result["latency_p90"] = cur.fetchone()

        cur.execute(
            "SELECT count(*), "
            "count(*) FILTER (WHERE review_status = 'PENDING'), "
            "count(*) FILTER (WHERE review_status = 'OVERTURNED') FROM support_tickets"
        )
        tickets, pending, overturned = cur.fetchone()
        result["tickets"] = {
            "total": tickets,
            "pending_review": pending,
            "overturned": overturned,
            "misroute_rate": round(overturned / tickets, 4) if tickets else None,
        }

        cur.execute(
            "SELECT sample_query, hit_count FROM cache_candidates WHERE status = 'PENDING' "
            "ORDER BY hit_count DESC LIMIT 5"
        )
        result["cache_candidates"] = [
            {"query": query, "count": count_} for query, count_ in cur.fetchall()
        ]
        return result


@router.get("/metrics")
async def metrics(hours: int = 0, store_id: str | None = None):
    """返回指标看板数据；hours=0 表示全部时间。"""
    if not settings.postgres_dsn:
        return {"error": "未配置 POSTGRES_DSN"}
    return await asyncio.to_thread(_collect, hours, store_id)
