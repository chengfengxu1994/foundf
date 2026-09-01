"""
importer.py — 原始流水导入器。

将券商 PDF/CSV 原始数据导入 broker_statement_raw 表，
不做任何修正，保持原始字符串。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from foundf_db import Warehouse


# ── 页面类型枚举 ─────────────────────────────────

PageScope = Literal[
    "ACCOUNT_SUMMARY",
    "CONSOLIDATED_POSITION",
    "CN_POSITION",
    "HK_CONNECT_POSITION",
    "CONSOLIDATED_TRANSACTION",
    "CN_TRANSACTION",
    "HK_CONNECT_TRANSACTION",
    "CASH_SUMMARY",
    "CONTINUATION_PAGE",
    "UNKNOWN_PAGE",
]

# ── broker_statement_raw 表 schema（增加 page_scope） ─────
RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_statement_raw (
    row_id              BIGINT,
    source_file         VARCHAR NOT NULL,
    page_number         INTEGER DEFAULT 1,
    page_scope          VARCHAR DEFAULT 'UNKNOWN_PAGE',
    source_row_number   INTEGER NOT NULL,
    raw_text            VARCHAR NOT NULL,
    import_batch_id     VARCHAR NOT NULL,
    parser_version      VARCHAR NOT NULL DEFAULT 'v2',
    imported_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_file, page_number, source_row_number, import_batch_id)
);
"""

# ── broker_account_snapshot 表（快照数据，非流水） ─────────
SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_account_snapshot (
    snapshot_id             VARCHAR PRIMARY KEY,
    statement_date          DATE NOT NULL,
    period_start            DATE,
    period_end              DATE,
    account_scope           VARCHAR NOT NULL DEFAULT 'CONSOLIDATED',
    market_scope            VARCHAR DEFAULT 'CN_AND_HK',
    currency_scope          VARCHAR DEFAULT 'RMB',
    total_assets            DOUBLE,
    security_value_total    DOUBLE,
    security_value_cn       DOUBLE,
    security_value_hk       DOUBLE,
    cash_balance            DOUBLE,
    available_cash          DOUBLE,
    hk_connect_available    DOUBLE,
    withdrawable_cash       DOUBLE,
    frozen_cash             DOUBLE,
    previous_20d_avg_assets DOUBLE,
    source_file             VARCHAR,
    page_number             INTEGER,
    raw_text_snapshot       VARCHAR,
    UNIQUE(statement_date, account_scope, market_scope)
);
"""

# ── broker_economic_event 表（去重后的真实经济事件） ────
ECONOMIC_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_economic_event (
    economic_event_id   VARCHAR PRIMARY KEY,
    trade_date          DATE NOT NULL,
    event_type          VARCHAR NOT NULL,
    market              VARCHAR DEFAULT '',
    symbol              VARCHAR DEFAULT '',
    security_name       VARCHAR DEFAULT '',
    quantity            DOUBLE DEFAULT 0,
    price               DOUBLE DEFAULT 0,
    gross_amount        DOUBLE DEFAULT 0,
    commission          DOUBLE DEFAULT 0,
    stamp_duty          DOUBLE DEFAULT 0,
    transfer_fee        DOUBLE DEFAULT 0,
    other_fee           DOUBLE DEFAULT 0,
    total_fee           DOUBLE DEFAULT 0,
    cash_delta          DOUBLE DEFAULT 0,
    currency            VARCHAR DEFAULT 'CNY',
    source_occurrences  VARCHAR DEFAULT '[]',  -- JSON array 记录多来源
    primary_source      VARCHAR DEFAULT '',
    dedup_status        VARCHAR DEFAULT 'UNIQUE',
    UNIQUE(trade_date, symbol, event_type, quantity, price)
);
"""

