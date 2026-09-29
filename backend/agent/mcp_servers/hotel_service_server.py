"""酒店订单客服 · MCP 工具服务。

与课程原版（cloud_platform_server.py，云平台 / MySQL / pymysql）的差异：
1. 业务域从云平台换成酒店订单，工具与字段全部重写；
2. 数据源从 MySQL 换成 PostgreSQL（本机已有实例，方言与驱动随之调整）；
3. 所有涉及个人数据的工具都做**属主校验**：先按 user_id 过滤，查不到就统一返回
   "未找到该订单"，不区分"不存在"和"不属于你"，避免泄露他人订单是否存在。

工具清单（7 个，与课程原版数量一致）：
    query_user_orders        订单列表
    query_order_detail       单笔订单明细
    analyze_order_cost       省钱空间分析（成本优化 Agent 用）
    search_hotel_catalog     酒店/房型目录检索（推荐 Agent 用）
    get_promotable_products  可推广的酒店活动（推广 Agent 用）
    get_promotion_materials  专属推广链接
    generate_ai_poster       生成推广海报
"""

import json
import os
import sys
from decimal import Decimal
from typing import Any

import psycopg
import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row

dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path)

# ------------------------------------------------------------------------------
# 工单域只有一份实现（backend/app/service/work_order_service.py），MCP server 复用同一份。
# 为什么要复用而不是在这里重写：状态机与 SLA 映射一旦出现两份，
# "按时闭环率"这个指标就会有两个口径，评测结论就不可信了。
# ------------------------------------------------------------------------------
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP_DIR = os.path.join(_BACKEND_DIR, "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

mcp = FastMCP("HotelServiceMCPServer")

CANCEL_POLICY_LABEL = {
    "FREE_5D": "提前 5 天免费取消",
    "FREE_24H": "提前 24 小时免费取消",
    "PARTIAL_3D": "入住前 3 天可退 50%",
    "NON_REFUND": "特惠不可取消",
}


# ==============================================================================
# 数据访问
# ==============================================================================
def get_connection() -> psycopg.Connection:
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("未配置 POSTGRES_DSN")
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


def _json(payload: dict) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return json.dumps(payload, ensure_ascii=False, default=default)


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


# ==============================================================================
# 订单类工具
# ==============================================================================
@mcp.tool()
def query_user_orders(user_id: str, limit: int = 5) -> str:
    """查询当前用户名下的酒店订单列表（含酒店、房型、入住日期、金额、状态、取消政策）。

    Args:
        user_id: [系统注入] 当前登录用户的唯一标识，模型无法伪造。
        limit: [模型生成] 返回的最大记录数，默认 5。
    """
    sql = """
        SELECT o.order_id,
               h.name  AS hotel_name,
               r.name  AS room_name,
               o.check_in,
               o.check_out,
               (o.check_out - o.check_in) AS nights,
               o.amount,
               o.status,
               o.cancel_policy,
               o.channel
        FROM orders o
        JOIN room_types r ON r.room_id = o.room_id
        JOIN hotels h     ON h.hotel_id = r.hotel_id
        WHERE o.user_id = %s
        ORDER BY o.check_in DESC
        LIMIT %s
    """
    try:
        rows = _query(sql, (user_id, limit))
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询订单失败: {exc}"})

    if not rows:
        return _json({"status": "success", "message": "当前账号下没有查询到订单记录。"})

    for row in rows:
        row["cancel_policy_desc"] = CANCEL_POLICY_LABEL.get(row["cancel_policy"], row["cancel_policy"])
    return _json({"status": "success", "data": rows})


@mcp.tool()
def query_order_detail(order_id: str, user_id: str) -> str:
    """查询单笔订单的明细：酒店、房型、入住/离店日期、间数、金额、取消政策、入住人限制。

    用于回答"我这单能不能退、退多少"这类问题，退改结果要结合政策文档计算。

    Args:
        order_id: 订单号，必须先通过 query_user_orders 获取。
        user_id: [系统注入] 当前登录用户 ID，用于属主校验。
    """
    if not order_id:
        return _json({"status": "error", "message": "必须提供订单号 order_id"})

    sql = """
        SELECT o.order_id,
               h.name AS hotel_name,
               h.city,
               h.star,
               r.name AS room_name,
               r.max_guests,
               r.breakfast,
               o.check_in,
               o.check_out,
               (o.check_out - o.check_in) AS nights,
               o.room_count,
               o.list_total,
               o.amount,
               o.status,
               o.cancel_policy,
               o.channel,
               o.created_at,
               (o.check_in - CURRENT_DATE) AS days_to_check_in
        FROM orders o
        JOIN room_types r ON r.room_id = o.room_id
        JOIN hotels h     ON h.hotel_id = r.hotel_id
        WHERE o.order_id = %s AND o.user_id = %s
    """
    try:
        rows = _query(sql, (order_id, user_id))
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询订单失败: {exc}"})

    if not rows:
        # 不区分"订单不存在"与"订单属于他人"，避免泄露他人订单是否存在
        return _json({"status": "not_found", "message": "未找到该订单，请确认订单号是否属于当前账号。"})

    row = rows[0]
    row["cancel_policy_desc"] = CANCEL_POLICY_LABEL.get(row["cancel_policy"], row["cancel_policy"])
    row["first_night_fee"] = round(float(row["amount"]) / max(1, row["nights"]), 2)
    return _json({"status": "success", "data": row})


@mcp.tool()
def analyze_order_cost(order_id: str, user_id: str = "") -> str:
    """分析一笔订单的省钱空间：对比挂牌价与同城均价、列出可用的未使用优惠券、判断是否处于免费取消期。

    这是"帮我看看这单买贵了没有、怎么更省"这类请求的数据来源。
    工具只返回事实与试算结果，**结论与建议由 Agent 组织，且必须提示风险**。

    Args:
        order_id: 订单号，必须先通过 query_user_orders 获取。
        user_id: [系统注入] 当前登录用户 ID，用于属主校验。
    """
    if not order_id:
        return _json({"status": "error", "message": "必须提供订单号 order_id"})

    base_sql = """
        SELECT o.order_id,
               o.user_id,
               h.name AS hotel_name,
               h.city,
               h.star,
               r.room_id,
               r.name AS room_name,
               r.list_price,
               o.check_in,
               o.check_out,
               (o.check_out - o.check_in) AS nights,
               o.amount,
               o.list_total,
               o.status,
               o.cancel_policy,
               (o.check_in - CURRENT_DATE) AS days_to_check_in
        FROM orders o
        JOIN room_types r ON r.room_id = o.room_id
        JOIN hotels h     ON h.hotel_id = r.hotel_id
        WHERE o.order_id = %s AND o.user_id = %s
    """
    try:
        rows = _query(base_sql, (order_id, user_id))
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询订单失败: {exc}"})

    if not rows:
        return _json({"status": "not_found", "message": "未找到该订单，请确认订单号是否属于当前账号。"})

    order = rows[0]
    amount = float(order["amount"])
    nights = max(1, order["nights"])

    try:
        # 1) 同城同星级均价（不含本单）
        peers = _query(
            """
            SELECT ROUND(AVG(o.amount / GREATEST(o.check_out - o.check_in, 1)), 2) AS city_avg_nightly
            FROM orders o
            JOIN room_types r ON r.room_id = o.room_id
            JOIN hotels h     ON h.hotel_id = r.hotel_id
            WHERE h.city = %s AND h.star = %s AND o.order_id <> %s
            """,
            (order["city"], order["star"], order_id),
        )
        city_avg = (
            float(peers[0]["city_avg_nightly"])
            if peers and peers[0]["city_avg_nightly"] is not None
            else None
        )

        # 2) 可用优惠券（未使用、未过期、满足门槛）
        coupons = _query(
            """
            SELECT c.coupon_id, c.name, c.discount_type, c.discount_value, c.min_amount,
                   c.applicable_city, c.member_only, uc.expire_at,
                   CASE
                       WHEN c.discount_type = 'CASH' THEN LEAST(c.discount_value, %s::numeric)
                       ELSE ROUND(%s::numeric * (1 - c.discount_value), 2)
                   END AS expected_discount
            FROM user_coupons uc
            JOIN coupons c ON c.coupon_id = uc.coupon_id
            WHERE uc.user_id = %s
              AND uc.status = 'UNUSED'
              AND uc.expire_at >= CURRENT_DATE
            ORDER BY expected_discount DESC
            """,
            (amount, amount, user_id),
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"试算失败: {exc}"})
    applicable, not_applicable = [], []
    for coupon in coupons:
        city_ok = coupon["applicable_city"] in (None, order["city"])
        amount_ok = amount >= float(coupon["min_amount"])
        coupon["expected_discount"] = float(coupon["expected_discount"])
        if city_ok and amount_ok:
            applicable.append(coupon)
        else:
            coupon["reason"] = (
                f"仅限{coupon['applicable_city']}使用" if not city_ok else
                f"订单金额未达门槛 {coupon['min_amount']} 元"
            )
            not_applicable.append(coupon)

    # 3) 是否还在免费取消期（用于判断"能否取消后重新下单"）
    policy = order["cancel_policy"]
    days = order["days_to_check_in"]
    if policy == "FREE_5D":
        in_free_window = days >= 5
    elif policy == "FREE_24H":
        in_free_window = days >= 1
    elif policy == "PARTIAL_3D":
        in_free_window = days >= 3
    else:
        in_free_window = False

    result = {
        "order": {
            "order_id": order["order_id"],
            "hotel_name": order["hotel_name"],
            "city": order["city"],
            "star": order["star"],
            "room_name": order["room_name"],
            "check_in": order["check_in"],
            "check_out": order["check_out"],
            "nights": nights,
            "paid_amount": amount,
            "list_total": float(order["list_total"]),
            "status": order["status"],
            "cancel_policy": policy,
            "cancel_policy_desc": CANCEL_POLICY_LABEL.get(policy, policy),
            "days_to_check_in": days,
        },
        "benchmark": {
            "list_price_per_night": float(order["list_price"]),
            "paid_per_night": round(amount / nights, 2),
            "city_same_star_avg_nightly": city_avg,
        },
        "coupons": {
            "applicable": applicable,
            "not_applicable": not_applicable,
        },
        "refund_window": {
            "in_free_cancel_window": in_free_window,
            "can_rebook_hint": (
                "处于免费取消期内，理论上可取消后重新下单使用优惠券"
                if in_free_window
                else "已超过免费取消期，取消会产生费用，不建议取消重下单"
            ),
        },
        "facts_note": "以上均为系统试算结果；是否取消重下单还取决于库存与届时房价，需用户自行确认。",
    }
    return _json({"status": "success", "data": result})


