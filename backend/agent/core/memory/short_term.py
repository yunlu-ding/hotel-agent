"""短期对话内存（Redis 优先，PostgreSQL 兜底）。

消息按用户/会话存储，并基于 TTL 过期。
当消息数量超过 COMPRESSION_THRESHOLD 时，较旧的消息
会自动被修剪，以仅保留最近的消息。

为什么加了 PostgreSQL 兜底：
- 课程原实现只连 Redis，连不上就**静默降级**（available=False），
  于是"多轮记忆"看起来生效、实际每次都重新查一遍数据；
- 本机 Windows 上 Redis 免安装版因页面文件限制起不来（见 02 文档），
  而项目里本来就有 PostgreSQL 在跑，因此用一张带 expire_at 的表实现同样的
  "按用户/会话隔离 + TTL + 窗口裁剪"语义，接口与行为保持一致。
"""

import datetime as dt
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

COMPRESSION_THRESHOLD = 10  # trim when messages exceed this count
DEFAULT_TTL = 1800          # 30 minutes in seconds
PG_TABLE = "short_term_memory"


class ShortTermMemory:
    """基于 Redis 的短期对话内存。

    功能：
    - 按用户/会话的键隔离 (`memory:short:{user_id}:{session_id}`)
    - 基于 TTL 的自动过期（默认 30 分钟，可配置）
    - 当超过 COMPRESSION_THRESHOLD 时自动修剪消息
    - 优雅降级：如果 Redis 不可用，操作将变为空操作

    用法::

        mem = ShortTermMemory()
        await mem.initialize()

        await mem.save_messages("user1", "s1", messages)
        msgs = await mem.get_messages("user1", "s1")
        await mem.close()
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        ttl: int = DEFAULT_TTL,
        postgres_dsn: str | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._ttl = ttl
        self._postgres_dsn = postgres_dsn
        self._client: Any = None
        self._available: bool = False
        self._backend: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Connect to Redis; sets _available=False on failure (no exception raised)."""
        try:
            import redis.asyncio as aioredis  # type: ignore[import]

            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                health_check_interval=30,
                retry_on_timeout=True,
            )
            await self._client.ping()
            self._available = True
            self._backend = "redis"
            logger.info("ShortTermMemory: Redis connected at %s", self._redis_url)
        except Exception as exc:
            logger.warning(
                "ShortTermMemory: Redis unavailable (%s) – 尝试 PostgreSQL 兜底。", exc
            )
            self._available = False

        if not self._available and self._postgres_dsn:
            await self._init_postgres()

        if not self._available:
            logger.warning("ShortTermMemory: 没有可用的存储后端，短期记忆关闭。")

    async def _init_postgres(self) -> None:
        """用 PostgreSQL 表实现同样的短期记忆语义（TTL + 会话隔离）。

        注意：Windows 上 uvicorn 默认用 ProactorEventLoop（MCP 的子进程通信依赖它），
        而 psycopg 的异步连接不支持 Proactor，所以这里用**同步连接 + 线程池**，
        既不换事件循环、也不阻塞主协程。
        """
        import asyncio

        try:
            await asyncio.to_thread(self._pg_connect)
            self._backend = "postgres"
            self._available = True
            logger.info("ShortTermMemory: 使用 PostgreSQL 作为短期记忆存储（带 TTL）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("ShortTermMemory: PostgreSQL 兜底失败 (%s)", exc)
            self._available = False

    def _pg_connect(self) -> None:
        import psycopg

        self._client = psycopg.connect(self._postgres_dsn, autocommit=True)
        with self._client.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {PG_TABLE} (
                    user_id    VARCHAR(64)  NOT NULL,
                    session_id VARCHAR(128) NOT NULL,
                    payload    JSONB        NOT NULL,
                    expire_at  TIMESTAMPTZ  NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                )
                """
            )

    def _pg_get(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        with self._client.cursor() as cur:
            cur.execute(
                f"SELECT payload FROM {PG_TABLE} "
                "WHERE user_id = %s AND session_id = %s AND expire_at > now()",
                (user_id, session_id),
            )
            row = cur.fetchone()
        return row[0] if row else []

    def _pg_save(self, user_id: str, session_id: str, messages: list[dict[str, Any]]) -> None:
        expire_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self._ttl)
        with self._client.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {PG_TABLE} (user_id, session_id, payload, expire_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, session_id)
                DO UPDATE SET payload = EXCLUDED.payload, expire_at = EXCLUDED.expire_at
                """,
                (user_id, session_id, json.dumps(messages, ensure_ascii=False), expire_at),
            )

    def _pg_clear(self, user_id: str, session_id: str) -> None:
        with self._client.cursor() as cur:
            cur.execute(
                f"DELETE FROM {PG_TABLE} WHERE user_id = %s AND session_id = %s",
                (user_id, session_id),
            )

    async def close(self) -> None:
        """Close the underlying connection."""
        if self._client is not None:
            try:
                if self._backend == "redis":
                    await self._client.aclose()
                else:
                    await self._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_messages(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        """Return stored messages for the given user/session.

        Returns an empty list when Redis is unavailable or the key is missing.
        """
        if not self._available:
            return []
        try:
            if self._backend == "redis":
                data = await self._client.get(self._key(user_id, session_id))
                return json.loads(data) if data else []
            import asyncio

            return await asyncio.to_thread(self._pg_get, user_id, session_id)
        except Exception as exc:
            logger.warning("ShortTermMemory.get_messages failed: %s", exc)
            self._available = False
            return []

    async def save_messages(
        self, user_id: str, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Persist messages to Redis, applying compression when needed.

        Args:
            user_id: Unique user identifier.
            session_id: Unique session identifier.
            messages: List of message dicts with ``role`` and ``content`` keys.
        """
        if not self._available:
            return
        try:
            if len(messages) > COMPRESSION_THRESHOLD:
                messages = self._trim(messages)
            if self._backend == "redis":
                await self._client.set(
                    self._key(user_id, session_id),
                    json.dumps(messages, ensure_ascii=False),
                    ex=self._ttl,
                )
            else:
                import asyncio

                await asyncio.to_thread(self._pg_save, user_id, session_id, messages)
            logger.debug(
                "ShortTermMemory: saved %d messages for %s:%s", len(messages), user_id, session_id
            )
        except Exception as exc:
            logger.warning("ShortTermMemory.save_messages failed: %s", exc)
            self._available = False

    async def append_message(
        self, user_id: str, session_id: str, role: str, content: str
    ) -> None:
        """Append a single message and re-persist."""
        messages = await self.get_messages(user_id, session_id)
        messages.append({"role": role, "content": content})
        await self.save_messages(user_id, session_id, messages)

    async def clear(self, user_id: str, session_id: str) -> None:
        """Delete all messages for a user/session."""
        if not self._available:
            return
        try:
            if self._backend == "redis":
                await self._client.delete(self._key(user_id, session_id))
            else:
                import asyncio

                await asyncio.to_thread(self._pg_clear, user_id, session_id)
        except Exception as exc:
            logger.error("ShortTermMemory.clear failed: %s", exc)

    @property
    def backend(self) -> str | None:
        """当前实际使用的存储后端：redis / postgres。"""
        return self._backend

    @property
    def available(self) -> bool:
        """True if Redis is reachable."""
        return self._available

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key(user_id: str, session_id: str) -> str:
        return f"memory:short:{user_id}:{session_id}"

    @staticmethod
    def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep system messages + the 6 most recent non-system messages."""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]
        return system_msgs + other_msgs[-6:]
