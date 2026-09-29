# 05 · TDD · 随行管家（基于参考项目二次开发）

> 上游：[04-PRD](04-PRD.md) · [03 技术选型](03-技术选型与关键决定的后果.md) · [00 §4.4 三色清单](00-需求日志.md) · [00 §5 Agent 体系](00-需求日志.md)
>
> 本文回答四个问题：**继承什么、改什么、新建什么、每个东西放在哪。**

---

## 0. 这一版是什么

不是"从零写一套"，也不是"把云平台客服改个名字"。准确表述是：

> **在前段（订单/政策/推荐）继承参考项目已验证的实现，在住中段（工单闭环）与维度（门店、阶段、三级记忆）上从零新建。**

参考项目的资产与缺口已在 [00 §4.4](00-需求日志.md) 逐文件登记，本文只做落地设计。

---

## 1. 基线与复用

### 1.1 技术栈（定稿）

| 层 | 选型 | 状态 |
|---|---|---|
| 编排 | LangGraph 1.x（StateGraph + 条件边 + 共享 state） | 沿用 |
| 大模型 | DashScope qwen-plus（OpenAI 兼容层） | 沿用 |
| Embedding | DashScope text-embedding-v2（1536 维） | 沿用（**不可逆约束**） |
| 向量库 | **Zilliz Cloud 托管** | 保留（你的决定，[03 变动 4](03-技术选型与关键决定的后果.md)） |
| 业务库 | PostgreSQL 18.6（本机，库 `hotel_service`） | 沿用 + 扩展 |
| 短期记忆 | PostgreSQL（Redis 可选加速） | 切默认 |
| 图谱 | Neo4j | 默认关闭 |
| 工具层 | MCP（FastMCP，stdio） | 沿用 |
| 后端 | FastAPI + SSE | 沿用 + 扩展 |
| 前端 | Vue 3 + Element Plus | 沿用 + 新增两视图 |

### 1.2 直接复用（不改逻辑）

`core/workflow/state.py` · `core/mcp/mcp_manager.py` · `core/memory/short_term.py` ·
`tools/graph_tool.py` · `router/chat.py` · `schemas/chat.py` · `app_config/settings.py` ·
前端 SSE 解析与聊天骨架

### 1.3 必须改造（改动点已定位）

| 文件 | 改动 |
|---|---|
| `infra/cache.py` | 过滤器与写入加 `store_id`；集合改 `qa_semantic_cache_v2`；新增门店级失效 |
| `agents/intent_router.py` | 加入住中/离店意图词表；输出 `stage` 与 `required_auth`；投诉从硬触发改为双通道 |
| `agents/handoff_agent.py` | 工单表换 `work_orders`；四类接收部门；会话标记 `needs_human` |
| `agents/{orchestrator,product,billing,recommendation}.py` | 提示词与知识源按酒店三阶段改写 |
| `mcp_servers/hotel_service_server.py` | 保留订单 4 工具；新增住中/鉴权/门店工具；注入 `store_id`+`room_no` |
| `core/memory/{memory_manager,long_term,preference_extractor}.py` | 三级记忆；偏好从向量改 PG 表 |
| `infra/observability.py` | `answer_log` 加 `store_id/stage/required_auth/auth_passed/degraded_paths/work_order_id` |
| `mock_data/*` `database/*` | 换成酒店三层知识 + 门店 + 房态 + 工单 |

### 1.4 新建

住中工单域 · `room_service_agent` · `maintenance_agent` · `departure_agent` ·
`auth_guard` · 阶段识别 · 三级记忆的入住层 · 坐席台 · 健康自检与一键启动 · 评测集与运行脚本

---

## 2. 二次开发的三条原则

1. **场景绑定的地方必须改写，不能改名了事**——判据是：它引用的知识、数据、权限是否变了。
2. **通用的地方一行不改**——`mcp_manager` 这类与业务无关的代码不做"顺手优化"，避免引入新 bug。
3. **对外口径**：统一表述"基于课程项目二次开发"，并明确列出自研部分（见 [00 §4.2 变动 2](00-需求日志.md)）。

---

## 3. 架构总览

