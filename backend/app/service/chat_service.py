import asyncio
import json
import sys
import os
import time
from datetime import datetime, timezone

# 初始化 Agent 和 Graph
AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

from core.workflow.graph_manager import AgentGraphManager
from core.memory.memory_manager import MemoryManager
from core.runtime import reset_runtime_context, set_runtime_context
from infra.cache import semantic_cache
from infra.health import current_degraded_paths
from infra.observability import ensure_tables, record_answer, record_cache_candidate
from service import session_service, work_order_service

# Global variables for graph and memory
graph = None
memory = None
_extract_llm = None


def _get_extract_llm():
    """给长期偏好抽取用的模型（与业务 Agent 用同一个）。"""
    global _extract_llm
    if _extract_llm is None:
        from langchain_openai import ChatOpenAI

        from config import get_settings

        settings = get_settings()
        _extract_llm = ChatOpenAI(
            api_key=settings.dashscope_api_key,
            model=settings.model,
            base_url=settings.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0.0,
        )
    return _extract_llm

async def init_agent_system():
    global graph, memory
    if graph is None:
        print("🚀 初始化 Multi-Agent 图编排...")
        graph_manager = AgentGraphManager()
        graph = graph_manager.build_graph()
        
        print("🧠 初始化 Memory 系统...")
        from config import get_settings
        settings = get_settings()
        memory = MemoryManager(
            redis_url=settings.redis_url,
            redis_ttl=settings.redis_ttl,
            postgres_dsn=settings.postgres_dsn,
            milvus_host=settings.milvus_host,
            milvus_port=settings.milvus_port,
            milvus_uri=settings.milvus_uri,
            milvus_api_key=settings.milvus_api_key,
            embedding_api_key=settings.dashscope_api_key,
        )
        await memory.initialize()
        await semantic_cache.initialize()
        # 治理表（埋点 / 缓存候选 / 工单复核）必须在启动时就建好，
        # 否则写入会静默失败——这个坑踩过一次：10 轮对话一条日志都没落库。
        await asyncio.to_thread(ensure_tables)
        print("✅ Agent 系统初始化完成！")

async def _extract_memory_context(user_id: str, session_id: str, query: str) -> str:
    context_parts = []
    if memory and memory.short_term.available:
        history = await memory.short_term.get_messages(user_id, session_id)
        if history:
            recent_history = history[-10:] if len(history) > 10 else history
            context_parts.append("【近期对话历史】:")
            for msg in recent_history:
                role = "User" if msg["role"] == "user" else "Assistant"
                context_parts.append(f"{role}: {msg['content']}")
    
    # 会员级偏好：走 PG 表（不再走向量库，理由见 core/memory/member_store.py 顶部）。
    # 它是"锦上添花"——读不到就不注入，绝不影响这一轮回答。
    try:
        from core.memory.member_store import load_preferences

        prefs = await asyncio.to_thread(load_preferences, user_id)
        if prefs:
            context_parts.append("\n【用户长期偏好/背景】:")
            for p in prefs:
                context_parts.append(f"- {p}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ [memory] 会员偏好读取失败（不影响回答）：{exc}")
                
    return "\n".join(context_parts)

