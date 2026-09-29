"""会员级记忆：**用 PostgreSQL 表，不走向量库**（05-TDD §5.7）。

为什么把偏好从 Milvus 挪走：
  1. 偏好"少而结构化"（每人不超过一二十条，格式就是"类别: 内容"），
     向量检索带来的召回收益很小，却引入了三类风险：跨门店/跨会话污染、
     需要额外一次网络调用、以及 Zilliz 的集合名额（免费版只有 5 个，已被占满）。
  2. **写入与读取都必须过滤"本次入住"的信息**——门店名、房间号、订单号、日期。
     参考项目把这些也当成"用户偏好"，结果下一个会话会用它覆盖系统注入的门店
     （第 15 轮实测到的那个 bug）。这里在 store 层统一拦掉，任何调用方都不会漏。

三级记忆的分工（03 §3.2）：
  会话级 → `short_term_memory`（PG，Redis 可选）
  入住级 → `stay_context` + `guest_sessions`（退房即失效）
  会员级 → `member_preferences`（本模块；跨入住，但只保留真正长期成立的偏好）
"""

from __future__ import annotations

import os

from core.memory.preference_extractor import is_stay_scoped


def _connect():
    import psycopg

    from dotenv import load_dotenv

    backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(os.path.join(backend, "agent", ".env"), override=False)
    return psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True, connect_timeout=5)


def load_preferences(user_id: str, limit: int = 8) -> list[str]:
    """读取该会员的长期偏好（已过滤入住级信息）。读取失败返回空列表，绝不抛给调用方。"""
    if not user_id:
        return []
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pref_key, pref_value FROM member_preferences "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ [member_store] 读取会员偏好失败（不影响回答）：{exc}")
        return []

    items = [f"{key}: {value}" for key, value in rows]
    kept = [item for item in items if not is_stay_scoped(item)]
    if len(kept) != len(items):
        print(f"🧹 [member_store] 读取时过滤掉 {len(items) - len(kept)} 条入住级偏好")
    return kept


def save_preferences(user_id: str, items: list[str], session_id: str = "") -> int:
    """写入长期偏好。**写入前同样过滤**——两头都拦，才不会脏。"""
    if not user_id or not items:
        return 0
    saved = 0
    try:
        with _connect() as conn, conn.cursor() as cur:
            for item in items:
                if is_stay_scoped(item):
                    print(f"🧹 [member_store] 拒绝写入入住级信息：{item[:40]}")
                    continue
                key, _, value = item.partition(":")
                key, value = key.strip(), value.strip()
                if not key or not value:
                    continue
                cur.execute(
                    """
                    INSERT INTO member_preferences (user_id, pref_key, pref_value, source_session)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id, pref_key) DO UPDATE
                        SET pref_value = EXCLUDED.pref_value,
                            source_session = EXCLUDED.source_session,
                            updated_at = now()
                    """,
                    (user_id, key[:48], value, session_id[:128] or None),
                )
                saved += 1
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ [member_store] 写入会员偏好失败（不影响回答）：{exc}")
        return 0
    return saved