```
┌─ 住客端（Vue3）────────────────┐   ┌─ 坐席台（Vue3）──────────────┐
│ 扫码带 store_id/room_no         │   │ 工单池 / 接单 / 完成 / 升级   │
└───────────────┬────────────────┘   └──────────────┬──────────────┘
                │ POST /api/chat (SSE)               │ REST
┌───────────────▼────────────────────────────────────▼──────────────┐
│ FastAPI  app_main.py                                               │
│  router/chat.py · router/work_orders.py · router/metrics.py         │
│  router/health.py · router/session.py                              │
│  ── service/chat_service.py（缓存优先 → 编排 → 埋点）                │
│  ── infra/cache.py（两级缓存 + store_id + 版本绑定）                 │
│  ── infra/health.py（依赖自检，主链路依赖显式报警）                   │
│  ── infra/observability.py（answer_log / work_order_events 埋点）    │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ LangGraph 编排（agent/core/workflow/graph_manager.py）              │
│                                                                    │
│  START → orchestrator → auth_guard → ┬ policy_agent                │
│         （意图+阶段+所需鉴权）        ├ order_agent ⇄ room_state     │
│                                      ├ recommendation_agent        │
│                                      ├ room_service_agent          │
│                                      ├ maintenance_agent           │
│                                      ├ departure_agent             │
│                                      ├ clarify_agent（反问/澄清）    │
│                                      └ handoff_agent（转人工/投诉）  │
│                                                                    │
│  MemoryManager：会话级(PG) / 入住级(stay_context) / 会员级(PG 表)    │
└───────────────┬────────────────────────────────────────────────────┘
                │ MCP(stdio)
┌───────────────▼────────────────────────────────────────────────────┐
│ hotel_service MCP Server                                           │
│  订单域（沿用）：query_user_orders / query_order_detail /            │
│                 search_hotel_catalog                                │
│  鉴权域（新）：verify_stay / verify_order                           │
│  门店域（新）：query_store_facts                                    │
│  知识域（改）：query_policy_docs(query, store_id)                    │
│  住中域（新）：create_work_order / query_work_order / query_room_state│
│  离店域（新）：create_invoice_request / query_lost_found             │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ PostgreSQL `hotel_service`  +  Zilliz（知识向量 / 语义缓存）          │
└────────────────────────────────────────────────────────────────────┘
```

**数据流关键顺序**：先查缓存（缓存键含 `store_id`）→ 未命中则取记忆上下文 →
`orchestrator` 判意图+阶段+所需鉴权 → `auth_guard` 放行或反问 → 业务 Agent 调 MCP 工具 →
产出工单/申请单/答案 → 回执与埋点。

---

## 4. 目录结构

```
hotel-agent/
├── 00-需求日志.md … 07-本地运行与自检手册.md
├── backend/
│   ├── app/
│   │   ├── app_main.py
│   │   ├── app_config/settings.py
│   │   ├── infra/         cache.py · observability.py · health.py
│   │   ├── router/        chat.py · work_orders.py · metrics.py · health.py · session.py
│   │   ├── schemas/       chat.py · work_order.py · session.py
│   │   └── service/       chat_service.py · work_order_service.py
│   ├── agent/
│   │   ├── agents/        orchestrator · auth_guard · policy/order/recommendation/
│   │   │                  room_service/maintenance/departure · clarify · handoff
│   │   ├── core/          workflow(graph_manager,state) · memory(3 级) · mcp · graph(关闭)
│   │   ├── tools/         vector_tool · catalog_tool · store_tool
│   │   ├── mcp_servers/   hotel_service_server.py
│   │   └── config/        settings.py · mcp_servers.json
│   ├── database/          schema.sql · seed_stores.sql · migrate_from_reference.sql
│   ├── mock_data/         集团政策/*.md · 门店事实/HT001.md HT002.md · 房态/*.csv
│   └── run.py
├── web/                   index.html · src/{guest,agent}（住客端 / 坐席台）
├── 工具/                  启动本地.ps1 · 自检.py
└── 评测/                  评测集/{A..E}*.csv · 运行评测.py · 评测报告.md
```

---

## 5. 关键设计

### 5.1 阶段识别（Guest Journey Stage）

| 优先级 | 信号 | 判定 |
|---|---|---|
| 1（最强） | 上下文 | 扫码带 `room_no`+`store_id` → `IN`；带 `order_id` → 按数据判 |
| 2 | 数据 | 按订单日期：`today < check_in` → `PRE`；`check_in ≤ today ≤ check_out` → `IN`；`today > check_out` → `POST` |
| 3（最弱） | 语言 | "我住 302" / "我下周入住" / "我上周住的" |

