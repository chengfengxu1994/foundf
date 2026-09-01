"""
SEC EDGAR 数据提供者（美股财务数据）。

使用 SEC EDGAR 公开 API：
    - companyfacts — 获取公司财务数据
    - companyconcept — 获取特定概念数据

User-Agent 从环境变量读取：SEC_USER_AGENT
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..base import (
    DailyPrice,
    DataProvider,
    FinancialData,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)

SEC_BASE = "https://data.sec.gov"
COMPANY_FACTS_URL = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{{cik}}.json"


# GAAP 标签映射
GAAP_TAGS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "revenue_alt": "Revenues",
    "net_income": "NetIncomeLoss",
    "operating_cf": "NetCashProvidedByOperateActivities",
    "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "equity": "StockholdersEquity",
    "assets": "Assets",
    "liabilities": "Liabilities",
}


class SECProvider(DataProvider):
    """SEC EDGAR 数据提供者（美股财务数据）。"""

    def __init__(self):
        super().__init__("sec_edgar")
        self.user_agent = os.getenv("SEC_USER_AGENT") or "FoundF/1.0 (contact@example.com)"
        self._client = httpx.Client(
            timeout=30,
            headers={"User-Agent": self.user_agent},
        )

    def get_stock_basic(self) -> list[StockBasic]:
        return []

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[TradeCalendar]:
        return []

    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list[DailyPrice]:
        return []

    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None,
    ) -> list[FinancialData]:
        """从 SEC EDGAR 获取公司财务数据。"""
        cik = self._resolve_cik(symbol)
        if not cik:
            return []

        url = COMPANY_FACTS_URL.format(cik=cik)
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            self.record_error()
            return []

        facts = data.get("facts", {}).get("us-gaap", {})
        results: list[FinancialData] = []
        annual_data: dict[str, dict[str, Any]] = {}

        for metric, tag in GAAP_TAGS.items():
            tag_data = facts.get(tag) or facts.get(GAAP_TAGS.get(f"{metric}_alt", ""))
            if not tag_data:
                continue
            units = tag_data.get("units", {}).get("USD") or tag_data.get("units", {}).get("CNY")
            if not units:
                continue

            for entry in units:
                if entry.get("fp") != "FY":
                    continue  # 只取年报
                end_date_str = str(entry.get("end", ""))[:10]
                if end_date_str not in annual_data:
                    annual_data[end_date_str] = {"symbol": symbol, "report_date": end_date_str}

                filed = str(entry.get("filed", ""))[:10] or end_date_str
                annual_data[end_date_str]["filed_at"] = filed
                annual_data[end_date_str][metric] = float(entry.get("val", 0))

        for end_date_str, vals in annual_data.items():
            fd = FinancialData(
                symbol=symbol,
                report_date=end_date_str,
                report_type="annual",
                filed_at=vals.get("filed_at", ""),
                revenue=vals.get("revenue"),
                profit=vals.get("net_income"),
                cashflow=vals.get("operating_cf"),
            )
            # 计算 ROE/ROIC
            if vals.get("net_income") and vals.get("equity") and vals["equity"] != 0:
                fd.roe = vals["net_income"] / vals["equity"]
            results.append(fd)

        self.record_success()
        # 按报告日期排序
        results.sort(key=lambda r: r.report_date, reverse=True)
        return results

    def health_check(self) -> ProviderHealth:
        try:
            resp = self._client.get(f"{SEC_BASE}/", timeout=10)
            ok = resp.status_code == 200
            return ProviderHealth(
                provider_name=self.name,
                status="healthy" if ok else "degraded",
                api_available=ok,
                message="SEC EDGAR 可访问" if ok else f"HTTP {resp.status_code}",
            )
        except Exception as e:
            return ProviderHealth(
                provider_name=self.name,
                status="error",
                api_available=False,
                message=str(e),
            )

    def _resolve_cik(self, symbol: str) -> str:
        """通过 ticker 查询 CIK 编号。"""
        cik_map = {
            "AAPL": "0000320193", "MSFT": "0000789019",
            "GOOGL": "0001652044", "AMZN": "0001018724",
            "NVDA": "0001045810", "META": "0001326801",
            "TSLA": "0001318605", "JPM": "0000019617",
            "V": "0001403161", "BRK-B": "0001067983",
        }
        cik = cik_map.get(symbol.upper())
        if cik:
            return cik

        # 尝试通过 SEC ticker 映射查询
        try:
            resp = self._client.get(
                f"https://www.sec.gov/files/company_tickers.json",
                timeout=15,
            )
            data = resp.json()
            for entry in data.values():
                if entry.get("ticker", "").upper() == symbol.upper():
                    cik = str(entry["cik_str"]).zfill(10)
                    return cik
        except Exception:
            pass
        return ""
