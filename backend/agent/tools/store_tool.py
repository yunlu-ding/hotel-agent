"""门店事实工具：结构化 KV，而不是向量检索。

为什么单独做一个工具（而不是把门店事实塞进政策文档）：
门店事实有三个特征——**归属明确（某家店）、结构明确（键值）、变更频繁（改个早餐时间）**。
把它放进向量库，这三条都会变成隐患：检索可能召回别家店的条目，
改了规则还要等重建索引，而且无法做"门店级缓存失效"。

设计依据：05-TDD §5.5 知识分层与检索。
优先级：**门店事实优先于政策文档**；两者冲突时以门店事实为准。
"""

from __future__ import annotations

import json
import os

import psycopg
from dotenv import load_dotenv
from langchain_core.tools import tool

from core.runtime import current_store_id

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

# 事实键的中文名，仅用于把结果说得更像人话
FACT_LABELS = {
    "breakfast_time": "早餐时间",
    "breakfast_location": "早餐地点",
    "wifi_ssid": "WiFi 名称",
    "wifi_password": "WiFi 密码",
    "pool_hours": "泳池开放时间",
    "gym_hours": "健身房开放时间",
    "beach": "沙滩",
    "parking": "停车",
    "laundry": "洗衣服务",
    "checkout_time": "退房时间",
}


def _query(store_id: str, fact_keys: list[str] | None) -> list[tuple[str, str, str]]:
    dsn = os.getenv("POSTGRES_DSN")
    sql = """
        SELECT f.fact_key, f.fact_value, h.name
          FROM store_facts f
          LEFT JOIN hotels h ON h.hotel_id = f.store_id
         WHERE f.store_id = %s AND f.active = TRUE
    """
    params: list[object] = [store_id]
    if fact_keys:
        sql += " AND f.fact_key = ANY(%s)"
        params.append(fact_keys)
    sql += " ORDER BY f.fact_key"
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@tool
def query_store_facts(fact_keys: list[str] | None = None) -> str:
    """查询**当前客人所在门店**的事实信息（门店自己设定的规则，不是集团统一政策）。

    适用场景（这些问题的答案每家店都不一样，必须查本工具）：
      · 早餐几点开始/结束、在哪里吃（breakfast_time / breakfast_location）
      · WiFi 叫什么、密码是什么（wifi_ssid / wifi_password）
      · 泳池/健身房几点开放（pool_hours / gym_hours）
      · 有没有停车位、收不收费（parking）
      · 洗衣服务怎么算（laundry）
      · 几点退房、能不能延迟退房（checkout_time）

    Args:
        fact_keys: 可选，指定要查的键（如 ["breakfast_time"]）。
                   不传则返回该门店的全部事实。

    注意：门店由系统上下文注入，你无法也不需要指定门店；
    如果本工具查不到，再考虑用 query_policy_docs 查集团政策。
    """
    store_id = current_store_id()
    print(f"[store_tool] 解析到的门店 = {store_id!r}（fact_keys={fact_keys}）", flush=True)
    try:
        rows = _query(store_id, fact_keys)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error", "message": f"查询门店信息失败: {exc}"}, ensure_ascii=False)

    if not rows:
        return json.dumps(
            {
                "status": "not_found",
                "store_id": store_id,
                "message": "该门店暂无登记的门店信息，请用 query_policy_docs 查集团政策，或建议客人联系前台。",
            },
            ensure_ascii=False,
        )

    hotel_name = rows[0][2] or store_id
    facts = [
        {"key": key, "label": FACT_LABELS.get(key, key), "value": value}
        for key, value, _ in rows
    ]
    return json.dumps(
        {
            "status": "success",
            "store_id": store_id,
            "hotel_name": hotel_name,
            "facts": facts,
            "note": "以上为门店自行设定的信息，与其他门店可能不同；冲突时以本结果为准。",
        },
        ensure_ascii=False,
    )