**两条实现要点**：

1. **阶段是会话级、动作是意图级**（[00 §4.5](00-需求日志.md)）。住中的客人问"下个月有什么房型"，
   照常走 `recommendation_agent`（L0），只是不能帮他改这张订单。
2. **阶段一旦被数据确认，不能被单句话改写**——"我下周入住"可能是住中客人在问下次预订。
   语言信号只在会话尚无 `stay_id` 时生效。

### 5.2 鉴权门 `auth_guard` 与三级凭据

```
orchestrator 输出 required_auth ∈ {L0, L1, L2}
        ↓
auth_guard 比对 session.auth_level
        ↓
 满足 → 放行到业务 Agent
 不满足 → clarify_agent 反问最小必要信息
        ↓ 连续失败 3 次
      handoff_agent（转前台，避免房号枚举攻击）
```

| 等级 | 凭据 | 用于 | 备注 |
|---|---|---|---|
| L0 | 无 | 门店事实、政策、房型推荐 | 不做鉴权拦截 |
| L1 | 房号 + 姓氏 + 离店日 | 住中服务、报修 | 房号是**弱凭据**，所以配姓氏验证 |
| L2 | 订单号 + 手机后四位 | 订单、账务、发票、失物 | 境外客人无国内手机号时的备用：订单号 + 姓氏 |

**隐私与安全约定（必须写进代码注释）**：

- 统一失败话术：**不区分"房号不存在"与"姓氏不匹配"**，避免枚举攻击；
- `auth_attempts` 只存提交值的**哈希**与失败原因，不存明文；
- 日志与埋点禁止出现"房号 + 姓名"组合，房号在日志中脱敏为 `30*`；
- **例外**：安全类报修（漏水/漏电/燃气/门锁）允许"**先派单、后验证**"——
  延误的代价高于误派的代价。

### 5.3 多 Agent 编排

```python
# graph_manager.py 关键边（示意）
builder.add_edge(START, "orchestrator")
builder.add_conditional_edges("orchestrator", self._route_to_auth_guard)   # 意图+阶段+所需鉴权
builder.add_conditional_edges("auth_guard", self._auth_outcome, {
    "pass": <业务 agent>,          # 由 metadata.target_agent 决定
    "auth_required": "clarify_agent",
    "denied": "handoff_agent",
})
# 唯一的多跳链路：延退 / 换房申请
builder.add_conditional_edges("order_agent", self._need_room_state, {
    "room_state": "room_state_node",
    END: END,
})
builder.add_edge("room_state_node", "order_agent")   # 回到 order_agent 生成申请单
```

**防环**：`metadata.hop_count` 上限 `HOP_MAX=2`，超限直接产出申请单并提示"需前台确认"。

### 5.4 工单域（本项目的心脏）

**状态机**

```
CREATED ──assign──→ ASSIGNED ──accept──→ ACCEPTED ──done──→ DONE
   │                    │                    │
   │                    └──timeout───────────┴──→ ESCALATED ──reassign──→ ASSIGNED
   │
   └──cancel──→ CANCELLED        ACCEPTED ──fail──→ FAILED ──retry──→ ASSIGNED
```

| 类别 | 默认承接部门 | 默认 SLA | 升级对象 |
|---|---|---|---|
| `SERVICE`（送物/清洁/加床用品/叫醒） | 客房部 | 20 分钟 | 值班经理 |
| `MAINTENANCE`（普通报修） | 工程部 | 30 分钟到场 | 值班经理 |
| `MAINTENANCE_SAFETY`（漏水/漏电/燃气/门锁） | 工程部 + 值班经理 | 10 分钟 | 店长（P0） |
| `COMPLAINT`（投诉，双通道） | 前台 / 值班经理 | 15 分钟响应 | 值班经理 |
| `CHECKOUT_REQUEST`（延退/换房申请） | 前台 | 30 分钟 | 值班经理 |
| `INVOICE`（发票申请） | 前台 | 24 小时 | 值班经理 |
| `LOST_FOUND`（失物） | 客房部 | 4 小时 | 值班经理 |

**两条设计约束**：

1. `assignee_type` 取 `HUMAN / DEVICE` —— 现在只写 `HUMAN`，
   但字段先留出来，将来接送物机器人（[01 §2 第 5 类](01-现状与竞品.md)）不需要改表；
