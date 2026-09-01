"""
DuckDB 数据仓库 Schema 定义。

严格遵循设计文档的三层模型：
- warehouse 层表：以原始格式存储结构化数据
- analytics 层视图：以 mv_ 前缀标识的物化/逻辑分析视图

设计文档中的表结构：
    stock_basic, daily_price, minute_price,
    financial_statement, news_event, portfolio
"""

# ═══════════════════════════════════════════════════════════════
# Warehouse Layer — 基础存储表
# ═══════════════════════════════════════════════════════════════

SCHEMA_SQL = """
-- DuckDB 兼容的 schema 定义。
-- DuckDB 不支持复合 PRIMARY KEY 中的 DATE/TIMESTAMP 列，
-- 因此使用 UNIQUE 约束替代复合主键，并显式创建索引。

-- 股票/ETF 基础信息
CREATE TABLE IF NOT EXISTS stock_basic (
    code        VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    market      VARCHAR NOT NULL,       -- 'A', 'HK_CONNECT', 'US', 'ETF_CN', 'ETF_INTL'
    asset_type  VARCHAR NOT NULL DEFAULT 'STOCK',  -- 'STOCK', 'ETF'
    industry    VARCHAR,
    list_date   DATE,
    status      VARCHAR NOT NULL DEFAULT 'active', -- 'active', 'delisted', 'suspended'
    currency    VARCHAR NOT NULL DEFAULT 'CNY',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 日行情数据
CREATE TABLE IF NOT EXISTS daily_price (
    date        DATE NOT NULL,
    symbol      VARCHAR NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE NOT NULL,
    volume      DOUBLE,
    amount      DOUBLE,
    adj_factor  DOUBLE,              -- 复权因子（前复权时 close * adj_factor = 实际价格）
    source      VARCHAR NOT NULL,     -- 'tushare', 'baostock', 'eastmoney', 'yfinance'
    quality_score INTEGER DEFAULT 100, -- 数据质量评分
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_price_date ON daily_price(date);
CREATE INDEX IF NOT EXISTS idx_daily_price_symbol ON daily_price(symbol);

-- 分钟行情数据
CREATE TABLE IF NOT EXISTS minute_price (
    datetime    TIMESTAMP NOT NULL,
    symbol      VARCHAR NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE NOT NULL,
    volume      DOUBLE,
    amount      DOUBLE,
    source      VARCHAR NOT NULL,
    UNIQUE (symbol, datetime)
);
CREATE INDEX IF NOT EXISTS idx_minute_price_date ON minute_price(datetime);
CREATE INDEX IF NOT EXISTS idx_minute_price_symbol ON minute_price(symbol);

-- A 股盘中实时快照（东方财富批量行情，deploy/quote_daemon.py 每分钟写入）。
-- 快照语义非分钟 K 线；未复权原始价，仅供盘中执行闸门与复盘，不进 daily_price。
CREATE TABLE IF NOT EXISTS cn_quote_snapshot (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR NOT NULL,
    last        DOUBLE,
    pct_chg     DOUBLE,      -- 涨跌幅 %
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    prev_close  DOUBLE,
    volume_hand DOUBLE,      -- 成交量（手，东财口径）
    amount      DOUBLE,      -- 成交额（元）
    source      VARCHAR NOT NULL DEFAULT 'eastmoney',
    fetched_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_cn_quote_ts ON cn_quote_snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_cn_quote_symbol ON cn_quote_snapshot(symbol);

-- A 股每日基本面指标（tushare daily_basic，真实估值因子数据源）。
-- 与 daily_price 解耦：该表为未复权口径的估值快照，改写不影响价格主线。
CREATE TABLE IF NOT EXISTS daily_basic (
    date          DATE NOT NULL,
    symbol        VARCHAR NOT NULL,
    turnover_rate DOUBLE,          -- 换手率（%）
    volume_ratio  DOUBLE,          -- 量比
    pe            DOUBLE,          -- 静态市盈率
    pe_ttm        DOUBLE,          -- 滚动市盈率
    pb            DOUBLE,          -- 市净率
    total_mv      DOUBLE,          -- 总市值（万元，tushare 口径）
    circ_mv       DOUBLE,          -- 流通市值（万元）
    source        VARCHAR NOT NULL DEFAULT 'tushare',
    fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_basic_date ON daily_basic(date);
CREATE INDEX IF NOT EXISTS idx_daily_basic_symbol ON daily_basic(symbol);

-- 证券注册表（真 PIT 宇宙重建 Phase 1，deploy/build_stock_registry.py 写入）。
-- 口径：symbol 为 6 位纯数字（与 daily_price 同口径），exchange 单列；
-- baostock query_stock_basic() 全量过滤 type=1（股票）落库，ETF(type=5) 不入表；
-- out_date NULL = 未退市。tushare stock_basic 仅作退市清单交叉校验，不写本表。
CREATE TABLE IF NOT EXISTS stock_registry (
    symbol       VARCHAR NOT NULL,           -- 6 位数字，与 daily_price 同口径
    code_name    VARCHAR,
    exchange     VARCHAR NOT NULL,           -- 'SH'/'SZ'
    ipo_date     DATE,
    out_date     DATE,                       -- NULL = 未退市
    security_type VARCHAR NOT NULL,          -- baostock type: 1股票 5ETF
    list_status  VARCHAR NOT NULL,           -- 'LISTED'/'DELISTED'
    source       VARCHAR NOT NULL DEFAULT 'baostock',
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol)
);
CREATE INDEX IF NOT EXISTS idx_stock_registry_status
    ON stock_registry(list_status, exchange);

-- 日频交易状态表（真 PIT 宇宙重建 Phase 2）。
-- 口径：trade_status 1=正常 0=停牌（baostock tradestatus）；
-- is_st 1=ST 0=否（baostock isST），历史归档无 isST 字段时落 -1（未知）。
-- 现役池由 deploy/mine_baostock_status_archive.py 从 raw 归档回填
-- （source='baostock-archive'），退市股由 deploy/backfill_delisted_daily.py
-- 补采（source='baostock'）；UNIQUE(symbol,date) + INSERT OR REPLACE 幂等。
CREATE TABLE IF NOT EXISTS stock_status_daily (
    date         DATE NOT NULL,
    symbol       VARCHAR NOT NULL,
    trade_status INTEGER NOT NULL,   -- 1 正常 0 停牌（baostock tradestatus）
    is_st        INTEGER NOT NULL,   -- 1 ST 0 否（baostock isST）；-1 未知（归档无此字段）
    source       VARCHAR NOT NULL DEFAULT 'baostock',
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_status_daily_date ON stock_status_daily(date);

-- 财务数据（单表支持所有报表类型）
CREATE TABLE IF NOT EXISTS financial_statement (
    symbol          VARCHAR NOT NULL,
    report_date     DATE NOT NULL,
    report_type     VARCHAR NOT NULL DEFAULT 'annual',
    filed_at        DATE,
    revenue         DOUBLE,
    net_profit      DOUBLE,
    operating_cf    DOUBLE,
    free_cash_flow  DOUBLE,
    total_assets    DOUBLE,
    total_liabilities DOUBLE,
    equity          DOUBLE,
    roe             DOUBLE,
    roa             DOUBLE,
    debt_ratio      DOUBLE,
    gross_margin    DOUBLE,
    net_margin      DOUBLE,
    eps             DOUBLE,
    bvps            DOUBLE,
    revenue_growth  DOUBLE,
    profit_growth   DOUBLE,
    r_and_d_expense DOUBLE,
    source          VARCHAR NOT NULL,
    pe              DOUBLE,          -- 市盈率（populate_financial_statement 快照口径）
    pb              DOUBLE,          -- 市净率（同上）
    cfo_to_np       DOUBLE,          -- 经营现金流/净利润（baostock CFOToNP，应计质量）
    UNIQUE (symbol, report_date, report_type)
);
CREATE INDEX IF NOT EXISTS idx_financial_symbol ON financial_statement(symbol);

-- 新闻/事件库
CREATE TABLE IF NOT EXISTS news_event (
    content_hash    VARCHAR PRIMARY KEY,
    published_at    TIMESTAMP,
    symbol          VARCHAR,
    title           VARCHAR NOT NULL,
    content         TEXT,
    source          VARCHAR NOT NULL,
    source_url      VARCHAR,
    category        VARCHAR DEFAULT 'general',
    sentiment       DOUBLE,
    impact_score    INTEGER,
    confidence      INTEGER DEFAULT 0,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news_event(symbol);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_event(published_at);

-- 长期重大事件主表：只存结构化索引，详细原文位于 NAS event_archive。
CREATE TABLE IF NOT EXISTS investment_event (
    event_id            VARCHAR PRIMARY KEY,
    occurred_at         TIMESTAMP NOT NULL,
    discovered_at       TIMESTAMP NOT NULL,
    event_type          VARCHAR NOT NULL,
    scope               VARCHAR NOT NULL DEFAULT 'MARKET',
    title               VARCHAR NOT NULL,
    summary             TEXT NOT NULL,
    materiality_score   INTEGER NOT NULL,
    risk_score          INTEGER NOT NULL,
    confidence_score    INTEGER NOT NULL,
    verification_status VARCHAR NOT NULL,
    decision_eligible   BOOLEAN NOT NULL DEFAULT FALSE,
    market_regime       VARCHAR,
    expected_horizon    VARCHAR,
    source_count        INTEGER NOT NULL DEFAULT 0,
    independent_domains INTEGER NOT NULL DEFAULT 0,
    archive_relpath     VARCHAR NOT NULL,
    content_hash        VARCHAR NOT NULL UNIQUE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_investment_event_time
    ON investment_event(occurred_at);
CREATE INDEX IF NOT EXISTS idx_investment_event_type
    ON investment_event(event_type);
CREATE INDEX IF NOT EXISTS idx_investment_event_materiality
    ON investment_event(materiality_score);

-- 自动采集只进入候选队列；未确认前不得升级为 investment_event。
CREATE TABLE IF NOT EXISTS event_candidate (
    candidate_id         VARCHAR PRIMARY KEY,
    detected_at          TIMESTAMP NOT NULL,
    query_hash           VARCHAR NOT NULL,
    assessment_status    VARCHAR NOT NULL,
    decision_eligible    BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_count       INTEGER NOT NULL,
    stored_evidence_count INTEGER NOT NULL,
    provider_status_json TEXT NOT NULL,
    audit_relpath        VARCHAR,
    review_status        VARCHAR NOT NULL DEFAULT 'PENDING',
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (query_hash, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_event_candidate_review
    ON event_candidate(review_status, detected_at);

-- 证据索引。没有存储许可时，title/url/content 均不入库，只保留哈希和域名。
CREATE TABLE IF NOT EXISTS event_evidence (
    evidence_id         VARCHAR PRIMARY KEY,
    event_id            VARCHAR NOT NULL,
    provider            VARCHAR NOT NULL,
    source_domain       VARCHAR,
    source_title        VARCHAR,
    source_url          VARCHAR,
    published_at        TIMESTAMP,
    retrieved_at        TIMESTAMP NOT NULL,
    authority_level     VARCHAR NOT NULL,
    license_mode        VARCHAR NOT NULL,
    storage_allowed     BOOLEAN NOT NULL DEFAULT FALSE,
    content_hash        VARCHAR NOT NULL,
    archive_relpath     VARCHAR,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_event_evidence_event
    ON event_evidence(event_id);

-- 事件对持仓、行业、指数或宏观序列的影响假设。
CREATE TABLE IF NOT EXISTS event_asset_link (
    link_id             VARCHAR PRIMARY KEY,
    event_id            VARCHAR NOT NULL,
    symbol              VARCHAR NOT NULL,
    relation_type       VARCHAR NOT NULL,
    impact_direction    VARCHAR NOT NULL DEFAULT 'UNCERTAIN',
    impact_channels_json TEXT NOT NULL DEFAULT '[]',
    expected_effect     TEXT,
    confidence_score    INTEGER NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, symbol, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_event_asset_symbol
    ON event_asset_link(symbol);

-- 在未来观察窗口结束后填写，防止用事后信息改写当时判断。
CREATE TABLE IF NOT EXISTS event_outcome_review (
    review_id           VARCHAR PRIMARY KEY,
    event_id            VARCHAR NOT NULL,
    review_date         DATE NOT NULL,
    horizon_days        INTEGER NOT NULL,
    observed_outcome    TEXT NOT NULL,
    asset_return        DOUBLE,
    benchmark_return    DOUBLE,
    thesis_result       VARCHAR NOT NULL,
    causal_confidence   INTEGER NOT NULL,
    lesson              TEXT NOT NULL,
    data_as_of          DATE NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, review_date, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_event_review_event
    ON event_outcome_review(event_id);

-- 长期经验必须先是候选，不能自动成为生产策略。
CREATE TABLE IF NOT EXISTS investment_lesson (
    lesson_id             VARCHAR PRIMARY KEY,
    pattern_name          VARCHAR NOT NULL,
    event_type            VARCHAR NOT NULL,
    market_regime         VARCHAR,
    applicable_conditions TEXT NOT NULL,
    invalidation_conditions TEXT NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    sample_size           INTEGER NOT NULL,
    status                VARCHAR NOT NULL DEFAULT 'CANDIDATE',
    human_confirmed       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at           TIMESTAMP
);

-- 研报机构身份与人工治理状态；BLOCKED 必须人工确认。
CREATE TABLE IF NOT EXISTS research_institution (
    institution_id       VARCHAR PRIMARY KEY,
    canonical_name       VARCHAR NOT NULL UNIQUE,
    aliases_json         TEXT NOT NULL DEFAULT '[]',
    jurisdiction         VARCHAR,
    status               VARCHAR NOT NULL DEFAULT 'ACTIVE',
    status_reason        TEXT,
    human_confirmed      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 研报元数据。原文仅在合同允许时进入 NAS research_archive。
CREATE TABLE IF NOT EXISTS research_report (
    report_id            VARCHAR PRIMARY KEY,
    institution_id       VARCHAR NOT NULL,
    title                VARCHAR NOT NULL,
    published_at         TIMESTAMP NOT NULL,
    source_domain        VARCHAR,
    source_url           VARCHAR,
    license_mode         VARCHAR NOT NULL,
    storage_allowed      BOOLEAN NOT NULL DEFAULT FALSE,
    archive_relpath      VARCHAR NOT NULL,
    content_hash         VARCHAR NOT NULL UNIQUE,
    verification_status VARCHAR NOT NULL DEFAULT 'REVIEW_REQUIRED',
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_research_report_institution
    ON research_report(institution_id);
CREATE INDEX IF NOT EXISTS idx_research_report_published
    ON research_report(published_at);

-- 将研报拆成事前可检验主张，避免事后重新解释。
CREATE TABLE IF NOT EXISTS research_claim (
    claim_id             VARCHAR PRIMARY KEY,
    report_id            VARCHAR NOT NULL,
    symbol               VARCHAR NOT NULL,
    claim_type           VARCHAR NOT NULL,
    claim_summary        TEXT NOT NULL,
    direction            VARCHAR NOT NULL DEFAULT 'UNCERTAIN',
    target_value         DOUBLE,
    target_unit          VARCHAR,
    base_value           DOUBLE,
    base_value_date      DATE,
    benchmark_symbol     VARCHAR,
    horizon_date         DATE NOT NULL,
    confidence_score     INTEGER NOT NULL,
    evaluable            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (report_id, symbol, claim_type, horizon_date)
);
CREATE INDEX IF NOT EXISTS idx_research_claim_symbol
    ON research_claim(symbol);
CREATE INDEX IF NOT EXISTS idx_research_claim_horizon
    ON research_claim(horizon_date);

-- 到期后以当时可得数据评估，不改写原主张。
CREATE TABLE IF NOT EXISTS research_claim_outcome (
    outcome_id           VARCHAR PRIMARY KEY,
    claim_id             VARCHAR NOT NULL UNIQUE,
    evaluated_at         TIMESTAMP NOT NULL,
    data_as_of           DATE NOT NULL,
    actual_value         DOUBLE,
    benchmark_value      DOUBLE,
    asset_return         DOUBLE,
    benchmark_return     DOUBLE,
    direction_correct    BOOLEAN,
    normalized_error     DOUBLE,
    result               VARCHAR NOT NULL,
    evaluation_source    VARCHAR NOT NULL,
    notes                TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 机构长期得分只调整来源证据权重，不直接成为股票收益因子。
CREATE TABLE IF NOT EXISTS research_institution_score (
    institution_id       VARCHAR PRIMARY KEY,
    resolved_claims      INTEGER NOT NULL,
    correct_claims       DOUBLE NOT NULL,
    directional_accuracy DOUBLE,
    shrunk_accuracy      DOUBLE NOT NULL,
    reliability_score   DOUBLE NOT NULL,
    evidence_weight      DOUBLE NOT NULL,
    recommended_status  VARCHAR NOT NULL,
    methodology_version VARCHAR NOT NULL,
    calculated_at       TIMESTAMP NOT NULL
);

-- 所有长期事件写操作的显式确认与审计。
CREATE TABLE IF NOT EXISTS event_write_audit (
    audit_id              VARCHAR PRIMARY KEY,
    action                VARCHAR NOT NULL,
    object_type           VARCHAR NOT NULL,
    object_id             VARCHAR NOT NULL,
    confirmation_reference VARCHAR NOT NULL,
    payload_hash          VARCHAR NOT NULL,
    written_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 投资组合
CREATE TABLE IF NOT EXISTS portfolio (
    symbol          VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    market          VARCHAR NOT NULL,
    asset_type      VARCHAR NOT NULL,
    cost_price      DOUBLE,
    shares          DOUBLE NOT NULL,
    current_price   DOUBLE,
    profit_loss     DOUBLE,
    weight          DOUBLE,
    currency        VARCHAR NOT NULL DEFAULT 'CNY',
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 宏观指标数据
CREATE TABLE IF NOT EXISTS macro_data (
    series_id       VARCHAR NOT NULL,
    observation_date DATE NOT NULL,
    value           DOUBLE,
    unit            VARCHAR,
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (series_id, observation_date)
);
CREATE INDEX IF NOT EXISTS idx_macro_series ON macro_data(series_id);

-- 自选观察列表。行情观察与正式 portfolio/NAV 完全隔离。
CREATE TABLE IF NOT EXISTS market_watchlist (
    list_id       VARCHAR PRIMARY KEY,
    name          VARCHAR NOT NULL,
    description   TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_watchlist_item (
    list_id          VARCHAR NOT NULL,
    symbol           VARCHAR NOT NULL,
    provider_symbol  VARCHAR NOT NULL,
    name              VARCHAR NOT NULL,
    market            VARCHAR NOT NULL,
    exchange          VARCHAR,
    currency          VARCHAR NOT NULL,
    asset_type        VARCHAR NOT NULL DEFAULT 'STOCK',
    sort_order        INTEGER NOT NULL DEFAULT 0,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (list_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_item_provider
    ON market_watchlist_item(provider_symbol);

-- 行情快照保留原币、来源时间及延迟声明；不得直接作为正式 CNY 组合估值。
CREATE TABLE IF NOT EXISTS market_quote_snapshot (
    provider_symbol  VARCHAR NOT NULL,
    symbol           VARCHAR NOT NULL,
    quote_time       TIMESTAMPTZ NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL,
    price            DOUBLE NOT NULL,
    previous_close   DOUBLE,
    open              DOUBLE,
    high              DOUBLE,
    low               DOUBLE,
    volume            DOUBLE,
    currency          VARCHAR NOT NULL,
    market_state      VARCHAR,
    source            VARCHAR NOT NULL,
    source_tier       VARCHAR NOT NULL,
    delay_minutes     INTEGER,
    freshness         VARCHAR NOT NULL,
    quality_status    VARCHAR NOT NULL,
    error_code        VARCHAR,
    UNIQUE (provider_symbol, source, quote_time)
);
CREATE INDEX IF NOT EXISTS idx_market_quote_symbol_time
    ON market_quote_snapshot(provider_symbol, quote_time);

-- 每次采集都留运行记录，包括空响应、限流或断网，便于长期监控来源稳定性。
CREATE TABLE IF NOT EXISTS market_quote_collection_run (
    run_id          VARCHAR PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ NOT NULL,
    source          VARCHAR NOT NULL,
    requested       INTEGER NOT NULL,
    received        INTEGER NOT NULL,
    stored          INTEGER NOT NULL,
    status          VARCHAR NOT NULL,
    error_code      VARCHAR,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_quote_run_time
    ON market_quote_collection_run(finished_at);

-- 观察列表的人工修改审计；确认编号不可为空。
CREATE TABLE IF NOT EXISTS market_watch_write_audit (
    audit_id              VARCHAR PRIMARY KEY,
    action                VARCHAR NOT NULL,
    list_id               VARCHAR NOT NULL,
    symbol                VARCHAR,
    confirmation_reference VARCHAR NOT NULL,
    payload_hash          VARCHAR NOT NULL,
    written_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 国信模拟盘原始导出索引。只有明确证明为模拟账户的文件才能进入归档；
-- 在取得真实样本并人工固定字段映射前，不生成任何订单/成交/费用规范化记录。
CREATE TABLE IF NOT EXISTS broker_sim_export_archive (
    artifact_id             VARCHAR PRIMARY KEY,
    export_kind             VARCHAR NOT NULL,
    broker                  VARCHAR NOT NULL,
    client_name             VARCHAR NOT NULL,
    exported_at             TIMESTAMPTZ NOT NULL,
    received_at             TIMESTAMPTZ NOT NULL,
    source_filename         VARCHAR NOT NULL,
    source_sha256           VARCHAR NOT NULL UNIQUE,
    source_size_bytes       BIGINT NOT NULL,
    account_mode            VARCHAR NOT NULL,
    account_proof_reference VARCHAR NOT NULL,
    confirmation_reference  VARCHAR NOT NULL,
    archive_relpath         VARCHAR NOT NULL UNIQUE,
    mapping_status          VARCHAR NOT NULL DEFAULT 'PENDING_REAL_SAMPLE',
    normalization_status    VARCHAR NOT NULL DEFAULT 'RAW_ONLY',
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (account_mode = 'SIMULATION'),
    CHECK (mapping_status = 'PENDING_REAL_SAMPLE'),
    CHECK (normalization_status = 'RAW_ONLY')
);
CREATE INDEX IF NOT EXISTS idx_broker_sim_export_time
    ON broker_sim_export_archive(exported_at);

-- 模拟盘成交字段映射的人工验收记录（仿 walk_forward_basis_approval 模式；
-- 归档表本身 CHECK 锁定 PENDING/RAW_ONLY，验收状态只能落在独立表）。
-- 批准只解锁回归研究输入，不授权生产权重变化或任何交易。
CREATE TABLE IF NOT EXISTS broker_sim_mapping_approval (
    approval_id            VARCHAR PRIMARY KEY,
    mapping_version        VARCHAR NOT NULL UNIQUE,
    reviewer               VARCHAR NOT NULL,
    confirmation_reference VARCHAR NOT NULL,
    approved_at            TIMESTAMPTZ NOT NULL,
    decision               VARCHAR NOT NULL,
    evidence_sha256        VARCHAR NOT NULL,
    notes                  VARCHAR,
    CHECK (decision IN ('APPROVED', 'REJECTED'))
);

-- Walk-Forward 的输入资产索引。原始 manifest 与证据文件保存在
-- data/raw/walk_forward_inputs/ 下；这里仅保存不可变哈希和可查询的规范化索引。
-- “已归档”不等于“可回测”，价格基准还必须经过独立人工批准并通过仓库覆盖校验。
CREATE TABLE IF NOT EXISTS walk_forward_input_bundle (
    bundle_id              VARCHAR PRIMARY KEY,
    dataset_id             VARCHAR NOT NULL,
    universe_id            VARCHAR NOT NULL,
    generated_at           TIMESTAMPTZ NOT NULL,
    as_of                  DATE NOT NULL,
    research_start         DATE NOT NULL,
    research_end           DATE NOT NULL,
    received_at            TIMESTAMPTZ NOT NULL,
    manifest_sha256        VARCHAR NOT NULL UNIQUE,
    archive_relpath        VARCHAR NOT NULL UNIQUE,
    membership_count       INTEGER NOT NULL,
    distinct_symbol_count  INTEGER NOT NULL,
    price_series_count     INTEGER NOT NULL,
    benchmark_symbol       VARCHAR NOT NULL,
    archive_status         VARCHAR NOT NULL DEFAULT 'HASH_VERIFIED_RAW_ONLY',
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (research_start <= research_end),
    CHECK (membership_count > 0),
    CHECK (distinct_symbol_count > 0),
    CHECK (price_series_count > 0),
    CHECK (archive_status = 'HASH_VERIFIED_RAW_ONLY')
);
CREATE INDEX IF NOT EXISTS idx_walk_forward_bundle_received
    ON walk_forward_input_bundle(received_at);

CREATE TABLE IF NOT EXISTS walk_forward_universe_membership (
    bundle_id       VARCHAR NOT NULL,
    symbol          VARCHAR NOT NULL,
    effective_from  DATE NOT NULL,
    effective_to    DATE,
    source_id       VARCHAR NOT NULL,
    CHECK (effective_to IS NULL OR effective_from <= effective_to),
    UNIQUE (bundle_id, symbol, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_walk_forward_membership_bundle
    ON walk_forward_universe_membership(bundle_id);

CREATE TABLE IF NOT EXISTS walk_forward_price_evidence (
    bundle_id          VARCHAR NOT NULL,
    symbol             VARCHAR NOT NULL,
    is_benchmark       BOOLEAN NOT NULL,
    benchmark_id       VARCHAR,
    basis              VARCHAR NOT NULL,
    price_field        VARCHAR NOT NULL,
    adjustment_method  VARCHAR NOT NULL,
    source_id          VARCHAR NOT NULL,
    warehouse_source   VARCHAR NOT NULL,
    data_start         DATE NOT NULL,
    data_end           DATE NOT NULL,
    first_session      DATE NOT NULL,
    last_session       DATE NOT NULL,
    expected_row_count INTEGER NOT NULL,
    warehouse_sha256   VARCHAR NOT NULL,
    artifact_relpath   VARCHAR NOT NULL,
    artifact_sha256    VARCHAR NOT NULL,
    artifact_size_bytes BIGINT NOT NULL,
    CHECK (data_start <= data_end),
    CHECK (data_start <= first_session),
    CHECK (first_session <= last_session),
    CHECK (last_session <= data_end),
    CHECK (expected_row_count > 0),
    CHECK (artifact_size_bytes > 0),
    UNIQUE (bundle_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_walk_forward_price_bundle
    ON walk_forward_price_evidence(bundle_id);

-- 策略候选信号：只记录研究信号与证据哈希，不含任何交易金额、数量或下单指令。
-- 候选生成时点 generated_at 是防未来数据穿越的锚点，成交必须晚于它。
CREATE TABLE IF NOT EXISTS strategy_candidate (
    candidate_id      VARCHAR PRIMARY KEY,
    generated_at      TIMESTAMPTZ NOT NULL,
    data_as_of        DATE NOT NULL,
    strategy_version  VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    side              VARCHAR NOT NULL,
    conviction        DOUBLE NOT NULL,
    evidence_hash     VARCHAR NOT NULL,
    status            VARCHAR NOT NULL DEFAULT 'CANDIDATE',
    source            VARCHAR NOT NULL,
    content_sha256    VARCHAR NOT NULL UNIQUE,
    decision_price    DOUBLE,  -- 信号决策价（=sim_targets.prices），回归偏差口径基准
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (side IN ('BUY', 'SELL', 'HOLD_REDUCE')),
    CHECK (conviction >= 0 AND conviction <= 1),
    CHECK (status IN ('CANDIDATE', 'USER_SUBMITTED', 'EXPIRED', 'REJECTED')),
    CHECK (source IN ('USER', 'AI_AGENT', 'FACTOR_MODEL', 'COMBINED'))
);
CREATE INDEX IF NOT EXISTS idx_strategy_candidate_symbol
    ON strategy_candidate(symbol, generated_at);
CREATE INDEX IF NOT EXISTS idx_strategy_candidate_status
    ON strategy_candidate(status, generated_at);

-- 候选状态流转审计；只允许单向前进，原始信号内容不可改写。
CREATE TABLE IF NOT EXISTS strategy_candidate_status_audit (
    audit_id              VARCHAR PRIMARY KEY,
    candidate_id          VARCHAR NOT NULL,
    previous_status       VARCHAR NOT NULL,
    new_status            VARCHAR NOT NULL,
    confirmation_reference VARCHAR NOT NULL,
    written_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (candidate_id, new_status)
);

-- 策略预注册冻结（pre-registration freeze）：策略版本/参数/宇宙/评价指标的
-- 冻结快照 + 哈希。freeze_date 是样本外观察起点，只允许用此日之后新发生的数据。
-- 冻结后规则不可变（store 层不提供任何参数更新接口），只能退役或被新冻结取代；
-- 失败版本保留（RETIRED_FAILED 不删除），防选择性报告。
CREATE TABLE IF NOT EXISTS strategy_freeze (
    freeze_id        VARCHAR PRIMARY KEY,  -- sf_ + 内容哈希前 20 位（幂等）
    strategy_id      VARCHAR NOT NULL,
    strategy_version VARCHAR NOT NULL,
    params_json      VARCHAR NOT NULL,     -- canonical JSON（sort_keys）
    params_hash      VARCHAR NOT NULL,     -- sha256 前 16 位
    universe_spec_json VARCHAR,
    universe_hash    VARCHAR,
    metrics_spec_json  VARCHAR,
    freeze_date      DATE NOT NULL,        -- 样本外观察起点
    code_ref         VARCHAR,              -- freeze 时 git rev-parse --short HEAD，取不到为 NULL
    status           VARCHAR NOT NULL DEFAULT 'FROZEN',
    reviewer         VARCHAR NOT NULL,     -- 人工审批：创建即冻结必须带
    confirmation_reference VARCHAR NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('FROZEN', 'SUPERSEDED', 'RETIRED_FAILED', 'RETIRED_SUCCESS'))
);
-- 每个 strategy_id 最多一条 FROZEN（store 层在事务内保证）。
CREATE INDEX IF NOT EXISTS idx_strategy_freeze_active
    ON strategy_freeze(strategy_id, status);

-- 冻结状态流转审计；只允许单向前进，冻结内容本身不可改写。
CREATE TABLE IF NOT EXISTS strategy_freeze_audit (
    audit_id    VARCHAR PRIMARY KEY,
    freeze_id   VARCHAR NOT NULL,
    from_status VARCHAR,                   -- 创建审计为 NULL -> FROZEN
    to_status   VARCHAR NOT NULL,
    actor       VARCHAR NOT NULL,
    reason      VARCHAR,
    "at"        TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- at 是 DuckDB 保留字，须加引号
);
CREATE INDEX IF NOT EXISTS idx_strategy_freeze_audit
    ON strategy_freeze_audit(freeze_id);

-- 手机同花顺模拟盘规范化成交（deploy/phone/normalize_ths_fills.py 写入）。
-- 费用在模拟盘不可观测，fee_amount 恒为 NULL → 回归层标 MISSING_FEE 不补零。
CREATE TABLE IF NOT EXISTS broker_sim_normalized_fill (
    fill_id          VARCHAR PRIMARY KEY,  -- sha(symbol,side,fill_time,price,qty) 语义去重
    symbol           VARCHAR NOT NULL,
    side             VARCHAR NOT NULL,
    fill_time        TIMESTAMPTZ NOT NULL,
    fill_price       DOUBLE NOT NULL,
    quantity         DOUBLE NOT NULL,
    fee_amount       DOUBLE,
    candidate_id     VARCHAR,              -- 无法归属候选时为 NULL（孤儿成交，回归层拒收计数）
    account_mode     VARCHAR NOT NULL DEFAULT 'SIMULATION',
    source           VARCHAR NOT NULL DEFAULT 'ths_phone_sim',
    mapping_version  VARCHAR NOT NULL,
    source_record    VARCHAR NOT NULL,     -- 原始解析 cells JSON（留痕）
    imported_at      TIMESTAMPTZ NOT NULL,
    CHECK (side IN ('BUY', 'SELL'))
);
CREATE INDEX IF NOT EXISTS idx_bsnf_symbol_time ON broker_sim_normalized_fill(symbol, fill_time);
CREATE INDEX IF NOT EXISTS idx_bsnf_candidate ON broker_sim_normalized_fill(candidate_id);

-- 模拟盘每日账户快照（deploy/phone/daily_sim_review.py 写入），
-- TWR/回撤等长期绩效口径的原始序列；仅模拟观察期数据。
CREATE TABLE IF NOT EXISTS sim_nav_daily (
    date          DATE NOT NULL UNIQUE,
    total_assets  DOUBLE,
    cash          DOUBLE,
    market_value  DOUBLE,
    holdings_json VARCHAR,
    source        VARCHAR NOT NULL DEFAULT 'ths_phone_sim',
    captured_at   TIMESTAMPTZ NOT NULL
);

-- 价格是否确为“含分红总回报口径”无法靠文件名或 manifest 自证，必须由独立人工
-- 核验记录批准。批准只解锁研究输入，不授权生产权重变化或任何交易。
CREATE TABLE IF NOT EXISTS walk_forward_basis_approval (
    bundle_id              VARCHAR PRIMARY KEY,
    manifest_sha256        VARCHAR NOT NULL,
    reviewer               VARCHAR NOT NULL,
    confirmation_reference VARCHAR NOT NULL,
    approved_at            TIMESTAMPTZ NOT NULL,
    decision               VARCHAR NOT NULL,
    CHECK (decision = 'APPROVED')
);

""".strip()

