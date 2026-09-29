-- ============================================================================
-- 随行管家 · 阶段二新增表（幂等，可重复执行）
--
-- 设计依据：05-TDD §6 数据模型
-- 与参考项目库（hotel_service）的关系：
--   · 复用：users / hotels / room_types / orders / coupons / user_coupons / promotion_activities
--     ——其中 hotels.hotel_id 就是 store_id（不新建 stores 表，避免两个近义概念）
--   · 新增：本文件 10 张表 + answer_log 扩展字段
--   · 保留只读：support_tickets（工单已由 work_orders 接管）
--
-- 执行：python 工具/初始化数据库.py
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. 门店事实（结构化 KV）：解决"同一句'早餐几点'在不同门店答案不同"
--    这是 P2（门店差异）的第一道防线：门店事实优先于向量检索的政策文档。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS store_facts (
    store_id    VARCHAR(32) NOT NULL,
    fact_key    VARCHAR(64) NOT NULL,
    fact_value  TEXT        NOT NULL,
    updated_by  VARCHAR(64),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    active      BOOLEAN     NOT NULL DEFAULT TRUE,
    PRIMARY KEY (store_id, fact_key)
);

-- ---------------------------------------------------------------------------
-- 2. PMS 房态镜像（mock）
--    真实项目里这一层是 PMS 适配器；mock 阶段它就是一张只读镜像表。
--    用途：① 入住中判定（阶段识别）② 延退/换房能否批准（多跳链路）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pms_room_state (
    store_id        VARCHAR(32) NOT NULL,
    room_no         VARCHAR(16) NOT NULL,
    room_type_id    VARCHAR(32) NOT NULL,
    room_status     VARCHAR(16) NOT NULL DEFAULT 'OCCUPIED',  -- OCCUPIED / VACANT / CLEANING / MAINTENANCE
    guest_last_name VARCHAR(32),                              -- 仅用于比对；禁止进日志
    check_in_date   DATE,
    check_out_date  DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, room_no)
);