# ── broker_event_source_link 表 ──────────────────────
SOURCE_LINK_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_event_source_link (
    link_id             BIGINT,
    economic_event_id   VARCHAR NOT NULL,
    source_file         VARCHAR NOT NULL,
    page_number         INTEGER,
    page_scope          VARCHAR,
    source_row_number   INTEGER,
    fee_breakdown       VARCHAR DEFAULT '{}',
    UNIQUE(economic_event_id, source_file, page_number, source_row_number)
);
"""

# broker_transaction_normalized 表 schema
NORMALIZED_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_transaction_normalized (
    transaction_id          BIGINT,
    trade_date              DATE NOT NULL,
    event_type              VARCHAR NOT NULL,
    market                  VARCHAR DEFAULT '',
    symbol                  VARCHAR DEFAULT '',
    security_name           VARCHAR DEFAULT '',
    quantity                DOUBLE DEFAULT 0,
    price                   DOUBLE DEFAULT 0,
    gross_amount            DOUBLE DEFAULT 0,
    commission              DOUBLE DEFAULT 0,
    stamp_duty              DOUBLE DEFAULT 0,
    transfer_fee            DOUBLE DEFAULT 0,
    other_fee               DOUBLE DEFAULT 0,
    total_fee               DOUBLE DEFAULT 0,
    cash_delta              DOUBLE DEFAULT 0,
    position_delta          DOUBLE DEFAULT 0,
    broker_cash_balance     DOUBLE,
    currency                VARCHAR DEFAULT 'CNY',
    source_row_number       INTEGER,
    classification_confidence DOUBLE DEFAULT 1.0,
    classification_reason   VARCHAR DEFAULT '',
    requires_manual_review  BOOLEAN DEFAULT FALSE,
    UNIQUE(trade_date, source_row_number)
);
"""

