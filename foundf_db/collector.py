"""
增量数据采集器 — 统一数据采集入口。

采集流程:
    1. 从数据源拉取最新数据（A股/港股/美股/宏观）
    2. 保存到 Raw 层（data/raw/ 只写不修改）
    3. 更新 DuckDB 数据仓库
    4. 返回采集统计

数据源:
    A股  → baostock + Tushare
    港股 → yfinance
    美股 → yfinance
    宏观基准 → yfinance
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .warehouse import Warehouse
from .raw_layer import ensure_raw_dirs, raw_path, save_raw_data, save_raw_news


class DataCollector:
    """增量数据采集器。

    使用方式:
        collector = DataCollector("data/finance.duckdb")
        stats = collector.run_daily_incremental()
        print(stats)
    """

    def __init__(
        self,
        duckdb_path: str | Path = "data/finance.duckdb",
        raw_base: str | Path = "data/raw",
        lookback_days: int = 365 * 3,  # 首次采集回溯3年
    ):
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        self.raw_base = Path(raw_base)
        self.lookback_days = lookback_days
        ensure_raw_dirs(self.raw_base)

    # ── 公共采集入口 ──────────────────────────────────

    def run_daily_incremental(
        self,
        cn_symbols: list[str] | None = None,
        hk_symbols: list[str] | None = None,
        us_symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        """执行每日增量数据采集。返回统计。"""
        stats: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}

        # 检查上次采集时间，决定是增量还是全量
        last_run = self._last_collect_time("daily_price")
        is_first_run = last_run is None
        fetch_start = (datetime.now() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d") if is_first_run else last_run

        print(f"增量采集 {'首次全量' if is_first_run else '增量更新'}, 起始: {fetch_start}")

        # 1. A 股
        if cn_symbols:
            count = self._collect_cn_stocks(cn_symbols, fetch_start)
            stats["cn_stock"] = count
            print(f"  A股: {count} 条")

        # 2. 港股
        if hk_symbols:
            count = self._collect_hk_stocks(hk_symbols, fetch_start)
            stats["hk_stock"] = count
            print(f"  港股: {count} 条")

        # 3. 美股
        if us_symbols:
            count = self._collect_us_stocks(us_symbols, fetch_start)
            stats["us_stock"] = count
            print(f"  美股: {count} 条")

        # 4. 宏观基准
        benchmark_symbols = {
            "CSI300": "000300.SS",
            "SP500": "^GSPC",
            "NASDAQ": "^IXIC",
            "HSI": "^HSI",
            "HK_TECH": "3067.HK",
            "N225": "^N225",
            "GOLD": "GC=F",
        }
        count = self._collect_benchmarks(benchmark_symbols, fetch_start)
        stats["benchmarks"] = count
        print(f"  基准: {count} 条")

        self._mark_collect_time()
        stats["completed_at"] = datetime.now(timezone.utc).isoformat()
        return stats

    # ── A 股采集 ────────────────────────────────────────

    def _collect_cn_stocks(self, symbols: list[str], start_date: str) -> int:
        """从 baostock 采集 A 股日线。"""
        try:
            import baostock as bs
        except ImportError:
            print("    ⚠ baostock 未安装，跳过 A 股采集")
            return 0

        total = 0
        bs.login()
        try:
            for symbol in symbols:
                prefix = "sh." if symbol[:1] in ("5", "6", "9", "68") else "sz."
                code = f"{prefix}{symbol}"
                try:
                    rs = bs.query_history_k_data_plus(
                        code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start_date,
                        frequency="d",
                        adjustflag="2",
                    )
                    if rs.error_code != "0":
                        continue
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if not rows:
                        continue
                    df = pd.DataFrame(rows, columns=rs.fields)
                    df["date"] = pd.to_datetime(df["date"])
                    for col in ["open", "high", "low", "close"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
                    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

                    # 保存到 Raw 层
                    save_raw_data(df, symbol, "A", self.raw_base, source="baostock")

                    # 更新 DuckDB
                    market = "A" if symbol[:1] in ("0", "3") else "ETF_CN"
                    self._insert_to_warehouse(df, symbol, market, "baostock")
                    total += len(df)
                    time.sleep(0.3)
                except Exception:
                    continue
        finally:
            bs.logout()
        return total

    # ── 港股采集 ────────────────────────────────────────

    def _collect_hk_stocks(self, symbols: list[str], start_date: str) -> int:
        total = 0
        for symbol in symbols:
            try:
                df = self._yfinance_download(symbol, start_date)
                if df is None or df.empty:
                    continue
                save_raw_data(df, symbol, "HK_CONNECT", self.raw_base, source="yfinance")
                self._insert_to_warehouse(df, symbol, "HK_CONNECT", "yfinance")
                total += len(df)
                time.sleep(0.5)
            except Exception:
                continue
        return total

    # ── 美股采集 ────────────────────────────────────────

    def _collect_us_stocks(self, symbols: list[str], start_date: str) -> int:
        total = 0
        for symbol in symbols:
            try:
                df = self._yfinance_download(symbol, start_date)
                if df is None or df.empty:
                    continue
                save_raw_data(df, symbol, "US", self.raw_base, source="yfinance")
                self._insert_to_warehouse(df, symbol, "US", "yfinance")
                total += len(df)
                time.sleep(0.5)
            except Exception:
                continue
        return total

    # ── 宏观基准采集 ────────────────────────────────────

    def _collect_benchmarks(self, benchmarks: dict[str, str], start_date: str) -> int:
        total = 0
        for name, ticker in benchmarks.items():
            try:
                df = self._yfinance_download(ticker, start_date)
                if df is None or df.empty:
                    continue
                save_raw_data(df, name, "BENCHMARK", self.raw_base, source="yfinance")
                self._insert_to_warehouse(df, name, "BENCHMARK", "yfinance")
                total += len(df)
                time.sleep(0.5)
            except Exception:
                continue
        return total

    # ── 工具方法 ────────────────────────────────────────

    def _yfinance_download(self, symbol: str, start_date: str) -> pd.DataFrame | None:
        try:
            import yfinance as yf
        except ImportError:
            return None
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date)
        if df.empty:
            return None
        df = df.reset_index()
        col_map = {
            "Date": "date", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
        for c in ["open", "high", "low", "close"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def _insert_to_warehouse(self, df: pd.DataFrame, symbol: str, market: str, source: str) -> int:
        """将 DataFrame 写入 DuckDB daily_price 表。"""
        out = df[["date", "open", "high", "low", "close"]].copy()
        out["symbol"] = symbol
        out["source"] = source
        out["quality_score"] = 100
        out = out.dropna(subset=["close"])
        if out.empty:
            return 0
        if "volume" in df.columns:
            out["volume"] = df["volume"]
        if "amount" in df.columns:
            out["amount"] = df["amount"]
        self.warehouse.conn.register("_collect_batch", out)
        self.warehouse.conn.execute("""
            INSERT OR REPLACE INTO daily_price
            SELECT date::DATE, symbol, open, high, low, close,
                   volume, amount, NULL, source, quality_score, CURRENT_TIMESTAMP
            FROM _collect_batch
        """)
        self.warehouse.conn.unregister("_collect_batch")
        return len(out)

    def _last_collect_time(self, table: str) -> str | None:
        """查询 DuckDB 中最新的数据日期。"""
        rows = self.warehouse.query(
            f"SELECT MAX(date)::VARCHAR AS last_date FROM {table}"
        )
        return rows[0]["last_date"] if rows and rows[0]["last_date"] else None

    def _mark_collect_time(self) -> None:
        """记录采集时间戳。"""
        pass  # DuckDB 的 daily_price.fetched_at 已自动记录

    def close(self) -> None:
        self.warehouse.close()