# ==============================================================================
# 目录 / 推荐类工具
# ==============================================================================
@mcp.tool()
def search_hotel_catalog(
    keyword: str = "",
    city: str = "",
    max_price: float = 0,
    user_id: str = "",
) -> str:
    """在酒店/房型目录里做条件检索与筛选，返回真实在售的酒店与房型。

    适用于"三亚有哪些亲子酒店""800 元以内可取消的大床房"这类选型与推荐请求。

    Args:
        keyword: 关键词，可匹配酒店名、房型名、标签（如"亲子""海景""商务"）。
        city: 城市名，如"三亚"。
        max_price: 每晚价格上限（元），0 表示不限。
        user_id: [系统注入] 当前用户 ID（本工具只读目录，不涉及个人数据）。
    """
    sql = """
        SELECT h.hotel_id, h.name AS hotel_name, h.city, h.district, h.star, h.rating,
               h.tags AS hotel_tags,
               r.room_id, r.name AS room_name, r.bed_type, r.max_guests, r.breakfast,
               r.list_price, r.cancel_policy, r.tags AS room_tags
        FROM room_types r
        JOIN hotels h ON h.hotel_id = r.hotel_id
        WHERE (%s = '' OR h.city = %s)
          AND (%s = 0 OR r.list_price <= %s)
          AND (
                %s = ''
                OR h.name ILIKE '%%' || %s || '%%'
                OR r.name ILIKE '%%' || %s || '%%'
                OR %s = ANY(h.tags)
                OR %s = ANY(r.tags)
                OR EXISTS (SELECT 1 FROM unnest(h.tags || r.tags) AS t WHERE t ILIKE '%%' || %s || '%%')
          )
        ORDER BY h.rating DESC, r.list_price ASC
        LIMIT 20
    """
    params = (
        city, city,
        max_price, max_price,
        keyword, keyword, keyword, keyword, keyword, keyword,
    )
    try:
        rows = _query(sql, params)
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"检索酒店目录失败: {exc}"})

    if not rows:
        return _json(
            {
                "status": "not_found",
                "message": "没有匹配的酒店或房型，请放宽城市、价格或关键词条件。",
            }
        )

    for row in rows:
        row["cancel_policy_desc"] = CANCEL_POLICY_LABEL.get(row["cancel_policy"], row["cancel_policy"])
        row["list_price"] = float(row["list_price"])
    return _json({"status": "success", "count": len(rows), "data": rows})