# portfolio_ledger 表 schema
LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_ledger (
    entry_id            BIGINT,
    trade_date          DATE NOT NULL,
    transaction_id      BIGINT,
    event_type          VARCHAR NOT NULL,
    symbol              VARCHAR DEFAULT '',
    market              VARCHAR DEFAULT '',
    quantity_change     DOUBLE DEFAULT 0,
    quantity_after      DOUBLE DEFAULT 0,
    price               DOUBLE DEFAULT 0,
    cost_basis_change   DOUBLE DEFAULT 0,
    cost_basis_after    DOUBLE DEFAULT 0,
    avg_cost_after      DOUBLE DEFAULT 0,
    cash_change         DOUBLE DEFAULT 0,
    cash_balance        DOUBLE DEFAULT 0,
    realized_pnl        DOUBLE DEFAULT 0,
    unrealized_pnl      DOUBLE DEFAULT 0,
    commission          DOUBLE DEFAULT 0,
    stamp_duty          DOUBLE DEFAULT 0,
    transfer_fee        DOUBLE DEFAULT 0,
    cost_method         VARCHAR DEFAULT 'WEIGHTED_AVERAGE',
    UNIQUE(transaction_id, symbol)
);
"""


class StatementImporter:
    """券商流水导入器。

    从 SQLite（已解析的 PDF 数据）导入到三层表结构。
    """

    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse
        self._init_tables()
        self.batch_id = f"import_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def _init_tables(self) -> None:
        all_schemas = [
            RAW_SCHEMA, SNAPSHOT_SCHEMA, ECONOMIC_EVENT_SCHEMA,
            SOURCE_LINK_SCHEMA, NORMALIZED_SCHEMA, LEDGER_SCHEMA,
        ]
        for schema in all_schemas:
            for stmt in schema.split(";"):
                s = stmt.strip()
                if s:
                    self.warehouse.execute(s + ";")

    def import_from_sqlite(self, sqlite_path: str | Path) -> dict[str, int]:
        """从 SQLite trade_history 导入原始记录到三层表。"""
        import sqlite3
        conn = sqlite3.connect(str(sqlite_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *, rowid FROM trade_history ORDER BY trade_date, rowid
        """).fetchall()
        conn.close()

        raw_count = 0
        norm_count = 0

        for r in rows:
            row_id = r["id"]
            source_file = r["source_file"]

            # 1. 写入 broker_statement_raw
            raw_text = (f"{r['trade_date']}|{r['side']}|{r['symbol']}|"
                        f"{r['name']}|{r['quantity']}|{r['trade_price']}|"
                        f"{r['gross_amount_cny']}|佣金{r['commission_cny']}|"
                        f"印花税{r['stamp_tax_cny']}|过户费{r['trading_fee_cny']}")
            fp = self._fingerprint(source_file, row_id, raw_text)
            self.warehouse.execute(
                "INSERT OR IGNORE INTO broker_statement_raw "
                "(row_id, source_file, source_row_number, raw_text, import_batch_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [row_id, source_file, row_id, raw_text, self.batch_id],
            )
            raw_count += 1

            # 2. 写入 broker_transaction_normalized
            total_fee = (r["commission_cny"] + r["stamp_tax_cny"] +
                         r["levy_cny"] + r["trading_fee_cny"] +
                         r["system_fee_cny"] + r["settlement_fee_cny"] +
                         r["other_fee_cny"])
            gross = r["gross_amount_cny"]
            qty = r["quantity"]
            price = r["trade_price"]

            # cash_delta: HK Connect 总发生金额已经是净结算金额
            # 买入为负（资金减少），卖出为正（资金增加）
            if r["side"] == "BUY":
                cash_delta = -abs(gross)
                pos_delta = qty
            else:
                cash_delta = abs(gross)
                pos_delta = -qty

            self.warehouse.execute(
                "INSERT OR IGNORE INTO broker_transaction_normalized "
                "(transaction_id, trade_date, event_type, market, symbol, "
                "security_name, quantity, price, gross_amount, commission, "
                "stamp_duty, transfer_fee, other_fee, total_fee, "
                "cash_delta, position_delta, broker_cash_balance, "
                "source_row_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    row_id, r["trade_date"], r["side"], r["market"],
                    r["symbol"], r["name"],
                    qty, price, gross,
                    r["commission_cny"], r["stamp_tax_cny"],
                    r["trading_fee_cny"],
                    r["levy_cny"] + r["system_fee_cny"] +
                    r["settlement_fee_cny"] + r["other_fee_cny"],
                    round(total_fee, 2),
                    round(cash_delta, 2), pos_delta,
                    None,  # broker_cash_balance 未从PDF解析
                    row_id,
                ],
            )
            norm_count += 1

        return {"raw": raw_count, "normalized": norm_count}

    def _fingerprint(self, source_file: str, row_number: int, raw_text: str) -> str:
        """生成稳定的指纹用于去重。"""
        raw = f"{source_file}|{row_number}|{raw_text}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def import_snapshot(self, snapshot: dict[str, Any]) -> None:
        """导入一条账户快照（不覆盖已有快照）。"""
        sid = hashlib.md5(
            f"{snapshot['statement_date']}|{snapshot.get('account_scope','CONSOLIDATED')}".encode()
        ).hexdigest()[:16]
        self.warehouse.execute(
            "INSERT OR IGNORE INTO broker_account_snapshot "
            "(snapshot_id, statement_date, period_start, period_end, "
            "account_scope, market_scope, currency_scope, "
            "total_assets, security_value_total, security_value_cn, security_value_hk, "
            "cash_balance, available_cash, hk_connect_available, "
            "previous_20d_avg_assets, source_file, page_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                sid,
                snapshot.get("statement_date"),
                snapshot.get("period_start"),
                snapshot.get("period_end"),
                snapshot.get("account_scope", "CONSOLIDATED"),
                snapshot.get("market_scope", "CN_AND_HK"),
                snapshot.get("currency_scope", "RMB"),
                snapshot.get("total_assets"),
                snapshot.get("security_value_total"),
                snapshot.get("security_value_cn"),
                snapshot.get("security_value_hk"),
                snapshot.get("cash_balance"),
                snapshot.get("available_cash"),
                snapshot.get("hk_connect_available"),
                snapshot.get("previous_20d_avg_assets"),
                snapshot.get("source_file"),
                snapshot.get("page_number", 1),
            ],
        )

    @staticmethod
    def classify_page(text_block: str, source_file: str = "") -> str:
        """根据文本内容判断页面类型。"""
        has_total_assets = "总资产" in text_block
        has_security_value = "证券市值" in text_block
        has_cash_balance = "资金余额" in text_block
        has_transaction_header = any(kw in text_block for kw in ["发生日期", "成交数量", "资金流水明细"])
        has_hk_keywords = any(kw in text_block for kw in ["港股通", "沪港通"])
        has_cn_keywords = any(kw in text_block for kw in ["深交所", "上交所", "深市", "沪市", "深A", "沪A"])
        has_position_header = "持仓数量" in text_block or "成本价" in text_block
        has_summary_block = has_total_assets and has_security_value
        has_fee_header = any(kw in text_block for kw in ["交易征费", "交收费", "结算汇率"])
        is_hk_file = "港股通" in source_file or "hk" in source_file.lower()

        date_count = len(re.findall(r"^\d{8}$", text_block, re.MULTILINE))

        # 有费用头 = HK详细流水
        if has_fee_header:
            return "HK_CONNECT_TRANSACTION"
        # 有CN关键字
        if has_cn_keywords:
            if has_summary_block:
                return "CONSOLIDATED"
            if has_transaction_header:
                return "CN_TRANSACTION"
        # 大量交易日期行
        if date_count >= 3 and not has_summary_block:
            if is_hk_file:
                return "HK_CONNECT_TRANSACTION"
            return "CN_TRANSACTION"
        # 空页
        if date_count == 0 and not has_summary_block and not has_transaction_header:
            return "CONTINUATION_PAGE"

        if has_summary_block and has_transaction_header:
            if has_hk_keywords:
                return "HK_CONNECT_MIXED"
            return "CONSOLIDATED"
        if has_summary_block and not has_transaction_header:
            return "ACCOUNT_SUMMARY"
        if has_transaction_header and has_hk_keywords:
            return "HK_CONNECT_TRANSACTION"
        if has_transaction_header:
            return "CN_TRANSACTION"
        if has_position_header:
            if has_hk_keywords:
                return "HK_CONNECT_POSITION"
            return "CN_POSITION"
        if has_cash_balance and not has_transaction_header:
            return "CASH_SUMMARY"

    def deduplicate_events(self) -> dict[str, int]:
        """从 broker_transaction_normalized 生成去重后的 economic_event。"""
        txns = self.warehouse.query(
            "SELECT * FROM broker_transaction_normalized ORDER BY trade_date"
        )
        deduped = 0
        unique = 0

        for t in txns:
            eid = hashlib.md5(
                f"{t['trade_date']}|{t['event_type']}|{t['symbol']}|{t['quantity']}|{t['price']}".encode()
            ).hexdigest()[:16]

            existing = self.warehouse.query(
                "SELECT economic_event_id FROM broker_economic_event "
                "WHERE economic_event_id = ?", [eid]
            )
            if existing:
                # 更新 source_occurrences 记录重复来源
                occ = self.warehouse.query(
                    "SELECT source_occurrences FROM broker_economic_event "
                    "WHERE economic_event_id = ?", [eid]
                )
                if occ:
                    sources = json.loads(occ[0]["source_occurrences"])
                    sources.append({
                        "source_row": t["source_row_number"],
                        "page_scope": "HK_CONNECT_TRANSACTION",
                    })
                    self.warehouse.execute(
                        "UPDATE broker_economic_event SET source_occurrences=? "
                        "WHERE economic_event_id=?",
                        [json.dumps(sources, ensure_ascii=False), eid],
                    )
                deduped += 1
            else:
                self.warehouse.execute(
                    "INSERT INTO broker_economic_event "
                    "(economic_event_id, trade_date, event_type, market, symbol, "
                    "security_name, quantity, price, gross_amount, "
                    "commission, stamp_duty, transfer_fee, other_fee, total_fee, "
                    "cash_delta, currency, primary_source, dedup_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNIQUE')",
                    [
                        eid, t["trade_date"], t["event_type"], t["market"],
                        t["symbol"], "", t["quantity"], t["price"],
                        t["gross_amount"], t["commission"], t["stamp_duty"],
                        t["transfer_fee"], t["other_fee"], t["total_fee"],
                        t["cash_delta"], t["currency"], "sqlite_import",
                    ],
                )
                unique += 1

        return {"unique_events": unique, "duplicates_removed": deduped}

    def count_unknown(self) -> int:
        """统计规范化交易中 event_type=UNKNOWN 的记录数。"""
        rows = self.warehouse.query(
            "SELECT COUNT(*) as c FROM broker_transaction_normalized "
            "WHERE event_type = 'UNKNOWN'"
        )
        return rows[0]["c"] if rows else 0

    def list_transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.warehouse.query(
            "SELECT * FROM broker_transaction_normalized "
            "ORDER BY trade_date, source_row_number LIMIT ?",
            [limit],
        )
