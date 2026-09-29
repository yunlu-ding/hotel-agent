from __future__ import annotations

import os
import re
from typing import Any

from dotenv import load_dotenv

from app_config.settings import settings

# 阈值与模式来自 agent/.env；进程环境变量优先（方便命令行做对照实验）
load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "agent", ".env"),
    override=False,
)

COLLECTION_NAME = os.getenv("QA_CACHE_COLLECTION", "qa_semantic_cache")
EMBEDDING_DIM = 1536
L1_SEMANTIC_DISTANCE_THRESHOLD = 0.08

# ---------------------------------------------------------------------------
# 重要：集合用 COSINE 度量，Milvus 返回的是**相似度**（越大越像），不是距离。
# 课程原版把它当距离、拿 0.08 去比，导致完全相同的问题得分 1.0 也被判为"不命中"，
# 语义检索这条路径实际上永远不会触发（详见 02-环境复现方案与进度.md 第二节第 4 条）。
#
# 这里保留原行为作为基线（distance 模式，阈值 0.08），同时提供 similarity 模式，
# 便于做阈值标定实验：
#   CACHE_SIMILARITY_MODE=distance   + SEMANTIC_CACHE_THRESHOLD=0.08  ← 课程原行为
#   CACHE_SIMILARITY_MODE=similarity + SEMANTIC_CACHE_THRESHOLD=0.9   ← 修正后的行为
# ---------------------------------------------------------------------------
DEFAULT_SIMILARITY_MODE = "distance"

# ---------------------------------------------------------------------------
# 缓存准入与版本绑定（对应"决策 3"）
#
# 1) 黑名单：以下请求**绝不允许**被语义命中，只保留"文本完全一致"的精确匹配能力。
#    判据不是"问题属于哪个主题"，而是"答案是否绑定到某个人的具体数据"——
#    例如"退款要多久到账"（标准规则，可缓存）与"我这笔退款怎么还没到"（个人案例，不可缓存）
#    主题相同，但后者绝不能复用缓存的模板答案。
# 2) 版本绑定：缓存条目带知识库版本号，政策更新后旧版本条目自然失效，
#    不需要人工清理，也不会出现"政策改了缓存还在答旧答案"。
# ---------------------------------------------------------------------------
POLICY_VERSION = os.getenv("POLICY_VERSION", "POLICY-2026.1")

# 个人数据指代词：出现即禁止语义命中（只能精确匹配）
PERSONAL_PATTERNS = [
    r"我的",
    r"我这",
    r"我订",
    r"我买",
    r"我名下",
    r"订单号",
    r"HT\d{4}",
    r"订单\s*[A-Za-z0-9]{6,}",
]

# 高风险主题：即使不带个人指代，也不允许语义命中（金额、赔付、审批例外）
RISKY_PATTERNS = [
    r"退多少",
    r"能退多少",
    r"扣多少",
    r"手续费",
    r"赔多少",
    r"赔付",
    r"通融",
    r"特批",
    r"特殊情况",
    r"免单",
]


