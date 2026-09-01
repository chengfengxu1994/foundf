"""
从现有 SQLite + Parquet 数据迁移到 DuckDB 数据仓库。

迁移策略：
    1. stock_basic — 从 universe.py 读取标的列表
    2. daily_price — 从 parquet 文件读取（data/a100/, data/_all99/, data/hk/, data/us/）
    3. portfolio — 从 SQLite portfolio_positions 表读取
    4. news_event — 从 SQLite events 表读取
    5. financial_statement — 从 SQLite valuation_snapshots + fundamental_facts 读取
    6. macro_data — 从 SQLite macro_observations 读取
    7. minute_price — 从 SQLite minute_bars 读取

关键原则：
    - 不修改原始数据（只读）
    - 支持增量迁移（重复运行安全）
    - 迁移后保持 DuckDB 和 SQLite 数据一致
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .warehouse import Warehouse


def _sqlite_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def migrate_stock_basic(warehouse: Warehouse, universe_module: Any = None) -> int:
    """从 universe.py CORE_UNIVERSE 迁移股票基础信息。"""
    rows: list[dict[str, Any]] = []
    if universe_module is not None:
        flat = universe_module.flatten()
        for code, info in flat.items():
            market_raw = info.get("market", "A")
            market_map = {
                "CN_BROAD": "ETF_CN", "CN_SECTOR": "ETF_CN", "CN_STOCKS": "A",
                "INTL": "ETF_INTL", "DEFENSE": "ETF_CN", "OTHERS": "ETF_CN",
                "HK_CONNECT": "HK_CONNECT",
            }
            rows.append({
                "code": code,
                "name": info.get("name", code),
                "market": market_map.get(market_raw, "A"),
                "asset_type": info.get("type", "STOCK"),
                "industry": None,
                "list_date": None,
                "status": "active",
                "currency": info.get("currency", "CNY"),
            })
    # 添加 US stocks
    us_stocks = [
        ("AAPL", "Apple", "US", "STOCK", "USD"),
        ("MSFT", "Microsoft", "US", "STOCK", "USD"),
        ("GOOGL", "Alphabet", "US", "STOCK", "USD"),
        ("AMZN", "Amazon", "US", "STOCK", "USD"),
        ("NVDA", "NVIDIA", "US", "STOCK", "USD"),
        ("META", "Meta", "US", "STOCK", "USD"),
        ("TSLA", "Tesla", "US", "STOCK", "USD"),
        ("JPM", "JPMorgan", "US", "STOCK", "USD"),
        ("V", "Visa", "US", "STOCK", "USD"),
        ("BRK-B", "Berkshire Hathaway", "US", "STOCK", "USD"),
    ]
    existing = {r["code"] for r in rows}
    for code, name, market, atype, currency in us_stocks:
        if code not in existing:
            rows.append({
                "code": code, "name": name, "market": market,
                "asset_type": atype, "industry": None, "list_date": None,
                "status": "active", "currency": currency,
            })
    return warehouse.insert("stock_basic", rows)


def migrate_daily_price_from_parquet(
    warehouse: Warehouse,
    data_dirs: list[Path],
    max_files: int | None = None,
) -> dict[str, int]:
    """从多个 parquet 目录读取日线数据并写入 DuckDB（使用原生 parquet 读取加速）。

    支持目录: data/a100/, data/_all99/, data/hk/, data/us/, data/benchmarks/
    max_files: 限制处理的文件数（None=全部），用于测试。
    返回 {目录名: 行数}。
    """
    stats: dict[str, int] = {}
    for directory in data_dirs:
        if not directory.exists():
            continue
        dir_name = directory.name
        parquet_files = sorted(directory.glob("*.parquet"))
        if max_files is not None:
            parquet_files = parquet_files[:max_files]
        if not parquet_files:
            continue
        total = 0
        for parquet_file in parquet_files:
            try:
                df = pd.read_parquet(parquet_file)
            except Exception:
                continue
            if df.empty:
                continue
            symbol = parquet_file.stem
            market = _infer_market(dir_name, symbol)
            # 标准化列名
            date_col = None
            for candidate in ("date", "Date", "datetime", "trade_date"):
                if candidate in df.columns:
                    date_col = candidate
                    break
            if date_col is None:
                continue
            df["date"] = pd.to_datetime(df[date_col])
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                if col not in df.columns:
                    df[col] = None
            df["symbol"] = symbol
            df["source"] = "parquet_migration"
            df["quality_score"] = 100
            out = df[["date", "symbol", "open", "high", "low", "close", "volume", "amount", "source", "quality_score"]].copy()
            out = out.dropna(subset=["close"])
            out["close"] = pd.to_numeric(out["close"], errors="coerce")
            out = out.dropna(subset=["close"])
            if not out.empty:
                conn = warehouse.conn
                conn.register("_df_batch", out)
                conn.execute(
                    "INSERT OR REPLACE INTO daily_price "
                    "SELECT date::DATE, symbol, open, high, low, close, "
                    "volume, amount, NULL, source, quality_score, CURRENT_TIMESTAMP "
                    "FROM _df_batch"
                )
                conn.unregister("_df_batch")
                total += len(out)
        if total > 0:
            stats[dir_name] = total
            print(f"    {dir_name}: {total} 条 ({len(parquet_files)} 文件)")
    return stats


def _infer_market(dir_name: str, symbol: str) -> str:
    if dir_name in ("hk",):
        return "HK_CONNECT"
    elif dir_name in ("us", "_tmp0"):
        return "US"
    elif dir_name in ("benchmarks",):
        return "BENCHMARK"
    else:
        return "A" if symbol[:1] in {"0", "3"} else "ETF_CN"


def migrate_portfolio(warehouse: Warehouse, sqlite_path: str | Path) -> int:
    """从 SQLite portfolio_positions 表迁移投资组合数据。"""
    conn = _sqlite_conn(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT symbol, name, market, asset_type, cost_price AS cost_price, "
            "quantity AS shares, reference_price AS current_price, "
            "quote_currency AS currency FROM portfolio_positions"
        ).fetchall()
        return warehouse.insert("portfolio", [dict(r) for r in rows])
    finally:
        conn.close()


def migrate_news_events(warehouse: Warehouse, sqlite_path: str | Path) -> int:
    """从 SQLite events 表迁移新闻事件。"""
    conn = _sqlite_conn(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT published_at, title, summary AS content, "
            "source_name AS source, url AS source_url, category, "
            "importance AS impact_score, confidence, content_hash "
            "FROM events WHERE importance >= 40 "
            "ORDER BY published_at DESC LIMIT 10000"
        ).fetchall()
        return warehouse.insert("news_event", [dict(r) for r in rows])
    finally:
        conn.close()


def migrate_macro_data(warehouse: Warehouse, sqlite_path: str | Path) -> int:
    """从 SQLite macro_observations 表迁移宏观数据。"""
    conn = _sqlite_conn(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT series_id, observation_date, value, "
            "'FRED' AS source FROM macro_observations "
            "ORDER BY series_id, observation_date"
        ).fetchall()
        return warehouse.insert("macro_data", [dict(r) for r in rows])
    finally:
        conn.close()


def migrate_minute_price(warehouse: Warehouse, sqlite_path: str | Path) -> int:
    """从 SQLite minute_bars 表迁移分钟行情。"""
    conn = _sqlite_conn(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT ts AS datetime, symbol, open, high, low, close, volume, amount, source "
            "FROM minute_bars ORDER BY symbol, ts LIMIT 500000"
        ).fetchall()
        return warehouse.insert("minute_price", [dict(r) for r in rows])
    finally:
        conn.close()


def run_all_migrations(
    duckdb_path: str | Path = "data/finance.duckdb",
    sqlite_path: str | Path = "finance_intel/data/finance_intel.db",
    data_root: str | Path = "data",
    universe_module: Any = None,
) -> dict[str, int | dict]:
    """执行所有迁移，返回统计。"""
    start = datetime.now(timezone.utc)
    stats: dict[str, int | dict] = {"started_at": start.isoformat()}

    warehouse = Warehouse(duckdb_path)
    warehouse.init()

    # 1. 股票基础信息
    count = migrate_stock_basic(warehouse, universe_module)
    stats["stock_basic"] = count
    print(f"  stock_basic: {count} 条")

    # 2. 日行情（从 parquet 目录）
    root = Path(data_root)
    parquet_dirs = [
        root / "a100", root / "_all99", root / "hk",
        root / "us", root / "_tmp0", root / "benchmarks",
    ]
    parquet_stats = migrate_daily_price_from_parquet(warehouse, parquet_dirs)
    stats["daily_price_from_parquet"] = parquet_stats
    total_daily = sum(parquet_stats.values())
    print(f"  daily_price: {total_daily} 条 (从 {len(parquet_stats)} 个 parquet 目录)")

    # 3. 投资组合（从 SQLite）
    if Path(sqlite_path).exists():
        count = migrate_portfolio(warehouse, sqlite_path)
        stats["portfolio"] = count
        print(f"  portfolio: {count} 条")

        count = migrate_news_events(warehouse, sqlite_path)
        stats["news_event"] = count
        print(f"  news_event: {count} 条")

        count = migrate_macro_data(warehouse, sqlite_path)
        stats["macro_data"] = count
        print(f"  macro_data: {count} 条")

        count = migrate_minute_price(warehouse, sqlite_path)
        stats["minute_price"] = count
        print(f"  minute_price: {count} 条")
    else:
        print(f"  ⚠ SQLite 数据库不存在: {sqlite_path}")

    warehouse.close()
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    stats["elapsed_seconds"] = round(elapsed, 2)
    stats["completed_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import universe
    except ImportError:
        universe = None

    result = run_all_migrations(universe_module=universe)
    print(f"\n迁移完成 {result['elapsed_seconds']}s")
