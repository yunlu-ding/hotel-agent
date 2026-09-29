"""结构化目录检索工具（替代课程里的 Neo4j 图谱工具）。

设计说明（这是一个产品判断，不是妥协）：
- 课程用 Neo4j + 模型生成 Cypher 做"精确属性查询"，代价是模型写错语句就失败，
  只能靠关键词兜底，链路不可控；
- 酒店场景的结构化关系只有"城市 → 酒店 → 房型 → 政策"两三层，用确定性 SQL 足够；
- 所以这里改成"模型负责抽取筛选条件，SQL 负责确定性地查"：
  模型只做它擅长的语义理解，查询结果由数据库保证，不存在"生成的语句跑不通"。
如果要恢复图谱版本，把 product_agent 里的 import 换成 tools/graph_tool.py 即可。
"""

import json
import os
from decimal import Decimal
from typing import Any

import psycopg
from dotenv import load_dotenv
from langchain_core.tools import tool
from psycopg.rows import dict_row

dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path)


def _json(payload: dict) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return json.dumps(payload, ensure_ascii=False, default=default)


@tool
def query_hotel_structured(
    city: str = "",
    tag: str = "",
    max_price: float = 0,
    min_guests: int = 0,
    must_free_cancel: bool = False,
) -> str:
    """按结构化条件查询酒店与房型（城市、标签、价格上限、可住人数、是否支持免费取消）。

    当用户问"某城市有哪些酒店/房型""哪些房型能住 3 个人""哪些能免费取消"这类
    **属性筛选**问题时使用。参数由你从用户问题中抽取，不要传自然语言句子。

    Args:
        city: 城市名，如 "三亚"；不确定就留空。
        tag: 标签关键词，如 "亲子"、"海景"、"商务"、"近地铁"。
        max_price: 每晚价格上限（元），0 表示不限。
        min_guests: 最少可住人数，0 表示不限。
        must_free_cancel: 是否只返回支持免费取消的房型。
    """
    sql = """
        SELECT h.name AS hotel_name, h.city, h.star, h.rating, h.tags AS hotel_tags,
               r.name AS room_name, r.max_guests, r.breakfast, r.list_price,
               r.cancel_policy, r.tags AS room_tags
        FROM room_types r
        JOIN hotels h ON h.hotel_id = r.hotel_id
        WHERE (%s = '' OR h.city = %s)
          AND (%s = '' OR EXISTS (
                SELECT 1 FROM unnest(h.tags || r.tags) AS t WHERE t ILIKE '%%' || %s || '%%'))
          AND (%s = 0 OR r.list_price <= %s)
          AND (%s = 0 OR r.max_guests >= %s)
          AND (%s = FALSE OR r.cancel_policy IN ('FREE_5D', 'FREE_24H'))
        ORDER BY h.rating DESC, r.list_price ASC
        LIMIT 15
    """
    params = (
        city, city,
        tag, tag,
        max_price, max_price,
        min_guests, min_guests,
        must_free_cancel,
    )
    try:
        with psycopg.connect(
            os.getenv("POSTGRES_DSN"), row_factory=dict_row, connect_timeout=5
        ) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"结构化查询失败: {exc}"})

    if not rows:
        return _json(
            {
                "status": "not_found",
                "message": "没有满足条件的房型，可放宽城市、价格或人数条件。",
            }
        )
    for row in rows:
        row["list_price"] = float(row["list_price"])
    return _json({"status": "success", "count": len(rows), "data": rows})