# ==============================================================================
# 推广类工具
# ==============================================================================
@mcp.tool()
def get_promotable_products(user_id: str = "") -> str:
    """获取当前可以推广（带返佣）的酒店活动列表。

    当用户说"我想推广酒店""有哪些能赚佣金的""我要做分销"时调用。
    """
    try:
        rows = _query(
            """
            SELECT p.product_id, h.name AS hotel_name, h.city,
                   p.title, p.selling_points, p.commission_rate
            FROM promotion_activities p
            JOIN hotels h ON h.hotel_id = p.hotel_id
            WHERE p.active = TRUE
            ORDER BY p.product_id
            """
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询推广活动失败: {exc}"})

    return _json({"status": "success", "count": len(rows), "data": rows})


@mcp.tool()
def get_promotion_materials(product_id: str, user_id: str = "") -> str:
    """根据活动 ID 获取专属推广链接与卖点。必须先调用 get_promotable_products 拿到 product_id。

    Args:
        product_id: 活动 ID，如 "P_SY_RESORT"。
        user_id: [系统注入] 当前用户 ID，用于生成**只属于他本人**的分销链接。
    """
    if not product_id:
        return _json({"status": "error", "message": "必须提供活动 ID product_id"})

    rows = _query(
        """
        SELECT p.product_id, p.title, p.selling_points, p.base_link, p.commission_rate,
               h.name AS hotel_name, h.city
        FROM promotion_activities p
        JOIN hotels h ON h.hotel_id = p.hotel_id
        WHERE p.product_id = %s AND p.active = TRUE
        """,
        (product_id,),
    )
    if not rows:
        return _json(
            {
                "status": "not_found",
                "message": f"没有找到活动 {product_id}，可先用 get_promotable_products 查看可推广列表。",
            }
        )

    promo = rows[0]
    exclusive_link = (
        f"{promo['base_link']}?inviter={user_id}&pid={promo['product_id']}"
        if user_id
        else promo["base_link"]
    )
    return _json(
        {
            "status": "success",
            "data": {
                "product_id": promo["product_id"],
                "hotel_name": promo["hotel_name"],
                "city": promo["city"],
                "activity_title": promo["title"],
                "selling_points": promo["selling_points"],
                "exclusive_link": exclusive_link,
                "commission_rate": promo["commission_rate"],
            },
        }
    )


@mcp.tool()
def generate_ai_poster(prompt: str) -> str:
    """调用千问文生图模型 qwen-image-2.0 生成竖版推广海报，返回图片地址。

    Args:
        prompt: 生图提示词，例如"三亚海景度假酒店，蓝天白云，亲子氛围，竖版海报，高级质感"。
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return _json({"status": "error", "message": "未配置 DASHSCOPE_API_KEY"})

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": "qwen-image-2.0",
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {
            "negative_prompt": "低分辨率，低画质，画面过饱和，文字模糊，构图混乱",
            "prompt_extend": True,
            "watermark": False,
            "size": "1536*2688",
        },
    }

    last_error = "生成失败"
    for attempt in range(1, 3):
        try:
            sys.stderr.write(f"[AI-POSTER] attempt={attempt} submit\n")
            res = requests.post(url, json=payload, headers=headers, timeout=120)
            data = res.json()
            image_url = (
                data.get("output", {})
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", [{}])[0]
                .get("image")
            )
            if res.status_code == 200 and image_url:
                return _json(
                    {
                        "status": "success",
                        "data": {
                            "poster_url": image_url,
                            "message": "海报生成成功（Qwen-Image）",
                            "request_id": data.get("request_id", ""),
                        },
                    }
                )
            last_error = data.get("message") or data.get("code") or f"HTTP {res.status_code}"
            sys.stderr.write(f"[AI-POSTER] attempt={attempt} failed: {last_error}\n")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            sys.stderr.write(f"[AI-POSTER] attempt={attempt} exception: {last_error}\n")

    return _json({"status": "error", "message": f"海报生成失败: {last_error}"})


# ==============================================================================
# 住中工单类工具（有业务副作用 → 放在 MCP server 里，便于集中审计）
#
# 身份类参数（store_id / session_id / room_no / stay_id）全部标 [系统注入]：
# 由 agent 侧的 ToolCallInterceptor 用运行时上下文**覆盖**模型传参，
# 模型既不需要也无法指定——它写什么都会被覆盖掉。
# ==============================================================================
@mcp.tool()
def create_work_order(
    category: str,
    intent: str = "",
    payload_json: str = "{}",
    store_id: str = "",
    session_id: str = "",
    room_no: str = "",
    stay_id: str = "",
    order_id: str = "",
    guest_verified: bool = True,
) -> str:
    """为客人创建一张服务工单（送物、报修、清洁、叫醒、延退、发票、失物等）。

    这是"把话变成事"的唯一入口：客人说"送两瓶水"，必须调用本工具建单，
    **不可以只在回答里说"已为您记录"**。

    Args:
        category: [模型生成] 必须从这个列表里选一个：
            SERVICE（送物/清洁/加床用品/叫醒）、MAINTENANCE（空调/热水/网络/电视等故障）、
            MAINTENANCE_SAFETY（漏水/漏电/燃气/门锁失灵等可能危及安全）、
            COMPLAINT（投诉）、CHECKOUT_REQUEST（延退/换房申请）、
            INVOICE（发票申请）、LOST_FOUND（失物招领）。
        intent: [模型生成] 一句话概括客人要办的事，例如"送两瓶矿泉水"。
        payload_json: [模型生成] 结构化细节的 JSON 字符串，例如
            {"item": "矿泉水", "qty": 2, "when": "尽快"} 或 {"fault": "空调不制冷"}。
        store_id: [系统注入] 客人所在门店，模型无法伪造。
        session_id: [系统注入] 会话号。
        room_no: [系统注入] 房间号（来自已验证的入住上下文）。
        stay_id: [系统注入] 入住上下文号（为空表示尚未通过房间级验证）。
        order_id: [系统注入] 订单号（若有）。
        guest_verified: [系统注入] 是否已通过身份验证。安全类工单允许未验证先派单。

    重要：**SLA 与承接部门由服务端按 category 决定**，你不需要也不应该自己承诺时间；
    工具返回的 due_at / sla_minutes 才是可以告诉客人的口径。
    """
    try:
        # 注意：不要在这里 `import json as _json`——会遮蔽模块顶部定义的 _json() 辅助函数，
        # 报错会变成 "'module' object is not callable"（踩过一次）。
        payload = json.loads(payload_json) if payload_json else {}
        if not isinstance(payload, dict):
            payload = {"raw": payload}
    except Exception:  # noqa: BLE001
        payload = {"raw": payload_json}

    # 门店上下文缺失时必须拒绝建单，而不是写一条 store_id 为空的脏数据。
    # 为什么加这条：我用进程内脚本单独调图时没带门店上下文，
    # 结果库里出现了几张"门店=空"的工单——脏数据看起来像正常工单，最难发现。
    if not store_id:
        return _json({"status": "error", "message": "缺少门店上下文（store_id），无法建单。请通过 /api/chat 发起请求。"})
    if not session_id:
        return _json({"status": "error", "message": "缺少会话上下文（session_id），无法建单。"})

    from service import work_order_service as wos

    try:
        # "已验证"必须同时有入住上下文：没有 stay_id 就不可能是验证过的客人。
        # 这样即使模型自称 guest_verified=true，也不会污染 P3 的鉴权度量。
        effective_verified = bool(guest_verified) and bool(stay_id)
        result = wos.create_work_order(
            store_id=store_id,
            session_id=session_id,
            category=category.strip().upper(),
            intent=intent,
            room_no=room_no or None,
            stay_id=stay_id or None,
            order_id=order_id or None,
            payload=payload,
            guest_verified=effective_verified,
            actor="agent",
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[work-order] create failed: {exc}\n")
        return _json({"status": "error", "message": f"建单失败: {exc}"})

    return _json(
        {
            "status": "success",
            "data": result,
            "hint": "请把工单号、承接部门与时限（sla_minutes / due_at）告诉客人，不要自行缩短或延长。",
        }
    )


@mcp.tool()
def query_work_order(work_order_id: str, store_id: str = "") -> str:
    """查询一张工单的当前状态与处理进度（含完整流转事件）。

    客人问"我的水什么时候送到""刚才报的修处理到哪了"时使用。

    Args:
        work_order_id: [模型生成] 工单号（形如 WO260916A1B2）；没有工单号时先问客人。
        store_id: [系统注入] 当前门店，用于租户隔离。
    """
    from service import work_order_service as wos

    detail = wos.get_work_order(work_order_id)
    if not detail:
        return _json({"status": "not_found", "message": "没有找到这个工单号，请确认后重试。"})
    if store_id and detail.get("store_id") and detail["store_id"] != store_id:
        # 跨门店查询统一按"查不到"处理，不泄露其他门店是否存在该工单
        return _json({"status": "not_found", "message": "没有找到这个工单号，请确认后重试。"})
    return _json({"status": "success", "data": detail})


@mcp.tool()
def query_lost_found(
    item_desc: str = "",
    room_no: str = "",
    days_back: int = 30,
    store_id: str = "",
) -> str:
    """查询酒店失物招领台账：客人离店后说"东西落在房间了"时用。

    返回结果区分**保管中**（STORED，可安排寄回）与**已归还**（RETURNED），
    已归还的不要重复安排，直接告诉客人"已寄回/已归还"。

    Args:
        item_desc: [模型生成] 物品描述关键词，例如"充电器""雨伞"；留空则返回全部。
        room_no: [模型生成] 客人住过的房间号（能提供就提供，能显著提高匹配率）。
        days_back: [模型生成] 回溯天数，默认 30。
        store_id: [系统注入] 当前门店，用于租户隔离。
    """
    sql = """
        SELECT item_id, room_no, found_date, item_desc, status, storage_location
          FROM lost_found_items
         WHERE store_id = %s
           AND found_date >= CURRENT_DATE - make_interval(days => %s)
    """
    params: list[Any] = [store_id or "", days_back]
    if room_no:
        sql += " AND room_no = %s"
        params.append(room_no)
    if item_desc:
        sql += " AND item_desc ILIKE '%%' || %s || '%%'"
        params.append(item_desc)
    sql += " ORDER BY found_date DESC LIMIT 10"
    try:
        rows = _query(sql, tuple(params))
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询失物台账失败: {exc}"})

    if not rows:
        return _json(
            {
                "status": "not_found",
                "message": "台账里没有匹配的物品。可以放宽条件（去掉物品描述或扩大回溯天数）再查一次；"
                           "如果仍然没有，如实告诉客人没有找到，并建议联系前台登记寻找。",
            }
        )
    for row in rows:
        row["status_label"] = {"STORED": "保管中", "RETURNED": "已归还", "DISCARDED": "已处理"}.get(
            row["status"], row["status"]
        )
    return _json({"status": "success", "count": len(rows), "data": rows})


@mcp.tool()
def query_session_work_orders(status: str = "", limit: int = 5, session_id: str = "") -> str:
    """查询**当前这位客人**在本会话里提出的所有工单及其进度。

    客人问"我刚才要的水送到哪了""我报的修处理到哪一步了"时**必须用本工具**，
    不需要问客人要工单号——工单号是系统内部标识，客人通常不知道也不该被要求知道。

    Args:
        status: [模型生成] 可选过滤：CREATED/ASSIGNED/ACCEPTED/DONE/FAILED/ESCALATED。
        limit: [模型生成] 返回条数上限，默认 5。
        session_id: [系统注入] 当前会话号，模型无法伪造也不需要指定。
    """
    if not session_id:
        return _json({"status": "error", "message": "缺少会话上下文，无法查询本会话工单。"})

    sql = """
        SELECT w.work_order_id, w.category, w.status, w.assignee_dept, w.priority,
               w.sla_minutes, w.due_at, w.accepted_at, w.done_at, w.on_time,
               w.guest_notified, w.intent, w.created_at
          FROM work_orders w
         WHERE w.session_id = %s
    """
    params: list[Any] = [session_id]
    if status:
        sql += " AND w.status = %s"
        params.append(status.strip().upper())
    sql += " ORDER BY w.created_at DESC LIMIT %s"
    params.append(max(1, min(int(limit or 5), 20)))
    try:
        rows = _query(sql, tuple(params))
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询工单失败: {exc}"})

    if not rows:
        return _json({"status": "not_found", "message": "本会话还没有工单记录。"})
    labels = {
        "CREATED": "已创建", "ASSIGNED": "已派单待接单", "ACCEPTED": "已接单处理中",
        "DONE": "已完成", "FAILED": "处理失败", "ESCALATED": "超时已升级", "CANCELLED": "已取消",
    }
    for row in rows:
        row["status_label"] = labels.get(row["status"], row["status"])
    return _json({"status": "success", "count": len(rows), "data": rows})


@mcp.tool()
def query_room_state(
    room_no: str = "",
    target_date: str = "",
    days: int = 3,
    store_id: str = "",
) -> str:
    """查询房态：**判断"能不能延迟退房 / 能不能换房"的依据**。

    这条工具是 v1 唯一多跳链路（延退/换房）的第二跳：
    订单 Agent 先确认这张订单，再由本工具回答"房间里还有没有下一位客人"。

    两类数据不在同一个权限域里——订单需要客人身份，房态不需要——
    所以它们必须由不同的步骤分别取得，而不是让模型一次猜出来。

    Args:
        room_no: [系统注入] 客人所在房间。
        target_date: [模型生成] 客人希望延到的那一天（YYYY-MM-DD）；留空则按"次日"判断。
        days: [模型生成] 向后看几天内的新预订，默认 3 天。
        store_id: [系统注入] 当前门店。
    """
    import datetime as _dt

    if not room_no:
        return _json({"status": "error", "message": "缺少房间号，无法查询房态。"})

    try:
        current = _query(
            """
            SELECT room_status, check_in_date, check_out_date, room_type_id
              FROM pms_room_state
             WHERE store_id = %s AND room_no = %s
            """,
            (store_id or "", room_no),
        )
        if not current:
            return _json({"status": "not_found", "message": f"没有查到 {room_no} 房的房态记录。"})
        row = current[0]
        room_type_id = row["room_type_id"]

        # 同房型在未来几天内是否有新客人（用订单近似：订单上没有房间号，只有房型）
        upcoming = _query(
            """
            SELECT min(o.check_in) AS next_arrival, count(*) AS booked
              FROM orders o JOIN room_types r ON r.room_id = o.room_id
             WHERE r.hotel_id = %s AND o.room_id = %s
               AND o.check_in BETWEEN CURRENT_DATE AND CURRENT_DATE + make_interval(days => %s)
               AND o.status IN ('PAID', 'COMPLETED')
            """,
            (store_id or "", room_type_id, days),
        )
        next_arrival = upcoming[0]["next_arrival"] if upcoming else None
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "message": f"查询房态失败: {exc}"})

    requested = None
    if target_date:
        try:
            requested = _dt.date.fromisoformat(target_date[:10])
        except Exception:  # noqa: BLE001
            requested = None
    if requested is None:
        base = row["check_out_date"] or _dt.date.today()
        requested = base + _dt.timedelta(days=1)

    # 判定规则（写成一条明确规则，而不是让模型自由发挥）：
    #   次日没有新客人 → 房态上可以延；有 → 需前台按当日运营确认（可能要换房）
    blocked_by = next_arrival if (next_arrival and next_arrival <= requested) else None
    can_extend = blocked_by is None
    return _json(
        {
            "status": "success",
            "data": {
                "room_no": room_no,
                "room_status": row["room_status"],
                "current_check_in": row["check_in_date"],
                "current_check_out": row["check_out_date"],
                "room_type_id": room_type_id,
                "requested_date": requested.isoformat(),
                "next_arrival": next_arrival,
                "can_extend": can_extend,
                "blocked_by_arrival": blocked_by,
                "conclusion": (
                    f"房态上可以延至 {requested.isoformat()}（此后 {days} 天内该房型无新预订）"
                    if can_extend
                    else f"{blocked_by} 该房型已有新预订，能否延退需前台按当日运营确认（可能需要换房）"
                ),
                "note": "这是**房态判断**，不是批准承诺；最终以后台确认为准。",
            },
        }
    )


if __name__ == "__main__":
    # 子进程的 stdout 是 MCP 协议通道，日志一律走 stderr；
    # 并且显式把编码设成 UTF-8 + replace，避免中文/emoji 在 GBK 环境下抛异常。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    sys.stderr.write("[MCP] hotel_service MCP Server 启动（stdio）\n")
    mcp.run()
