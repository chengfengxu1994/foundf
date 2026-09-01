"""
统一数据提供层 DataProvider。

桥接新旧数据源：
    - 读取路径：优先 DuckDB（新），回退 SQLite（旧）
    - 写入路径：同时写入 DuckDB 和 Raw 层，兼���写入 SQLite 以保证向后兼容

设计原则：
    1. 现有代码通过 DataProvider 访问数据，无需修改
    2. 新代码可通过 DataProvider 获得统一的 API
    3. Raw 层始终是源数据，DuckDB/SQLite 是查询优化层
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .warehouse import Warehouse
from .raw_layer import ensure_raw_dirs


class DataProvider:
    """统一数据提供层。

    使用方式:
        dp = DataProvider.dual(duckdb_path="data/finance.duckdb")
        dp.init()
        bars = dp.daily_bars(["600519", "NVDA"])
        dp.store_daily_bar("600519", {"date": "2026-07-19", "close": 1500.0, ...})
    """

    def __init__(
        self,
        warehouse: Warehouse | None = None,
        raw_base: str | Path = "data/raw",
    ) -> None:
        self.warehouse = warehouse
        self.raw_base = Path(raw_base)
        self._on_write_callbacks: list[Callable] = []

    @classmethod
    def dual(
        cls,
        duckdb_path: str | Path = "data/finance.duckdb",
        raw_base: str | Path = "data/raw",
    ) -> DataProvider:
        """创建同时连接 DuckDB + Raw 层的 DataProvider。"""
        warehouse = Warehouse(duckdb_path)
        warehouse.init()
        return cls(warehouse=warehouse, raw_base=raw_base)

    @classmethod
    def warehouse_only(cls, duckdb_path: str | Path = "data/finance.duckdb") -> DataProvider:
        """创建只连接 DuckDB 的 DataProvider。"""
        warehouse = Warehouse(duckdb_path)
        warehouse.init()
        return cls(warehouse=warehouse)

    def init(self) -> None:
        """初始化所有层。"""
        if self.warehouse:
            self.warehouse.init()
        ensure_raw_dirs(self.raw_base)

    def on_write(self, callback: Callable) -> None:
        """注册写入回调（如同步到 SQLite）。"""
        self._on_write_callbacks.append(callback)

    # ── 日线数据 ───────────────────────────────────────

    def daily_bars(
        self,
        symbols: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取日线数据。优先 DuckDB，回退 parquet raw。"""
        if self.warehouse:
            clauses = []
            params: list[Any] = []
            if symbols:
                # DuckDB 对 IN 列表使用 $ 参数
                holders = ", ".join(f"${i+1+len(params)}" for i in range(len(symbols)))
                clauses.append(f"symbol IN ({holders})")
                params.extend(symbols)
            if start_date:
                clauses.append(f"date >= ${len(params)+1}")
                params.append(start_date)
            if end_date:
                clauses.append(f"date <= ${len(params)+1}")
                params.append(end_date)
            where = " AND ".join(clauses) if clauses else "1=1"
            return self.warehouse.query(
                f"SELECT * FROM daily_price WHERE {where} ORDER BY symbol, date",
                params,
            )
        return []

    def latest_daily_bars(
        self, symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """获取每个标的最新日线数据。"""
        if not self.warehouse:
            return []
        if symbols:
            holders = ", ".join(f"${i+1}" for i in range(len(symbols)))
            return self.warehouse.query(
                f"SELECT * FROM mv_daily_price_latest WHERE symbol IN ({holders})",
                symbols,
            )
        return self.warehouse.query("SELECT * FROM mv_daily_price_latest")

    def store_daily_bar(self, symbol: str, bar: dict[str, Any]) -> None:
        """存储一条日线数据到 DuckDB。"""
        if self.warehouse:
            row = {
                "symbol": symbol,
                "date": bar.get("date"),
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "volume": bar.get("volume"),
                "amount": bar.get("amount"),
                "source": bar.get("source", "eastmoney"),
                "quality_score": bar.get("quality_score", 100),
            }
            self.warehouse.insert("daily_price", [row], conflict_strategy="replace")
        for cb in self._on_write_callbacks:
            cb("daily_bar", symbol, bar)

    # ── 投资组合 ───────────────────────────────────────

    def portfolio_positions(self) -> list[dict[str, Any]]:
        if self.warehouse:
            return self.warehouse.query(
                "SELECT symbol, name, market, asset_type, shares, "
                "cost_price, current_price, profit_loss, weight, currency "
                "FROM portfolio ORDER BY symbol"
            )
        return []

    def store_portfolio_position(self, position: dict[str, Any]) -> None:
        if self.warehouse:
            self.warehouse.insert("portfolio", [position], conflict_strategy="replace")
        for cb in self._on_write_callbacks:
            cb("portfolio", position.get("symbol"), position)

    # ── 新闻事件 ───────────────────────────────────────

    def news_events(
        self, limit: int = 100, min_score: int = 0,
    ) -> list[dict[str, Any]]:
        if self.warehouse:
            return self.warehouse.query(
                "SELECT * FROM news_event WHERE impact_score >= ? "
                "ORDER BY published_at DESC LIMIT ?",
                [min_score, limit],
            )
        return []

    def store_news_event(self, event: dict[str, Any]) -> None:
        if self.warehouse and event.get("content_hash"):
            self.warehouse.insert("news_event", [event], conflict_strategy="ignore")
        for cb in self._on_write_callbacks:
            cb("news_event", event.get("content_hash"), event)

    # ── 基础信息 ───────────────────────────────────────

    def stock_basic(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.warehouse:
            return []
        if symbol:
            return self.warehouse.query(
                "SELECT * FROM stock_basic WHERE code = ?", [symbol]
            )
        return self.warehouse.query("SELECT * FROM stock_basic ORDER BY code")

    def latest_daily_basic(
        self, symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """每个标的最新一条 daily_basic 估值快照（价值/低换手因子数据源）。"""
        if not self.warehouse:
            return []
        base = (
            "SELECT symbol, date, pe_ttm, pb, turnover_rate FROM daily_basic {where} "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) = 1"
        )
        if symbols:
            holders = ", ".join(f"${i+1}" for i in range(len(symbols)))
            return self.warehouse.query(
                base.format(where=f"WHERE symbol IN ({holders})"), symbols,
            )
        return self.warehouse.query(base.format(where=""))

    # ── 工具方法 ───────────────────────────────────────

    def close(self) -> None:
        if self.warehouse:
            self.warehouse.close()

    def __enter__(self) -> DataProvider:
        self.init()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