async def stream_chat(
    query: str,
    user_id: str,
    session_id: str,
    store_id: str | None = None,
    room_no: str | None = None,
):
    started = time.perf_counter()
    # 身份与门店由系统注入（模型无法伪造）：决定本次请求能看到哪些数据。
    from config import get_settings

    resolved_store = store_id or get_settings().default_store_id

    # 会话上下文（阶段 / 鉴权等级 / 入住上下文）：跨轮持久化，不属于图的一次推理
    session = await asyncio.to_thread(
        session_service.get_or_create_session,
        session_id, store_id=resolved_store, room_no=room_no,
    )
    # 阶段是创建时的快照，可能已经过时（客人退房了）——每轮先刷新一次
    await asyncio.to_thread(session_service.refresh_stage, session_id)
    session = await asyncio.to_thread(
        session_service.get_or_create_session,
        session_id, store_id=resolved_store,
    )

    prefix = ""
    # 正在收集凭据 → 这一轮完全交给会话层，不让模型参与鉴权流程
    if await asyncio.to_thread(session_service.pending_auth_state, session_id):
        outcome = await asyncio.to_thread(
            session_service.advance_auth, session_id, query, store_id=resolved_store
        )
        status = outcome.get("status")
        if status != "OK":
            reply = outcome.get("prompt") or outcome.get("message") or ""
            async for event in _emit_simple(
                reply, {"agent": "auth_guard", "auth_status": status, "cached": False},
                user_id, session_id, resolved_store, started,
            ):
                yield event
            return
        # 身份确认通过 → 接着把**原来的问题**答完，不让客人重说一遍
        prefix = outcome.get("message", "")
        query = outcome.get("original_query") or query
        session = await asyncio.to_thread(
            session_service.get_or_create_session, session_id, store_id=resolved_store
        )

    ctx_token = set_runtime_context(
        store_id=resolved_store,
        user_id=user_id,
        session_id=session_id,
        room_no=session.get("room_no"),
        stay_id=session.get("stay_id"),
    )
    try:
        async for event in _stream_chat_inner(
            query, user_id, session_id, resolved_store, started, session, prefix
        ):
            yield event
    finally:
        reset_runtime_context(ctx_token)


 
# 兜底建单：只对"本该产出一张单"的三个 Agent 生效
_FALLBACK_CATEGORY_AGENTS = {"room_service_agent", "maintenance_agent", "departure_agent"}

# 判定"Agent 声称**本轮**已经办了"的措辞。
#
# 踩过的坑：第一版把"工单号"也算进去，结果客人问"我刚才那件事的工单号是多少"，
# Agent 正确地查了旧工单并回答，兜底却因为回复里出现"工单号"三个字**又建了一张重复单**。
# 兜底机制本身也需要判据——宁可少兜一次，也不要凭空造单。
_CLAIM_WORDS = (
    "已创建工单", "已为您创建", "已建单", "已生成工单",
    "已提交", "已为您提交", "已安排", "已为您安排",
    "已记录", "已为您办理", "已登记",
)


def _infer_category(agent: str, query: str) -> str:
    """兜底建单时判断类别：用与路由同一套信号词，避免两处口径不一致。"""
    from agents.intent_router import (
        DEPARTURE_INVOICE_ACTIONS,
        LOST_FOUND_SIGNALS,
        MAINTENANCE_SAFETY_SIGNALS,
    )

    if agent == "departure_agent":
        if any(word in query for word in DEPARTURE_INVOICE_ACTIONS):
            return "INVOICE"
        if any(word in query for word in LOST_FOUND_SIGNALS):
            return "LOST_FOUND"
        return "LOST_FOUND"
    if agent == "maintenance_agent":
        if any(word in query for word in MAINTENANCE_SAFETY_SIGNALS):
            return "MAINTENANCE_SAFETY"
        return "MAINTENANCE"
    return "SERVICE"


