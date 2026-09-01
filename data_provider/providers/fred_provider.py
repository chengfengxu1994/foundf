"""
FRED 宏观数据提供者。

数据来源：Federal Reserve Economic Data (FRED)
API Key 从环境变量读取：FRED_API_KEY
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from ..base import (
    DataProvider,
    FinancialData,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)

FRED_API = "https://api.stlouisfed.org/fred"

# 默认关注的宏观序列
DEFAULT_SERIES = {
    "FEDFUNDS": "Federal Funds Effective Rate",
    "CPIAUCSL": "CPI All Urban Consumers",
    "UNRATE": "Unemployment Rate",
    "GDP": "Gross Domestic Product",
    "DGS10": "10-Year Treasury Rate",
    "DGS2": "2-Year Treasury Rate",
    "T10Y2Y": "10-Year-2-Year Yield Spread",
}


class FREDProvider(DataProvider):
    """FRED 宏观数据提供者。"""

    def __init__(self, api_key: str | None = None):
        super().__init__("fred")
        self.api_key = api_key or os.getenv("FRED_API_KEY", "")
        self._client = httpx.Client(timeout=20)

    def get_stock_basic(self) -> list[StockBasic]:
        return []

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[TradeCalendar]:
        return []

    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list:
        return []

    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None,
    ) -> list:
        return []

    # ── FRED 特定接口 ─────────────────────────────────

    def get_series(self, series_id: str, start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
        """获取 FRED 时间序列数据。"""
        if not self.api_key:
            raise RuntimeError("FRED_API_KEY 未设置")

        params: dict[str, str] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
        }
        if start_date:
            params["observation_start"] = start_date
        if end_date:
            params["observation_end"] = end_date

        try:
            resp = self._client.get(
                f"{FRED_API}/series/observations",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            observations = data.get("observations", [])
            results = []
            for obs in observations:
                value = obs.get("value", ".")
                if value == ".":
                    continue
                results.append({
                    "series_id": series_id,
                    "date": obs["date"],
                    "value": float(value),
                })
            self.record_success()
            return results
        except Exception as e:
            self.record_error()
            raise RuntimeError(f"FRED API 错误 ({series_id}): {e}")

    def get_all_series(self, start_date: str = "", end_date: str = "") -> dict[str, list[dict[str, Any]]]:
        """获取所有默认序列的数据。"""
        result = {}
        for sid in DEFAULT_SERIES:
            try:
                data = self.get_series(sid, start_date, end_date)
                if data:
                    result[sid] = data
            except RuntimeError:
                continue
        return result

    def health_check(self) -> ProviderHealth:
        if not self.api_key:
            return ProviderHealth(
                provider_name=self.name,
                status="degraded",
                api_available=False,
                message="FRED_API_KEY 未配置，跳过",
            )
        try:
            data = self.get_series("FEDFUNDS", "2026-01-01", "2026-01-10")
            ok = len(data) > 0
            return ProviderHealth(
                provider_name=self.name,
                status="healthy" if ok else "degraded",
                api_available=ok,
                message=f"FEDFUNDS: {len(data)} 条" if ok else "空数据",
            )
        except Exception as e:
            return ProviderHealth(
                provider_name=self.name,
                status="error",
                api_available=False,
                message=str(e),
            )
