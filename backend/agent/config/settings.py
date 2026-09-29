"""Application settings using Pydantic Settings."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # LLM Configuration
    dashscope_api_key: str = Field(alias="DASHSCOPE_API_KEY")
    model: str = Field(default="qwen-plus", alias="MODEL")
    base_url: str | None = Field(default=None, alias="BASE_URL")
    
    # MCP Configuration
    mcp_servers_config: Path = Field(
        default=Path(__file__).parent / "mcp_servers.json",
        alias="MCP_SERVERS_CONFIG"
    )
    
    # Weather API
    openweather_api_key: str | None = Field(default=None, alias="OPENWEATHER_API_KEY")
    
    # Redis (short-term memory)
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")
    redis_ttl: int = Field(default=1800, alias="REDIS_TTL")  # seconds

    # 业务库（订单数据）；Redis 不可用时，短期记忆也用它兜底
    postgres_dsn: str | None = Field(default=None, alias="POSTGRES_DSN")
    
    # Milvus (long-term memory)
    # MILVUS_URI 用于托管版 Milvus（如 Zilliz Cloud）；设置后优先于 HOST/PORT
    milvus_uri: str | None = Field(default=None, alias="MILVUS_URI")
    milvus_host: str = Field(default="localhost", alias="MILVUS_HOST")
    milvus_port: int = Field(default=19530, alias="MILVUS_PORT")
    milvus_api_key: str | None = Field(default=None, alias="MILVUS_API_KEY")
    
    # Neo4j (knowledge graph)
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="password", alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")

    # ------------------------------------------------------------------
    # 以下为本项目（酒店住客全旅程）新增配置。
    # 默认值刻意"保持参考项目当前行为"，避免在基线阶段改变任何既有语义。
    # 依据：05-TDD §8 环境与配置、03 技术选型与关键决定的后果
    # ------------------------------------------------------------------

    # 门店维度：hotels.hotel_id 即 store_id（不新建 stores 表，避免两个近义概念）
    default_store_id: str = Field(default="HT001", alias="DEFAULT_STORE_ID")

    # 向量集合（重命名与重建在阶段二执行，这里先沿用现有集合名，保证基线可跑）
    policy_collection: str = Field(default="hotel_policy_docs", alias="MILVUS_POLICY_COLLECTION")
    qa_cache_collection: str = Field(default="qa_semantic_cache", alias="QA_CACHE_COLLECTION")
    knowledge_version: str = Field(default="HOTEL-2026.1", alias="POLICY_VERSION")

    # 图谱默认关闭（03 变动 5）
    enable_neo4j: bool = Field(default=False, alias="ENABLE_NEO4J")

    # 工单 SLA 默认值（待校准，见 01-现状与竞品 §6 待核实清单）
    sla_service_minutes: int = Field(default=20, alias="SLA_SERVICE_MINUTES")
    sla_maintenance_minutes: int = Field(default=30, alias="SLA_MAINTENANCE_MINUTES")
    sla_safety_minutes: int = Field(default=10, alias="SLA_SAFETY_MINUTES")

    # 入住级记忆宽限期（小时）与多跳上限
    stay_grace_hours: int = Field(default=2, alias="STAY_GRACE_HOURS")
    hop_max: int = Field(default=2, alias="HOP_MAX")
    
    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    
    @field_validator("dashscope_api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        """Validate API key is not empty."""
        if not v or v.strip() == "":
            raise ValueError("DASHSCOPE_API_KEY cannot be empty")
        return v.strip()
    
    def get_model_config(self) -> dict[str, Any]:
        """Get model configuration for LangChain."""
        config: dict[str, Any] = {
            "model": self.model,
            "api_key": self.dashscope_api_key,
        }
        if self.base_url:
            config["base_url"] = self.base_url
        return config


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
