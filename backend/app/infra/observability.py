"""埋点与治理表（建议 4/5/6 的落地）。

三张表各管一件事：
1. answer_log            每条回答一行：路由来源、置信度、档位、是否命中缓存、耗时、工单号
                         ——没有它，漏转率/误转率/缓存错误率都只是猜测；
2. cache_candidates      "没被缓存覆盖但反复被问到"的问题池（只登记，不自动回写）；
3. support_tickets       在 handoff_agent 里创建，这里补上复核字段（可撤销 = 能算误转率）。

设计原则：所有写入都是 best-effort，埋点失败绝不影响用户拿到回答。
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "agent", ".env"),
    override=False,
)

DDL = [
    """
    CREATE TABLE IF NOT EXISTS answer_log (
        id            BIGSERIAL PRIMARY KEY,
        session_id    VARCHAR(128),
        user_id       VARCHAR(64),
        query         TEXT,
        route         VARCHAR(32),
        route_source  VARCHAR(16),
        confidence    NUMERIC(4,2),
        tier          VARCHAR(16),
        cache_hit     BOOLEAN,
        cache_level   VARCHAR(16),
        latency_ms    INTEGER,
        ticket_id     VARCHAR(32),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_candidates (
        query_norm   VARCHAR(2048) PRIMARY KEY,
        sample_query TEXT        NOT NULL,
        hit_count    INTEGER     NOT NULL DEFAULT 1,
        status       VARCHAR(16) NOT NULL DEFAULT 'PENDING',
        first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    ALTER TABLE support_tickets
        ADD COLUMN IF NOT EXISTS trigger_signals TEXT,
        ADD COLUMN IF NOT EXISTS route_source VARCHAR(16),
        ADD COLUMN IF NOT EXISTS review_status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
        ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS review_note TEXT
    """,
]


def _connect():
    import psycopg

    return psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True, connect_timeout=5)


def ensure_tables() -> None:
    with _connect() as conn, conn.cursor() as cur:
        for statement in DDL:
            cur.execute(statement)


def record_answer(payload: dict[str, Any]) -> None:
    """记录一条回答。任何异常都吞掉——埋点不能影响用户体验。"""
    try:
        import json as _json

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO answer_log
                    (session_id, user_id, query, route, route_source, confidence, tier,
                     cache_hit, cache_level, latency_ms, ticket_id,
                     store_id, stage, required_auth, auth_passed, degraded_paths, work_order_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    payload.get("session_id"),
                    payload.get("user_id"),
                    (payload.get("query") or "")[:2000],
                    payload.get("route"),
                    payload.get("route_source"),
                    payload.get("confidence"),
                    payload.get("tier"),
                    payload.get("cache_hit"),
                    payload.get("cache_level"),
                    payload.get("latency_ms"),
                    payload.get("ticket_id"),
                    payload.get("store_id"),
                    payload.get("stage"),
                    payload.get("required_auth"),
                    payload.get("auth_passed"),
                    _json.dumps(payload.get("degraded_paths") or [], ensure_ascii=False),
                    payload.get("work_order_id"),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 埋点写入失败（不影响回答）：{exc}")


def record_cache_candidate(query: str) -> None:
    """把"没命中缓存的问题"登记进候选池。只登记，不自动写缓存。"""
    try:
        normalized = " ".join(query.strip().lower().split())
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cache_candidates (query_norm, sample_query)
                VALUES (%s, %s)
                ON CONFLICT (query_norm) DO UPDATE
                    SET hit_count = cache_candidates.hit_count + 1,
                        last_seen = now()
                """,
                (normalized[:2000], query.strip()[:2000]),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 缓存候选登记失败（不影响回答）：{exc}")
