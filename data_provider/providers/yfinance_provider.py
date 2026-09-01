"""
yfinance 数据提供者（港股+美股 fallback）。

用于：
    - 美股日线
    - 港股日线（当 Tushare/HKEX 不可用时）
    - 宏观基准指数

从环境变量读取代理配置（如需要）：
    HTTP_PROXY, HTTPS_PROXY
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..base import (
    DailyPrice,
    DataProvider,
    FinancialData,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)


US_STOCKS = [
    ("AAPL", "Apple", "US"), ("MSFT", "Microsoft", "US"),
    ("GOOGL", "Alphabet", "US"), ("AMZN", "Amazon", "US"),
    ("NVDA", "NVIDIA", "US"), ("META", "Meta", "US"),
    ("TSLA", "Tesla", "US"), ("JPM", "JPMorgan", "US"),
    ("V", "Visa", "US"), ("BRK-B", "Berkshire Hathaway", "US"),
]

BENCHMARKS = {
    "CSI300": "000300.SS", "SP500": "^GSPC", "NASDAQ": "^IXIC",
    "HSI": "^HSI", "N225": "^N225", "GOLD": "GC=F",
}


class YFinanceProvider(DataProvider):
    """yfinance 数据提供者（港股+美股+宏观基准）。"""

    def __init__(self, cache_dir: str | Path = "data/raw/global/yfinance"):
        super().__init__("yfinance")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _archive_history(
        self,
        *,
        symbol: str,
        start_date: str,
        end_date: str,
        rows: list[dict[str, Any]],
    ) -> None:
        now = datetime.now(timezone.utc)
        archive_dir = self.cache_dir / now.strftime("%Y/%m/%d")
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / (
            f"{now.strftime('%H%M%S.%fZ')}_history_{uuid4().hex}.json"
        )
        with path.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": "foundf.raw.yfinance_history.v1",
                    "provider": "yfinance",
                    "symbol": symbol,
                    "request": {
                        "start": start_date,
                        "end_exclusive": end_date,
                        "auto_adjust": False,
                        "actions": False,
                    },
                    "fetched_at": now.isoformat(),
                    "rows": rows,
                },
                handle,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )

    def get_stock_basic(self) -> list[StockBasic]:
        results = []
        for code, name, market in US_STOCKS:
            results.append(StockBasic(code=code, name=name, market=market))
        return results

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[TradeCalendar]:
        return []

    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list[DailyPrice]:
        try:
            import yfinance as yf
        except ImportError:
            raise RuntimeError("yfinance 未安装: pip install yfinance")

        ticker = yf.Ticker(symbol)
        df = ticker.history(
            start=start_date,
            end=end_date,
            auto_adjust=False,
            actions=False,
        )
        if df.empty:
            self._archive_history(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                rows=[],
            )
            return []

        df = df.reset_index()
        self._archive_history(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            rows=df.to_dict(orient="records"),
        )
        col_map = {"Date": "date", "Open": "open", "High": "high",
                   "Low": "low", "Close": "close", "Volume": "volume"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        results: list[DailyPrice] = []
        for _, row in df.iterrows():
            try:
                date_val = row.get("date")
                if date_val is None:
                    continue
                date_str = (
                    date_val.strftime("%Y-%m-%d")
                    if hasattr(date_val, "strftime")
                    else str(date_val)[:10]
                )
                results.append(DailyPrice(
                    date=date_str,
                    symbol=symbol,
                    open=float(row.get("open", 0) or 0),
                    high=float(row.get("high", 0) or 0),
                    low=float(row.get("low", 0) or 0),
                    close=float(row.get("close", 0) or 0),
                    volume=float(row.get("volume", 0) or 0),
                ))
            except (ValueError, TypeError):
                continue

        self.record_success()
        return results

    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None,
    ) -> list[FinancialData]:
        return []

    def get_benchmarks(self, start_date: str, end_date: str) -> dict[str, list[DailyPrice]]:
        """获取宏观基准指数。"""
        result = {}
        for name, ticker in BENCHMARKS.items():
            prices = self.get_daily_price(ticker, start_date, end_date)
            if prices:
                result[name] = prices
        return result

    def get_recent_quotes(
        self,
        symbols: list[str],
        *,
        interval: str = "1m",
        period: str = "2d",
    ) -> list[dict[str, Any]]:
        """批量获取最近报价。

        Yahoo/yfinance 是个人研究用途的非官方适配器。即使数据时间很近，也不在这里
        推断交易所授权的实时等级；调用方必须把 ``delay_minutes=None`` 解释为延迟
        未核验，而不是零延迟。
        """
        if not symbols:
            return []
        try:
            import pandas as pd
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance/pandas 未安装") from exc

        try:
            frame = yf.download(
                tickers=sorted(set(symbols)),
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
                timeout=20,
            )
        except Exception as exc:
            self.record_error()
            raise RuntimeError(f"Yahoo 行情批量请求失败: {exc}") from exc
        if frame is None or frame.empty:
            self.record_error()
            return []

        now = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []
        many = isinstance(frame.columns, pd.MultiIndex)
        for symbol in sorted(set(symbols)):
            try:
                if many and symbol in frame.columns.levels[0]:
                    sub = frame[symbol]
                elif many and symbol in frame.columns.levels[1]:
                    sub = frame.xs(symbol, axis=1, level=1)
                elif many:
                    continue
                else:
                    sub = frame
                close = sub["Close"].dropna()
                if close.empty:
                    continue
                index_value = close.index[-1]
                if hasattr(index_value, "to_pydatetime"):
                    quote_dt = index_value.to_pydatetime()
                else:
                    quote_dt = datetime.fromisoformat(str(index_value))
                if quote_dt.tzinfo is None:
                    quote_dt = quote_dt.replace(tzinfo=timezone.utc)
                quote_dt = quote_dt.astimezone(timezone.utc)
                row = sub.loc[close.index[-1]]

                def number(field: str) -> float | None:
                    value = row.get(field)
                    if value is None or pd.isna(value):
                        return None
                    value = float(value)
                    return value if value == value else None

                price = number("Close")
                if price is None or price <= 0:
                    continue
                previous_rows = close[
                    close.index.map(
                        lambda value: (
                            value.to_pydatetime()
                            if hasattr(value, "to_pydatetime")
                            else datetime.fromisoformat(str(value))
                        ).date()
                        < quote_dt.date()
                    )
                ]
                previous_close = (
                    float(previous_rows.iloc[-1]) if not previous_rows.empty else None
                )
                age_seconds = max(0.0, (now - quote_dt).total_seconds())
                if age_seconds <= 5 * 60:
                    freshness = "RECENT"
                    quality = "RECENT_SOURCE_UNVERIFIED"
                elif age_seconds <= 36 * 3600:
                    freshness = "DELAYED_OR_CLOSED"
                    quality = "DELAY_NOT_VERIFIED"
                else:
                    freshness = "STALE"
                    quality = "STALE"
                results.append(
                    {
                        "provider_symbol": symbol,
                        "quote_time": quote_dt.isoformat(),
                        "fetched_at": now.isoformat(),
                        "price": price,
                        "previous_close": previous_close,
                        "open": number("Open"),
                        "high": number("High"),
                        "low": number("Low"),
                        "volume": number("Volume"),
                        "market_state": "UNKNOWN",
                        "source": self.name,
                        "source_tier": "UNOFFICIAL_PUBLIC_API",
                        "delay_minutes": None,
                        "freshness": freshness,
                        "quality_status": quality,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        if results:
            self.record_success()
        else:
            self.record_error()
        return results

    def health_check(self) -> ProviderHealth:
        try:
            prices = self.get_daily_price("SPY", "2026-01-01", "2026-01-10")
            ok = len(prices) > 0
            return ProviderHealth(
                provider_name=self.name,
                status="healthy" if ok else "degraded",
                api_available=ok,
                data_latency_days=1 if ok else 7,
                message=f"SPY: {len(prices)} 条" if ok else "无法获取数据",
            )
        except Exception as e:
            return ProviderHealth(
                provider_name=self.name,
                status="error",
                api_available=False,
                message=str(e),
            )