2. `work_order_events` 记录每一次流转 —— [02 P1](02-问题定义与优先级.md) 的"失败归因分布"
   （未建单/未接/未完成/未回执）就是靠它算出来的。

### 5.5 知识分层与检索

| 层 | 存在哪 | 谁维护 | 检索时的过滤 |
|---|---|---|---|
| 集团标准（会员权益、发票规则、退改总则） | Zilliz 集合 `hotel_knowledge_v2`（`store_id = NULL`）+ 本地文本快照 | 集团 | 始终参与 |
| 门店事实（早餐时间、WiFi、泳池、停车） | PG `store_facts`（结构化 KV） | 门店 | 按 `store_id` 精确取 |
| 门店知识（该店特有说明） | Zilliz（`store_id = HT001`）+ 本地快照 | 门店 | 按 `store_id` 过滤 |

**检索顺序**：先取结构化门店事实（准确、便宜）→ 再走向量检索集团政策与门店知识（模糊、互补）。
两者冲突时**以门店事实为准**（因为它是结构化且明确归属门店的）。

### 5.6 缓存（两级 + 门店 + 版本 + 失效）

| 项 | 参考项目 | 本项目 |
|---|---|---|
| 命中顺序 | 用户级精确 → 公共级精确 → 语义最近 1 条 | 保留，但**公共级按 `store_id` 分区** |
| 集合 | `qa_semantic_cache` | `qa_semantic_cache_v2`（schema 变了，必须重建） |
| 黑名单 | 个人指代 + 高风险主题 | 保留，**新增**"门店相关且刚刚变更"的动态失效 |
| 版本绑定 | `policy_version` | 保留，**新增** `store_facts.updated_at` 参与失效判定 |

**门店级失效的实现**：门店事实更新时，把 `store_facts.updated_at` 写进 `policy_version` 的派生值
（如 `HOTEL-2026.1#HT001@2026-09-16T10:00`），该店相关缓存条目自然失效。

### 5.7 三级记忆

| 级 | 存储 | 生命周期 | 内容 |
|---|---|---|---|
| 会话级 | PG `short_term_memory`（Redis 可选） | TTL 1800s，超 10 条裁剪 | 近期对话 |
| 入住级 | PG `stay_context` + `guest_sessions` | `check_out` + `STAY_GRACE_HOURS`（默认 2h）**自动失效** | 房号、姓氏、订单号、离店日、阶段 |
| 会员级 | PG `member_preferences` | 跨入住，但更窄 | 无烟、楼层、枕头、出差/度假偏好 |

> **会员偏好从向量改 PG 表**：偏好条目少（每人 < 20 条）且结构化，
> 向量检索的收益很小。Zilliz 只承担知识检索与语义缓存两件事，依赖更清晰。

**退房即清**：一个定时任务（或启动时 + 每小时）把 `check_out + grace < now()` 的
`stay_context` 置为 `EXPIRED` 并清空个人字段。这是 [03 §3.2 的隐私红线](03-技术选型与关键决定的后果.md)。

### 5.8 降级与可观测（对应 P7）

| 依赖 | 角色 | 不可用时 |
|---|---|---|
| PostgreSQL | 主链路 | **硬失败**（不做降级，直接报错，因为业务数据在这里） |
| Zilliz（知识 + 缓存） | 主链路 | **显式报警**；知识检索降级为本地文本快照的关键词检索；缓存整体跳过（不猜） |
| DashScope 生成 | 主链路 | 显式报错，不给伪回答 |
| DashScope embedding | 主链路 | 降级为关键词检索（同 Zilliz 不可用时的路径） |
| Redis | 可选加速 | 无感（本就走 PG） |
| Neo4j | 关闭 | 不适用 |

**每次回答必须落一行"用了哪些降级路径"**（`answer_log.degraded_paths`），
并在 SSE 的 `meta` 事件里返回给前端——**降级必须让用户与评测者都看得见**。

---

## 6. 数据模型

### 6.1 复用（参考项目已有）

`users` · `hotels`（**即门店主表，`hotel_id` 就是 `store_id`，不新建 stores 表，避免两个近义概念**）·
`room_types` · `orders` · `coupons` · `user_coupons` · `promotion_activities`

### 6.2 新增

