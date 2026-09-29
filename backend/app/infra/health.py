"""依赖健康检查：把"主链路依赖"和"可选依赖"分开报告。

设计依据（02-问题定义与优先级 · P7：环境降级不可见）：
参考项目的降级是**静默的**——Redis / Neo4j / Milvus 连不上时，
对外表现与正常时一模一样，于是同一套评测在不同机器上会得到不同分数，
而你无法解释"为什么今天变差了"。

这里把依赖分成三类：
    primary   不通即红色报警（知识检索、语义缓存、生成都在这条链上）
    optional  影响体验但不影响结论（Redis 加速、图谱）
    disabled  已被明确关闭（Neo4j，见 03 变动 5）

并额外输出 `degraded_paths`：**当前环境下实际生效的降级路径**，
它会随每次回答一起落进 answer_log，保证"哪次回答是在降级状态下产生的"可追溯。
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any

from dotenv import load_dotenv

# 与 chat_service / observability 保持一致的 .env 位置：backend/agent/.env
ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "agent", ".env"
)
load_dotenv(ENV_FILE, override=False)

DEFAULT_TIMEOUT = 6

# 每次回答都要记录"当时生效的降级路径"，但逐次做网络检查太贵。
# 这里做一个短 TTL 缓存：最多每 5 分钟重新探测一次。
_DEGRADED_CACHE: dict[str, Any] = {"at": 0.0, "paths": [], "overall": None}


def current_degraded_paths(ttl_seconds: int = 300) -> list[str]:
    now = time.time()
    if now - float(_DEGRADED_CACHE["at"]) > ttl_seconds:
        snapshot = collect_health(deep=False)
        _DEGRADED_CACHE.update(
            {"at": now, "paths": snapshot["degraded_paths"], "overall": snapshot["overall"]}
        )
    return list(_DEGRADED_CACHE["paths"])


def current_overall_status() -> str | None:
    return _DEGRADED_CACHE["overall"]


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------
def _check_postgres() -> dict[str, Any]:
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        return {"role": "primary", "status": "down", "detail": "未配置 POSTGRES_DSN"}
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=DEFAULT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute("select current_database(), count(*) from orders")
                db_name, orders = cur.fetchone()
                cur.execute("select count(*) from pg_tables where schemaname = current_schema()")
                tables = cur.fetchone()[0]
        return {
            "role": "primary",
            "status": "ok",
            "detail": f"库={db_name} 表={tables} orders={orders}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"role": "primary", "status": "down", "detail": f"{type(exc).__name__}: {exc}"}


def _check_milvus() -> dict[str, Any]:
    uri = os.getenv("MILVUS_URI") or (
        f"http://{os.getenv('MILVUS_HOST', 'localhost')}:{os.getenv('MILVUS_PORT', '19530')}"
    )
    token = os.getenv("MILVUS_API_KEY") or None
    policy_collection = os.getenv("MILVUS_POLICY_COLLECTION", "hotel_policy_docs")
    cache_collection = os.getenv("QA_CACHE_COLLECTION", "qa_semantic_cache")
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=uri, token=token)
        collections = list(client.list_collections())
        missing = [c for c in (policy_collection, cache_collection) if c not in collections]
        detail = f"{len(collections)} 个集合"
        if missing:
            detail += f"；缺少 {missing}"
        return {
            "role": "primary",
            "status": "ok" if not missing else "degraded",
            "detail": detail,
            "collections": collections,
        }
    except Exception as exc:  # noqa: BLE001
        return {"role": "primary", "status": "down", "detail": f"{type(exc).__name__}: {exc}"}


def _check_dashscope(deep: bool) -> dict[str, Any]:
    """浅检查只看 key 是否存在；深检查会真实调用一次 embedding（1 个短词，成本可忽略）。

    为什么必须能深检查：key 存在 ≠ key 可用。参考项目踩过的坑就是
    "看起来配好了、实际这条路径从来没跑过"。
    """
    key = os.getenv("DASHSCOPE_API_KEY")
    model = os.getenv("MODEL", "qwen-plus")
    if not key:
        return {"role": "primary", "status": "down", "detail": "未配置 DASHSCOPE_API_KEY"}
    if not deep:
        return {"role": "primary", "status": "ok", "detail": f"key 已配置（浅检查），模型={model}"}
    try:
        import httpx

        resp = httpx.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "text-embedding-v2", "input": "健康检查"},
            timeout=20,
        )
        if resp.status_code == 200:
            dim = len(resp.json()["data"][0]["embedding"])
            return {
                "role": "primary",
                "status": "ok",
                "detail": f"embedding 调用成功，维度={dim}，模型={model}",
            }
        return {
            "role": "primary",
            "status": "down",
            "detail": f"HTTP {resp.status_code}: {resp.text[:160]}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"role": "primary", "status": "down", "detail": f"{type(exc).__name__}: {exc}"}


def _check_redis() -> dict[str, Any]:
    url = os.getenv("REDIS_URL")
    if not url:
        return {
            "role": "optional",
            "status": "disabled",
            "detail": "未配置 REDIS_URL，短期记忆走 PostgreSQL（03 变动 6）",
        }
    host, port = "127.0.0.1", 6379
    try:
        host = url.split("//", 1)[-1].split(":")[0].split("/")[0] or host
        port = int(url.rsplit(":", 1)[-1].split("/")[0])
    except Exception:  # noqa: BLE001
        pass
    sock = socket.socket()
    sock.settimeout(2)
    try:
        reachable = sock.connect_ex((host, port)) == 0
    finally:
        sock.close()
    return {
        "role": "optional",
        "status": "ok" if reachable else "down",
        "detail": f"{host}:{port} " + ("可达" if reachable else "不可达，短期记忆自动走 PostgreSQL"),
    }


def _check_neo4j() -> dict[str, Any]:
    enabled = os.getenv("ENABLE_NEO4J", "false").lower() in ("1", "true", "yes")
    if not enabled:
        return {"role": "disabled", "status": "disabled", "detail": "图谱默认关闭（03 变动 5）"}
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    host = uri.split("//", 1)[-1].split(":")[0]
    sock = socket.socket()
    sock.settimeout(2)
    try:
        reachable = sock.connect_ex((host, 7687)) == 0
    finally:
        sock.close()
    return {
        "role": "optional",
        "status": "ok" if reachable else "down",
        "detail": f"{host}:7687 " + ("可达" if reachable else "不可达"),
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def collect_health(deep: bool = False) -> dict[str, Any]:
    checks = {
        "postgresql": _check_postgres(),
        "milvus": _check_milvus(),
        "dashscope": _check_dashscope(deep=deep),
        "redis": _check_redis(),
        "neo4j": _check_neo4j(),
    }

    primary_down = [n for n, c in checks.items() if c["role"] == "primary" and c["status"] != "ok"]
    optional_down = [
        n for n, c in checks.items() if c["role"] == "optional" and c["status"] == "down"
    ]

    degraded_paths: list[str] = []
    if checks["milvus"]["status"] != "ok":
        degraded_paths.append("知识检索 -> 本地文本快照关键词检索")
        degraded_paths.append("语义缓存 -> 整体跳过（不做猜测性命中）")
    if checks["dashscope"]["status"] != "ok":
        degraded_paths.append("生成 -> 不可用（不返回伪回答）")
    if checks["redis"]["status"] == "down":
        degraded_paths.append("短期记忆 -> PostgreSQL")
    if checks["neo4j"]["status"] == "disabled":
        degraded_paths.append("图谱检索 -> 已关闭")

    overall = "down" if primary_down else ("degraded" if optional_down or degraded_paths else "ok")
    return {
        "overall": overall,  # ok / degraded / down
        "deep": deep,
        "primary_down": primary_down,
        "optional_down": optional_down,
        "degraded_paths": degraded_paths,
        "checks": checks,
        "note": "主链路依赖不通时 overall=down（红色报警）；降级路径会随回答落库，保证结论可追溯。",
    }