def _fallback_create_work_order(
    agent: str, query: str, store_id: str, session_id: str, session: dict, reply: str,
) -> dict | None:
    """服务端兜底建单。失败返回 None（绝不因为兜底失败而影响回答）。"""
    try:
        result = work_order_service.create_work_order(
            store_id=store_id,
            session_id=session_id,
            category=_infer_category(agent, query),
            intent=query[:200],
            room_no=session.get("room_no"),
            stay_id=session.get("stay_id"),
            order_id=session.get("order_id"),
            payload={
                "fallback": True,
                "reason": "agent_claimed_without_creating",
                "agent": agent,
                "reply_excerpt": reply[:300],
            },
            guest_verified=bool(session.get("stay_id")) or session.get("auth_level", 0) >= 2,
            actor="fallback",
        )
        return {
            "work_order_id": result["work_order_id"],
            "category": result["category"],
            "status": result["status"],
            "assignee_dept": result["assignee_dept"],
            "priority": result["priority"],
            "sla_minutes": result["sla_minutes"],
            "due_at": result["due_at"],
            "fallback": True,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[fallback] 兜底建单失败：{exc}")
        return None


def _stream_params() -> tuple[int, float]:
    """流式分帧参数。

    这里的"打字机效果"是我们自己加的，**它会直接变成响应时间**：
    实测一份 1060 字的政策类回答，按 5 字/帧 + 20ms 停顿 = **4.2 秒纯等待**，
    把这一题的耗时从 ~14 秒推到 26 秒。
    所以默认改成"帧更大、停顿更短"，并保留环境变量以便需要时调回去。
    """
    try:
        chunk = max(1, int(os.getenv("STREAM_CHUNK_CHARS", "24")))
        delay = max(0.0, float(os.getenv("STREAM_CHUNK_DELAY", "0.005")))
    except Exception:  # noqa: BLE001
        return 24, 0.005
    return chunk, delay


async def _emit_simple(reply: str, meta: dict, user_id: str, session_id: str,
                       store_id: str, started: float):
    """非图链路的一句话回复（鉴权引导 / 锁定 / 求助），同样要落埋点。"""
    yield f"data: {json.dumps({'meta': meta}, ensure_ascii=False)}\n\n"
    chunk_size, chunk_delay = _stream_params()
    for i in range(0, len(reply), chunk_size):
        yield f"data: {json.dumps({'content': reply[i:i + chunk_size]}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(chunk_delay)
    await asyncio.to_thread(
        record_answer,
        {
            "session_id": session_id,
            "user_id": user_id,
            "query": "(凭据收集)",
            "store_id": store_id,
            "route": "auth_guard",
            "tier": "auth",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "degraded_paths": current_degraded_paths(),
        },
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    yield f"data: {json.dumps({'done': True, 'elapsed_ms': elapsed})}\n\n"


async def _stream_chat_inner(query: str, user_id: str, session_id: str, store_id: str,
                             started: float, session: dict, prefix: str = ""):
    # 埋点用：路由与档位信息（缓存命中时为空）
    trace: dict = {}
    work_order: dict | None = None
    turn_started = datetime.now(timezone.utc)
    # 缓存查询与记忆取用互不依赖，并行做（两者各自都要一次外部调用，串行就是白等）
    cache_hit, mem_context = await asyncio.gather(
        semantic_cache.get_cache(query, user_id, store_id),
        _extract_memory_context(user_id, session_id, query),
    )
    if cache_hit:
        response_text = cache_hit["answer"]
        meta = {
            "cached": True,
            "level": cache_hit["level"],
            "score": round(float(cache_hit.get("raw_score", cache_hit["distance"])), 4),
            "matched_question": cache_hit["matched_question"],
            "agent": "semantic_cache",
        }
        print(
            f"⚡ 语义缓存命中: {cache_hit['level']} distance={cache_hit['distance']:.4f} matched='{cache_hit['matched_question']}'"
        )
    else:
        print("🏃 进入 Agent 工作流推理...")
        state = {
            "messages": [("user", query)],
            "user_id": user_id,
            "session_id": session_id,
            "memory_context": mem_context,
            "next_agent": "",
            # 会话级上下文交给鉴权门与工具拦截器使用（阶段 / 鉴权等级 / 房间）
            "metadata": {
                "store_id": store_id,
                "session_id": session_id,
                "room_no": session.get("room_no"),
                "stay_id": session.get("stay_id"),
                "stage": session.get("stage"),
                "auth_level": session.get("auth_level", 0),
                "order_id": session.get("order_id"),
            },
        }
        # 身份与门店同时通过 config 传给 MCP 拦截器（模型写什么都会被覆盖）
        config = {
            "configurable": {
                "user_id": user_id,
                "store_id": store_id,
                "session_id": session_id,
                "room_no": session.get("room_no"),
                "stay_id": session.get("stay_id"),
                # 订单号也要注入：客人刚用订单号完成验证，再问一遍就是"重复描述"
                "order_id": session.get("order_id"),
            }
        }
        try:
            result = await asyncio.to_thread(asyncio.run, graph.ainvoke(state, config=config)) if not asyncio.iscoroutinefunction(graph.ainvoke) else await graph.ainvoke(state, config=config)
            response_text = result["messages"][-1].content
            result_meta = result.get("metadata", {}) if isinstance(result, dict) else {}
        except Exception as exc:  # noqa: BLE001
            # 编排层任何一环抛错，都不能让客人收到**空白回答**——那是静默失败。
            # 这里给一句诚实的话 + 明确的替代路径，并把原因记进埋点。
            print(f"❌ [graph] 编排执行失败：{type(exc).__name__}: {exc}")
            response_text = (
                "抱歉，刚才这个问题我没能处理完成（服务暂时不稳定）。\n\n"
                "您可以再说一次，或者直接联系前台/总机，我也可以帮您转接。"
            )
            result_meta = {"target_agent": "error_fallback", "graph_error": type(exc).__name__}
            # 下面的分支会读 result.get("next_agent")，所以这里必须给一个空字典，
            # 否则异常路径会再抛一个 NameError（"异常处理里再抛异常"是经典坑）。
            result = {}

        if result_meta.get("auth_required"):
            # 鉴权门拦下：发起凭据收集，并用第一个槽位的提示替换回答
            outcome = await asyncio.to_thread(
                session_service.begin_auth,
                session_id,
                required_level=int(result_meta["auth_required"]),
                original_query=query,
            )
            response_text = outcome["prompt"]
            routed = "auth_guard"
            meta = {
                "cached": False,
                "agent": "auth_guard",
                "auth_required": int(result_meta["auth_required"]),
                # 让评测与前端都能看到"本来该由谁处理"——鉴权门只是拦了一下，不是改了路由
                "auth_target_agent": result_meta.get("auth_target_agent"),
            }
            trace = {"route": "auth_guard", "tier": "auth"}
        else:
            # 注意：handoff / clarify 节点执行完会把 next_agent 清空，
            # 如果只取 next_agent，就会把"转人工"误记成 policy_agent（第一轮评测踩到）。
            # 真正的路由决策在 orchestrator 写入的 target_agent 里。
            routed = result.get("next_agent") or result_meta.get("target_agent") or "policy_agent"
            meta = {"cached": False, "agent": routed}
            trace = {
                "route": routed,
                "route_source": result_meta.get("route_source"),
                "confidence": result_meta.get("route_confidence"),
                "tier": result_meta.get("service_tier"),
                "ticket_id": result_meta.get("ticket_id"),
                "required_auth": result_meta.get("required_auth"),
                "auth_passed": result_meta.get("auth_passed"),
            }
            # 工单回捞：Agent 只把文本交回主图，工单号必须由服务层查出来
            work_order = await asyncio.to_thread(
                work_order_service.latest_for_session, session_id, turn_started
            )
            # 兜底：Agent **说**已经办了，但实际没建单（第一轮评测起反复出现的失效模式）。
            # 提示词拦不住它就换结构性约束——只要回复里声称"已创建/已安排/已提交"，
            # 服务端就补一张单，并把这笔记为 fallback（这个比例本身就是产品指标）。
            if work_order is None and routed in _FALLBACK_CATEGORY_AGENTS:
                if any(word in response_text for word in _CLAIM_WORDS):
                    work_order = await asyncio.to_thread(
                        _fallback_create_work_order,
                        routed, query, store_id, session_id, session, response_text,
                    )
                    if work_order:
                        meta["work_order_fallback"] = True
                        print(f"🧷 [兜底建单] {routed} 声称已办理但未建单 → 已补 {work_order['work_order_id']}")
        # 建议 5：把没被缓存覆盖的**正常业务问题**登记进候选池（只登记，不自动回写）。
        # 明确排除安全试探、投诉、越权、无归属这几类：
        # 它们的答案本来就不该被复用（投诉话术、安全拒绝话术都不是"可缓存的答案"）。
        allowed, _reason = semantic_cache.semantic_allowed(query)
        business_routes = {
            "policy_agent",
            "order_agent",
            "recommendation_agent",
            "room_service_agent",
            "maintenance_agent",
        }
        if allowed and routed in business_routes and trace.get("tier") in ("direct", "confirm"):
            await asyncio.to_thread(record_cache_candidate, query)

    # 身份刚确认通过：先用一句话衔接，再接着把原来的问题答完
    if prefix:
        response_text = f"{prefix}\n\n{response_text}"

    # 先发一帧元信息：前端用它显示"缓存直返 / 由哪个 Agent 处理"
    yield f"data: {json.dumps({'meta': meta}, ensure_ascii=False)}\n\n"

    # 工单事件单独一帧：前端据此渲染"工单号 + 承接部门 + 承诺时限"卡片。
    # 产品承诺的落点在这里，不能只藏在正文里。
    if work_order:
        yield f"data: {json.dumps({'work_order': work_order}, ensure_ascii=False)}\n\n"
    
    # 保存短时记忆
    if memory and memory.short_term.available:
        turn = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": response_text},
        ]
        await memory.save_conversation(user_id, session_id, turn)

        # 会员级偏好抽取：每 5 轮后台跑一次。
        #
        # 两处与参考项目不同：
        #   ① 写入目标是 PG 的 member_preferences，不再是 Milvus（见 member_store 顶部说明）；
        #   ② 抽取与写入都做"入住级信息"过滤——门店/房间/订单/日期一律不许进来，
        #      否则下一个会话会用它覆盖系统注入的门店（第 15 轮实测到的那个 bug）。
        history = await memory.short_term.get_messages(user_id, session_id)
        turns = len(history) // 2
        if turns and turns % 5 == 0:
            try:
                from core.memory.member_store import save_preferences
                from core.memory.preference_extractor import PreferenceExtractor

                conversation = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
                extractor = PreferenceExtractor(llm=_get_extract_llm())
                items = await extractor.extract(conversation_text=conversation)
                saved = await asyncio.to_thread(save_preferences, user_id, items, session_id)
                print(f"🧠 [会员记忆] 第 {turns} 轮触发抽取，写入 {saved} 条（候选 {len(items)} 条）")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ 会员偏好抽取失败（不影响回答）：{exc}")
        
    # 流式返回大模型结果（分帧参数见 _stream_params 的说明）
    chunk_size, chunk_delay = _stream_params()
    for i in range(0, len(response_text), chunk_size):
        chunk = response_text[i:i+chunk_size]
        yield f"data: {json.dumps({'content': chunk})}\n\n"
        await asyncio.sleep(chunk_delay)
        
    # 建议 6：每条回答落一行日志（失败不影响用户体验）
    # 注意必须写在最后一个 yield **之前**：客户端收到 done 就断开，
    # yield 之后的代码在生成器被关闭时不会执行（这个坑也踩过一次）。
    await asyncio.to_thread(
        record_answer,
        {
            "session_id": session_id,
            "user_id": user_id,
            "query": query,
            "store_id": store_id,
            "route": trace.get("route") or ("semantic_cache" if cache_hit else None),
            "route_source": trace.get("route_source") or ("cache" if cache_hit else None),
            "confidence": trace.get("confidence"),
            "tier": trace.get("tier") or ("direct" if cache_hit else None),
            "cache_hit": bool(cache_hit),
            "cache_level": cache_hit["level"] if cache_hit else None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "ticket_id": trace.get("ticket_id"),
            "degraded_paths": current_degraded_paths(),
            "stage": session.get("stage"),
            "required_auth": trace.get("required_auth"),
            "auth_passed": trace.get("auth_passed"),
            "work_order_id": work_order["work_order_id"] if work_order else None,
        },
    )
    yield f"data: {json.dumps({'done': True, 'elapsed_ms': int((time.perf_counter() - started) * 1000)})}\n\n"