```sql
-- 门店事实（结构化 KV，门店可自维护的落点）
CREATE TABLE store_facts (
  store_id     VARCHAR(32) NOT NULL,
  fact_key     VARCHAR(64) NOT NULL,   -- breakfast_time / wifi_ssid / wifi_password / pool_hours ...
  fact_value   TEXT        NOT NULL,
  updated_by   VARCHAR(64),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  active       BOOLEAN     NOT NULL DEFAULT TRUE,
  PRIMARY KEY (store_id, fact_key)
);

-- 会话上下文（阶段的载体）
CREATE TABLE guest_sessions (
  session_id       VARCHAR(128) PRIMARY KEY,
  channel          VARCHAR(32) NOT NULL,     -- qr_web / web / agent_desk
  store_id         VARCHAR(32) NOT NULL,
  room_no          VARCHAR(16),
  order_id         VARCHAR(64),
  stay_id          VARCHAR(64),
  stage            VARCHAR(8)  NOT NULL DEFAULT 'UNKNOWN',  -- PRE / IN / POST / UNKNOWN
  auth_level       SMALLINT    NOT NULL DEFAULT 0,          -- 0 / 1 / 2
  auth_verified_at TIMESTAMPTZ,
  needs_human      BOOLEAN     NOT NULL DEFAULT FALSE,      -- 投诉双通道的标记
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_active_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 入住上下文（退房即失效）
CREATE TABLE stay_context (
  stay_id          VARCHAR(64) PRIMARY KEY,
  store_id         VARCHAR(32) NOT NULL,
  room_no          VARCHAR(16) NOT NULL,
  guest_last_name  VARCHAR(32) NOT NULL,     -- 仅用于比对；禁止进日志
  order_id         VARCHAR(64),
  check_in_date    DATE        NOT NULL,
  check_out_date   DATE        NOT NULL,
  status           VARCHAR(16) NOT NULL DEFAULT 'IN_HOUSE', -- IN_HOUSE / CHECKED_OUT / EXPIRED
  expires_at       TIMESTAMPTZ NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 工单（吸收原 support_tickets）
CREATE TABLE work_orders (
  work_order_id  VARCHAR(32) PRIMARY KEY,
  store_id       VARCHAR(32) NOT NULL,
  room_no        VARCHAR(16),
  stay_id        VARCHAR(64),
  order_id       VARCHAR(64),
  session_id     VARCHAR(128) NOT NULL,
  category       VARCHAR(32) NOT NULL,   -- SERVICE/MAINTENANCE/MAINTENANCE_SAFETY/COMPLAINT/CHECKOUT_REQUEST/INVOICE/LOST_FOUND
  intent         VARCHAR(64),
  payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- 物品、数量、时间、故障描述
  priority       VARCHAR(4)  NOT NULL DEFAULT 'P2',          -- P0/P1/P2
  status         VARCHAR(16) NOT NULL DEFAULT 'CREATED',
  assignee_dept  VARCHAR(24),            -- FRONT_DESK/HOUSEKEEPING/ENGINEERING/DUTY_MANAGER
  assignee_type  VARCHAR(8)  NOT NULL DEFAULT 'HUMAN',       -- HUMAN/DEVICE（为送物机器人预留）
  assignee_name  VARCHAR(64),
  sla_minutes    INTEGER     NOT NULL,
  due_at         TIMESTAMPTZ NOT NULL,
  accepted_at    TIMESTAMPTZ,
  done_at        TIMESTAMPTZ,
  failed_reason  TEXT,
  on_time        BOOLEAN,
  guest_notified BOOLEAN     NOT NULL DEFAULT FALSE,
  review_status  VARCHAR(16) NOT NULL DEFAULT 'NONE',  -- 沿用误转复核：NONE/PENDING/OVERTURNED
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 工单事件（失败归因的证据链）
CREATE TABLE work_order_events (
  event_id      BIGSERIAL PRIMARY KEY,
  work_order_id VARCHAR(32) NOT NULL,
  event_type    VARCHAR(24) NOT NULL,   -- CREATED/ASSIGNED/ACCEPTED/DONE/FAILED/ESCALATED/NOTIFIED/CANCELLED
  from_status   VARCHAR(16),
  to_status     VARCHAR(16),
  actor         VARCHAR(64),            -- system / agent / staff account / guest
  note          TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 鉴权尝试（度量误挡率与枚举攻击）
CREATE TABLE auth_attempts (
  attempt_id     BIGSERIAL PRIMARY KEY,
  session_id     VARCHAR(128) NOT NULL,
  credential_type VARCHAR(16) NOT NULL,   -- ROOM / ORDER
  submitted_hash  VARCHAR(64) NOT NULL,   -- 只存哈希
  result         VARCHAR(8)  NOT NULL,    -- PASS / FAIL
  fail_reason    VARCHAR(64),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 会员级偏好（不再走向量）
CREATE TABLE member_preferences (
  user_id     VARCHAR(64) NOT NULL,
  pref_key    VARCHAR(48) NOT NULL,
  pref_value  TEXT        NOT NULL,
  source_session VARCHAR(128),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, pref_key)
);

-- 知识文本快照（断网降级用）
CREATE TABLE knowledge_chunks (
  chunk_id    VARCHAR(64) PRIMARY KEY,
  store_id    VARCHAR(32),               -- NULL = 集团级
  doc_id      VARCHAR(128) NOT NULL,
  source_file TEXT NOT NULL,
  section     TEXT,
  content     TEXT NOT NULL,
  policy_version VARCHAR(64) NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 评测落库（第 11 轮）
CREATE TABLE eval_runs (
  run_id     VARCHAR(32) PRIMARY KEY,
  suite      VARCHAR(16) NOT NULL,   -- A/B/C/D/E
  git_rev    VARCHAR(64),
  env_snapshot JSONB NOT NULL,       -- 依赖状态、降级开关、阈值快照
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);
CREATE TABLE eval_results (
  run_id      VARCHAR(32) NOT NULL,
  case_id     VARCHAR(32) NOT NULL,
  expected    TEXT,
  actual      TEXT,
  passed      BOOLEAN,
  latency_ms  INTEGER,
  degraded_paths JSONB,
  PRIMARY KEY (run_id, case_id)
);
```