-- ---------------------------------------------------------------------------
-- 3. 会话上下文：**阶段是会话级的**（00 §4.5）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guest_sessions (
    session_id       VARCHAR(128) PRIMARY KEY,
    channel          VARCHAR(32)  NOT NULL DEFAULT 'qr_web',   -- qr_web / web / agent_desk
    store_id         VARCHAR(32)  NOT NULL,
    room_no          VARCHAR(16),                              -- 扫码带入，弱凭据
    order_id         VARCHAR(64),
    stay_id          VARCHAR(64),
    stage            VARCHAR(8)   NOT NULL DEFAULT 'UNKNOWN',  -- PRE / IN / POST / UNKNOWN
    auth_level       SMALLINT     NOT NULL DEFAULT 0,          -- 0 / 1 / 2
    auth_verified_at TIMESTAMPTZ,
    needs_human      BOOLEAN      NOT NULL DEFAULT FALSE,      -- 投诉双通道的标记
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_active_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_guest_sessions_store ON guest_sessions (store_id, last_active_at DESC);

-- ---------------------------------------------------------------------------
-- 4. 入住上下文：**退房即失效**（03 §3.2 隐私红线）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stay_context (
    stay_id         VARCHAR(64) PRIMARY KEY,
    store_id        VARCHAR(32) NOT NULL,
    room_no         VARCHAR(16) NOT NULL,
    guest_last_name VARCHAR(32) NOT NULL,        -- 仅用于比对；禁止进日志
    order_id        VARCHAR(64),
    check_in_date   DATE        NOT NULL,
    check_out_date  DATE        NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'IN_HOUSE',   -- IN_HOUSE / CHECKED_OUT / EXPIRED
    expires_at      TIMESTAMPTZ NOT NULL,                       -- check_out + 宽限期
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stay_context_expire ON stay_context (status, expires_at);

-- ---------------------------------------------------------------------------
-- 5. 工单（本项目的心脏）：吸收原 support_tickets
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id  VARCHAR(32) PRIMARY KEY,
    store_id       VARCHAR(32) NOT NULL,
    room_no        VARCHAR(16),
    stay_id        VARCHAR(64),
    order_id       VARCHAR(64),
    session_id     VARCHAR(128) NOT NULL,
    category       VARCHAR(32) NOT NULL,   -- SERVICE / MAINTENANCE / MAINTENANCE_SAFETY /
                                           -- COMPLAINT / CHECKOUT_REQUEST / INVOICE / LOST_FOUND
    intent         VARCHAR(64),
    payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    priority       VARCHAR(4)  NOT NULL DEFAULT 'P2',
    status         VARCHAR(16) NOT NULL DEFAULT 'CREATED',
    assignee_dept  VARCHAR(24),
    assignee_type  VARCHAR(8)  NOT NULL DEFAULT 'HUMAN',   -- HUMAN / DEVICE（为送物机器人预留）
    assignee_name  VARCHAR(64),
    guest_verified BOOLEAN     NOT NULL DEFAULT FALSE,     -- 安全类允许"先派单后验证"
    sla_minutes    INTEGER     NOT NULL,
    due_at         TIMESTAMPTZ NOT NULL,
    accepted_at    TIMESTAMPTZ,
    done_at        TIMESTAMPTZ,
    failed_reason  TEXT,
    on_time        BOOLEAN,
    guest_notified BOOLEAN     NOT NULL DEFAULT FALSE,
    review_status  VARCHAR(16) NOT NULL DEFAULT 'NONE',    -- NONE / PENDING / OVERTURNED（误转复核）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_work_orders_store_status ON work_orders (store_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_orders_due ON work_orders (status, due_at) WHERE status IN ('CREATED','ASSIGNED','ACCEPTED');

-- ---------------------------------------------------------------------------
-- 6. 工单事件：失败归因的证据链（未建单/未接/未完成/未回执四类从这里算）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_order_events (
    event_id      BIGSERIAL PRIMARY KEY,
    work_order_id VARCHAR(32) NOT NULL,
    event_type    VARCHAR(24) NOT NULL,   -- CREATED/ASSIGNED/ACCEPTED/DONE/FAILED/ESCALATED/NOTIFIED/CANCELLED
    from_status   VARCHAR(16),
    to_status     VARCHAR(16),
    actor         VARCHAR(64),
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wo_events_order ON work_order_events (work_order_id, created_at);

-- ---------------------------------------------------------------------------
-- 7. 鉴权尝试：度量"越权拦截率"与"正当需求误挡率"（两个必须同时报）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_attempts (
    attempt_id      BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(128) NOT NULL,
    credential_type VARCHAR(16) NOT NULL,   -- ROOM / ORDER
    submitted_hash  VARCHAR(64) NOT NULL,   -- 只存哈希，不存明文
    result          VARCHAR(8)  NOT NULL,   -- PASS / FAIL
    fail_reason     VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 8. 会员级偏好：从向量库改回 PG 表（偏好少而结构化，向量检索收益小）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS member_preferences (
    user_id        VARCHAR(64) NOT NULL,
    pref_key       VARCHAR(48) NOT NULL,
    pref_value     TEXT        NOT NULL,
    source_session VARCHAR(128),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, pref_key)
);

-- ---------------------------------------------------------------------------
-- 9. 知识文本快照：断网时降级为关键词检索用（03 变动 4 保险 ②）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id       VARCHAR(64) PRIMARY KEY,
    store_id       VARCHAR(32),        -- NULL = 集团级
    doc_id         VARCHAR(128) NOT NULL,
    source_file    TEXT NOT NULL,
    section        TEXT,
    content        TEXT NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_store ON knowledge_chunks (store_id);

-- ---------------------------------------------------------------------------
-- 10. 评测落库：报告可复现（记录环境快照）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id       VARCHAR(32) PRIMARY KEY,
    suite        VARCHAR(16) NOT NULL,
    git_rev      VARCHAR(64),
    env_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS eval_results (
    run_id         VARCHAR(32) NOT NULL,
    case_id        VARCHAR(32) NOT NULL,
    expected       TEXT,
    actual         TEXT,
    passed         BOOLEAN,
    latency_ms     INTEGER,
    degraded_paths JSONB,
    PRIMARY KEY (run_id, case_id)
);

-- ---------------------------------------------------------------------------
-- 11. 埋点扩展：回答级字段（P7 要求"降级必须可见"）
-- ---------------------------------------------------------------------------
ALTER TABLE answer_log ADD COLUMN IF NOT EXISTS store_id       VARCHAR(32);
ALTER TABLE answer_log ADD COLUMN IF NOT EXISTS stage          VARCHAR(8);
ALTER TABLE answer_log ADD COLUMN IF NOT EXISTS required_auth  SMALLINT;
ALTER TABLE answer_log ADD COLUMN IF NOT EXISTS auth_passed    BOOLEAN;
ALTER TABLE answer_log ADD COLUMN IF NOT EXISTS degraded_paths JSONB;
ALTER TABLE answer_log ADD COLUMN IF NOT EXISTS work_order_id  VARCHAR(32);

-- 鉴权收集状态（引导客人补全凭据的过程要跨多轮，必须落库）
ALTER TABLE guest_sessions ADD COLUMN IF NOT EXISTS pending_auth JSONB;

-- 订单级凭据需要手机号后四位才能校验（参考项目没有这个字段）
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(20);

-- ---------------------------------------------------------------------------
-- 失物招领（离店后事务；mock 阶段是酒店保管柜的台账）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lost_found_items (
    item_id          VARCHAR(32) PRIMARY KEY,
    store_id         VARCHAR(32) NOT NULL,
    room_no          VARCHAR(16),
    found_date       DATE        NOT NULL,
    item_desc        TEXT        NOT NULL,
    status           VARCHAR(16) NOT NULL DEFAULT 'STORED',  -- STORED / RETURNED / DISCARDED
    storage_location TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lost_found_store ON lost_found_items (store_id, found_date DESC);