# ═══════════════════════════════════════════════════════════════
# Analytics Layer — 分析视图/物化查询
# ═══════════════════════════════════════════════════════════════

ANALYTIC_VIEWS_SQL = """

-- 组合实时盈亏视图
CREATE OR REPLACE VIEW mv_portfolio_pnl AS
SELECT
    p.symbol,
    p.name,
    p.market,
    p.shares,
    p.cost_price,
    p.current_price,
    p.shares * (COALESCE(p.current_price, 0) - COALESCE(p.cost_price, 0)) AS pnl,
    CASE WHEN p.cost_price IS NOT NULL AND p.cost_price > 0
        THEN (COALESCE(p.current_price, 0) - p.cost_price) / p.cost_price
        ELSE NULL
    END AS pnl_pct,
    p.weight,
    p.updated_at
FROM portfolio p;

-- 日行情最新记录视图（含 20日/60日均线）
CREATE OR REPLACE VIEW mv_daily_price_latest AS
WITH ranked AS (
    SELECT *,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
    FROM daily_price
),
ma AS (
    SELECT
        symbol,
        date,
        close,
        AVG(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
        AVG(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60
    FROM daily_price
)
SELECT
    r.symbol,
    r.date,
    r.close,
    m.ma20,
    m.ma60,
    r.volume,
    r.source,
    r.quality_score
FROM ranked r
JOIN ma m ON r.symbol = m.symbol AND r.date = m.date
WHERE r.rn = 1;

-- 因子计算基础视图（供多因子策略消费）
CREATE OR REPLACE VIEW mv_factor_input AS
SELECT
    dp.symbol,
    dp.date,
    dp.close,
    dp.volume,
    dp.amount,
    sb.market,
    sb.asset_type
FROM daily_price dp
JOIN stock_basic sb ON dp.symbol = sb.code;

""".strip()