### 6.3 改造

`answer_log` 增加：`store_id` · `stage` · `required_auth` · `auth_passed` ·
`degraded_paths JSONB` · `work_order_id`（原有 `route/route_source/confidence/tier/cache_hit/latency_ms/ticket_id` 保留）

---

## 7. 接口设计

### 7.1 住客端

| 方法与路径 | 说明 |
|---|---|
| `POST /api/session` | 建立会话上下文：`store_id`（必填）、`room_no` / `order_id`（可选）→ 返回 `session_id` + 判定出的 `stage` |
| `POST /api/chat` | SSE 流式问答；入参 `session_id + query`（**身份一律从会话取，不接受前端传 user_id**） |
| `POST /api/auth/verify` | 提交凭据（房间级 / 订单级）；失败返回统一话术 |
| `GET /api/work-orders/{id}?session_id=` | 客人查询自己工单的进度（**只返回本会话可见字段**） |

### 7.2 坐席台

| 方法与路径 | 说明 |
|---|---|
| `GET /api/staff/work-orders?store_id=&status=&dept=` | 工单池 |
| `POST /api/staff/work-orders/{id}/accept` | 接单 |
| `POST /api/staff/work-orders/{id}/done` | 完成（回填 `note`、触发 `on_time` 计算与客人回执） |
| `POST /api/staff/work-orders/{id}/fail` | 失败（必填 `failed_reason`） |
| `POST /api/staff/work-orders/{id}/escalate` | 升级 |
| `GET /api/staff/sessions/{id}/timeline` | 会话时间线（含跨阶段历史，用于人工接管） |
| `POST /api/staff/sessions/{id}/takeover` | 人工接管（AI 停止自动回复） |

### 7.3 运营与运维

| 方法与路径 | 说明 |
|---|---|
| `GET /api/metrics?hours=` | 看板：闭环率、按时率、失败归因分布、鉴权拒绝率/误挡率、缓存串味率、降级占比、时延 p50/p90 |
| `GET /api/health` | 依赖自检：PostgreSQL / Zilliz / DashScope / Redis / Neo4j；**主链路依赖不通即红色报警** |
| `GET /api/stores/{store_id}/facts` | 门店事实只读（v1 用脚本导入，不做管理界面） |

### 7.4 SSE 事件协议（前端按 `type` 分支）

```
{"meta": {"intent":"room_service_agent","stage":"IN","required_auth":1,
          "degraded":[], "cached": false}}
{"auth_required": {"level":1, "hint":"请告诉我房间号和入住人姓氏"}}
{"content": "..."}                                  # 增量文本
{"work_order": {"id":"WO260916A1B2","category":"SERVICE",
                "dept":"HOUSEKEEPING","sla_minutes":20,
                "due_at":"2026-09-16T10:35:00+08:00"}}
{"done": {"elapsed_ms": 1840}}
{"error": {"code":"DEPENDENCY_DOWN","detail":"Zilliz 不可用，已降级为关键词检索"}}
```