class SemanticCache:
    def __init__(self) -> None:
        self._client: Any = None
        self._embeddings: Any = None
        self._available: bool = False
        self._mode = os.getenv("CACHE_SIMILARITY_MODE", DEFAULT_SIMILARITY_MODE).lower()
        self._threshold = float(
            os.getenv("SEMANTIC_CACHE_THRESHOLD", L1_SEMANTIC_DISTANCE_THRESHOLD)
        )
        self._policy_version = os.getenv("POLICY_VERSION", POLICY_VERSION)

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def semantic_allowed(self, query: str) -> tuple[bool, str]:
        """判断这句话允不允许走语义命中（精确匹配不受限制）。

        返回 (是否允许, 原因)。
        """
        for pattern in PERSONAL_PATTERNS:
            if re.search(pattern, query):
                return False, f"含个人数据指代（{pattern}），只允许精确匹配"
        for pattern in RISKY_PATTERNS:
            if re.search(pattern, query):
                return False, f"高风险主题（{pattern}），只允许精确匹配"
        return True, "可语义命中"

    async def initialize(self) -> None:
        try:
            from pymilvus import MilvusClient
            from langchain_community.embeddings import DashScopeEmbeddings

            # 托管版 Milvus（Zilliz Cloud）用完整地址 MILVUS_URI；本地部署回落到 HOST:PORT
            uri = settings.milvus_uri or f"http://{settings.milvus_host}:{settings.milvus_port}"
            connect_kwargs: dict[str, Any] = {"uri": uri}
            if settings.milvus_api_key:
                connect_kwargs["token"] = settings.milvus_api_key

            self._client = MilvusClient(**connect_kwargs)
            self._embeddings = DashScopeEmbeddings(
                model="text-embedding-v2",
                dashscope_api_key=settings.dashscope_api_key,
            )
            self._ensure_collection()
            self._available = True
        except Exception as exc:
            print(f"SemanticCache init failed: {exc}")
            self._available = False

    async def set_cache(
        self,
        query: str,
        response: str,
        user_id: str | None = None,
        scope: str = "public",
        store_id: str = "",
    ) -> None:
        if not self._available:
            return
        normalized = self._normalize(query)
        owner = user_id or ""
        cache_scope = "user" if owner else scope
        # store_id 是 P2 的防线：公共缓存按门店隔离，
        # 否则会把 A 店的答案命中给 B 店客人（错得"很像对的"，而且会被稳定复制）。
        cache_store = "" if cache_scope == "user" else (store_id or "")
        try:
            embedding = await self._embeddings.aembed_query(normalized)
            safe_norm = normalized.replace('"', '\\"')
            safe_scope = cache_scope.replace('"', '\\"')
            safe_owner = owner.replace('"', '\\"')
            safe_store = cache_store.replace('"', '\\"')
            delete_filter = (
                f'question_norm == "{safe_norm}" and scope == "{safe_scope}" '
                f'and user_id == "{safe_owner}" and store_id == "{safe_store}"'
            )
            self._client.delete(collection_name=COLLECTION_NAME, filter=delete_filter)
            self._client.insert(
                collection_name=COLLECTION_NAME,
                data=[
                    {
                        "question": query.strip(),
                        "question_norm": normalized,
                        "answer": response,
                        "scope": cache_scope,
                        "user_id": owner,
                        "store_id": cache_store,
                        "enabled": 1,
                        "policy_version": self._policy_version,
                        "embedding": embedding,
                    }
                ],
            )
        except Exception as exc:
            print(f"SemanticCache set_cache failed: {exc}")

    async def get_cache(self, query: str, user_id: str, store_id: str = "") -> dict[str, Any] | None:
        if not self._available:
            return None
        normalized = self._normalize(query)
        safe_norm = normalized.replace('"', '\\"')
        safe_user = user_id.replace('"', '\\"')
        safe_store = (store_id or "").replace('"', '\\"')
        # 公共条目：集团级（store_id="") + 当前门店；其他门店的一律排除
        store_scope = f'(store_id == "" or store_id == "{safe_store}")'

        user_filter = (
            f'enabled == 1 and policy_version == "{self._policy_version}" '
            f'and question_norm == "{safe_norm}" and scope == "user" and user_id == "{safe_user}"'
        )
        public_filter = (
            f'enabled == 1 and policy_version == "{self._policy_version}" '
            f'and question_norm == "{safe_norm}" and scope == "public" and {store_scope}'
        )
        user_exact = self._query_one(user_filter)
        if user_exact:
            return {
                "answer": user_exact["answer"],
                "matched_question": user_exact["question"],
                "level": "L1_EXACT",
                "distance": 0.0,
            }

        public_exact = self._query_one(public_filter)
        if public_exact:
            return {
                "answer": public_exact["answer"],
                "matched_question": public_exact["question"],
                "level": "L1_EXACT",
                "distance": 0.0,
            }

        try:
            allowed, reason = self.semantic_allowed(query)
            if not allowed:
                print(f"🛡️ 语义缓存跳过：{reason}")
                return None
            query_embedding = await self._embeddings.aembed_query(normalized)
            scoped_filter = (
                f'enabled == 1 and policy_version == "{self._policy_version}" '
                f'and ((scope == "public" and {store_scope}) '
                f'  or (scope == "user" and user_id == "{safe_user}"))'
            )
            results = self._client.search(
                collection_name=COLLECTION_NAME,
                data=[query_embedding],
                filter=scoped_filter,
                limit=1,
                output_fields=["question", "answer", "scope", "user_id", "store_id"],
            )
            if not results:
                return None
            hit = results[0][0] if results[0] else None
            if not hit:
                return None
            distance = float(hit.get("distance", 1.0))
            if self._mode == "similarity":
                # 相似度越大越像：低于阈值视为没命中
                if distance < self._threshold:
                    return None
            else:
                # 距离越小越像（课程原逻辑）：高于阈值视为没命中
                if distance > self._threshold:
                    return None
            entity = hit.get("entity", {})
            return {
                "answer": entity.get("answer", ""),
                "matched_question": entity.get("question", ""),
                "level": "L1_SEMANTIC",
                "distance": distance,
                "raw_score": distance,
                "mode": self._mode,
                "threshold": self._threshold,
                "policy_version": self._policy_version,
            }
        except Exception as exc:
            print(f"SemanticCache get_cache failed: {exc}")
            return None

    @property
    def available(self) -> bool:
        return self._available

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.strip().lower().split())

    def _query_one(self, filter_expr: str) -> dict[str, Any] | None:
        try:
            rows = self._client.query(
                collection_name=COLLECTION_NAME,
                filter=filter_expr,
                output_fields=["question", "answer", "scope", "user_id"],
                limit=1,
            )
            if rows:
                return rows[0]
            return None
        except Exception:
            return None

    def _ensure_collection(self) -> None:
        from pymilvus import DataType

        if self._client.has_collection(COLLECTION_NAME):
            return

        schema = self._client.create_schema()
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("question", DataType.VARCHAR, max_length=2048)
        schema.add_field("question_norm", DataType.VARCHAR, max_length=2048)
        schema.add_field("answer", DataType.VARCHAR, max_length=8192)
        schema.add_field("scope", DataType.VARCHAR, max_length=16)
        schema.add_field("user_id", DataType.VARCHAR, max_length=128)
        # 门店维度："" = 集团级（对所有门店可用）；否则只对该门店的客人可见
        schema.add_field("store_id", DataType.VARCHAR, max_length=32)
        schema.add_field("enabled", DataType.INT8)
        # 知识库版本号：政策更新后旧条目自然失效（决策 3 的"版本绑定"）
        schema.add_field("policy_version", DataType.VARCHAR, max_length=64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM)

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="IVF_FLAT",
            metric_type="COSINE",
            params={"nlist": 256},
        )

        self._client.create_collection(
            collection_name=COLLECTION_NAME,
            schema=schema,
            index_params=index_params,
        )

    def recreate_collection(self) -> None:
        """重建集合（schema 变更或需要清空历史缓存时使用）。"""
        if self._client.has_collection(COLLECTION_NAME):
            self._client.drop_collection(COLLECTION_NAME)
            print(f"已删除旧集合 {COLLECTION_NAME}（准备按新 schema 重建）")
        self._ensure_collection()
        print(f"已重建集合 {COLLECTION_NAME}，当前知识库版本 {self._policy_version}")

    def invalidate_store(self, store_id: str, reason: str = "") -> int:
        """门店规则变更时，清掉**该门店的公共缓存**。

        这是 05-TDD §5.6 的"门店级失效"落地：
          门店改了早餐时间 → 该店的旧答案必须先失效，否则客人会一直拿到旧答案，
          而且因为它"秒回"，客人会更相信它。
        集团级条目（store_id=""）不动——集团政策没变。
        """
        if not self._available or not store_id:
            return 0
        safe = store_id.replace('"', "")
        try:
            result = self._client.delete(
                collection_name=COLLECTION_NAME,
                filter=f'scope == "public" and store_id == "{safe}"',
            )
            print(f"🧹 已失效门店 {store_id} 的公共缓存" + (f"（{reason}）" if reason else ""))
            return int(result.get("delete_count", 0)) if isinstance(result, dict) else 0
        except Exception as exc:  # noqa: BLE001
            print(f"SemanticCache invalidate_store failed: {exc}")
            return 0


semantic_cache = SemanticCache()