### 7.5 MCP 工具清单（`[注入]` 的参数模型无法伪造）

| 工具 | 参数 | 备注 |
|---|---|---|
| `query_user_orders` | `[注入]user_id`, `limit` | 沿用 |
| `query_order_detail` | `order_id`, `[注入]user_id` | 沿用 |
| `search_hotel_catalog` | `keyword`, `city`, `max_price` | 沿用（跨门店目录） |
| `verify_stay` | `[注入]store_id`, `room_no`, `last_name`, `check_out_date` | **新增**；失败统一话术 |
| `query_store_facts` | `[注入]store_id`, `fact_keys` | **新增** |
| `query_policy_docs` | `query`, `[注入]store_id` | **改造**：按集团 + 门店过滤 |
| `create_work_order` | `[注入]store_id, room_no, stay_id, session_id`, `category`, `intent`, `payload`, `priority` | **新增**；SLA 与部门由服务端按 category 决定，**模型不能自己定 SLA** |
| `query_work_order` | `work_order_id`, `[注入]store_id` | **新增** |
| `query_room_state` | `[注入]store_id`, `room_type_id`, `dates` | **新增**（多跳链路用） |
| `create_invoice_request` | `[注入]user_id`, `order_id`, `invoice_info` | **新增** |
| `query_lost_found` | `[注入]store_id`, `room_no`, `item_desc`, `date_range` | **新增** |
| ~~`analyze_order_cost` `get_promotable_products` `get_promotion_materials` `generate_ai_poster`~~ | — | **摘除**（代码保留） |

> **一条安全边界**：`create_work_order` 的 `sla_minutes` 与 `assignee_dept`
> **由服务端按 category 映射决定，不接受模型传参**——否则模型可能给自己发一个 24 小时的 SLA。

---

## 8. 环境与配置

| 变量 | 说明 | 默认 |
|---|---|---|
| `POSTGRES_DSN` | 业务库 | `postgresql://postgres:postgres@127.0.0.1:5432/hotel_service` |
| `DASHSCOPE_API_KEY` / `MODEL` / `BASE_URL` | 生成与 embedding | `qwen-plus` |
| `MILVUS_URI` / `MILVUS_API_KEY` | Zilliz 托管 | 沿用现有 |
| `MILVUS_POLICY_COLLECTION` | 知识集合 | `hotel_knowledge_v2`（**需重建**） |
| `QA_CACHE_COLLECTION` | 缓存集合 | `qa_semantic_cache_v2`（**需重建**） |
| `CACHE_SIMILARITY_MODE` / `SEMANTIC_CACHE_THRESHOLD` | 缓存阈值 | `similarity` / `0.85` |
| `POLICY_VERSION` | 知识版本 | `HOTEL-2026.1` |
| `REDIS_URL` | 可选加速 | 空 |
| `ENABLE_NEO4J` | 图谱开关 | `false` |
| `ROUTE_CONFIDENCE_THRESHOLD` | 路由阈值 | `0.85` |
| `HOP_MAX` | 多跳上限 | `2` |
| `SLA_SERVICE_MINUTES` / `SLA_MAINTENANCE_MINUTES` / `SLA_SAFETY_MINUTES` | SLA | `20` / `30` / `10` |
| `STAY_GRACE_HOURS` | 入住级记忆宽限期 | `2` |
| `DEFAULT_STORE_ID` | 未指定门店时的默认 | `HT001` |

**启动顺序（本地）**：① PostgreSQL（已在跑）→ ② `python run.py`（FastAPI :5000）
→ ③ `npm run dev`（前端 :5173）→ ④ `工具/自检.py` 验证依赖与降级状态。
MCP Server 由后端按需以子进程方式拉起，无需单独启动。

---

## 9. 可调旋钮

见 [03 §6](03-技术选型与关键决定的后果.md)，不在此重复。新增项已并入该表（SLA、宽限期、多跳上限、升级链）。

---

## 10. 实施清单

### 阶段一 · 基线（先证明遗产能跑）

| # | 任务 | 产出/验收 |
|---|---|---|
| 1 | 建目录，复制可复用文件，改 `.env` | 服务能起来 |
| 2 | `infra/health.py` + `/api/health` | 五个依赖状态可见，主链路依赖报警 |
| 3 | 集合改名（`_v2`）与 `store_id` 字段落进 schema | 集合重建脚本可重复执行 |
| 4 | `工具/启动本地.ps1` + `工具/自检.py` | 一条命令起服务，一条命令自检 |
| 5 | 跑通一次真实问答（政策 + 订单） | 有截图与耗时 |

### 阶段二 · 住中闭环（主体）

| # | 任务 | 产出/验收 |
|---|---|---|
| 6 | `schema.sql`：新增 9 张表；`answer_log` 加字段 | `psql` 可重建 |
| 7 | `store_facts` + `query_store_facts` + 2 家门店 mock 事实 | 同题两店答案不同 |
| 8 | 阶段识别 + `guest_sessions` | A/B 两家店的 `stage` 判定正确 |
| 9 | `auth_guard` + `verify_stay` + `auth_attempts` | 越权题集全拒；误挡率可算 |
| 10 | 工单域：`work_order_service` + 状态机 + `work_order_events` | 6 条流转路径全部可走通并落事件 |
| 11 | `room_service_agent` + `maintenance_agent` | "送两瓶水"→ 工单 + 20 分钟时限 + 回执 |
| 12 | SLA 超时升级任务 | 超时能自动升级并落 `ESCALATED` 事件 |
| 13 | 缓存加 `store_id` + 门店级失效 | 串味率可测（A 店答案不会命中给 B 店） |
| 14 | 看板 `/api/metrics` 扩展 | 闭环率/按时率/失败归因可见 |

### 阶段三 · 离店与协同

| # | 任务 | 产出/验收 |
|---|---|---|
| 15 | `departure_agent` + 发票 + 失物 | 离店后能开工单并查询进度 |
| 16 | 多跳链路（延退/换房）+ `query_room_state` | 三种结果（可批/需前台/不可批）都可复现 |
| 17 | 投诉双通道 | 既派单又标记 `needs_human` |
| 18 | 三级记忆 + 退房即清任务 | 跨阶段不重复问房号；过期入住上下文被清 |

### 阶段四 · 坐席台与评测

| # | 任务 | 产出/验收 |
|---|---|---|
| 19 | 坐席台（工单池 / 接单 / 完成 / 失败 / 升级 / 接管） | 人机闭环可演示 |
| 20 | 五个评测题集（A~E）+ `运行评测.py` + `eval_runs` 落库 | 五个数字都能算 |
| 21 | `评测报告.md`（含失败归因与诚实边界） | 报告可复现（记录 `env_snapshot`） |
| 22 | `07-本地运行与自检手册.md` | 新人能照着跑起来 |

---

## 11. 风险

PRD 的八条风险在此不重复，只列**技术上必须提前处理的四条**：

1. **Zilliz 集合必须重建**（加 `store_id` 字段），且旧集合保留只读以便对比——
   重建是不可逆动作，脚本要幂等、要打印影响范围。
2. **课程代码改造点集中在 `intent_router` 与 `handoff_agent`**，这两处改动最大，
   改完必须立刻跑 A/C 题集，确认没有把前段能力改坏。
3. **SSE 协议的兼容**：新增 `auth_required` / `work_order` 事件，前端要先做好分支，
   否则旧前端会静默丢事件。
4. **退房清理任务**是隐私红线，必须有独立的可观测（清理了多少条、是否有失败），
   不能是一个"跑没跑谁也不知道"的定时脚本。

---

## 12. 待你确认

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | Milvus 集合重建（`hotel_knowledge_v2` / `qa_semantic_cache_v2`），旧集合保留只读 | 同意则阶段一执行；这是不可逆动作，需你点头 |
| 2 | 工单表吸收 `support_tickets`（旧表保留只读） | 吸收，避免两套 SLA 与两套埋点 |
| 3 | 会员偏好从 Milvus 改为 PG 表 | 改，偏好少而结构化，向量检索收益小 |
| 4 | 坐席台范围：工单池 + 接单/完成/失败/升级 + 接管，不做排班与绩效 | 确认 |
| 5 | mock 门店数量与门店差异设计（建议 2 家：HT001 商务型 / HT002 度假型，且 HT001 刚改过早餐时间） | 确认 |
