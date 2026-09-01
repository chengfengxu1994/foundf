"""
港交所披露易数据提供者。

数据来源：港交所公开数据
- 港股通名单查询
- 股票基本信息
- 公告搜索

不要依赖：新浪/腾讯接口。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from ..base import (
    DailyPrice,
    DataProvider,
    FinancialData,
    NewsEvent,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)

HKEX_BASE = "https://www1.hkexnews.hk"
SEARCH_URL = f"{HKEX_BASE}/search/titleSearchServlet.do"
ACTIVE_STOCK_URL = f"{HKEX_BASE}/ncms/script/eds/activestock_sehk_c.json"


# 仅供观察列表配置使用；不得在官方请求失败时充当证券主数据。
HK_CONNECT_DEFAULT = [
    ("0005.HK", "汇丰控股"), ("0388.HK", "香港交易所"),
    ("0700.HK", "腾讯控股"), ("0883.HK", "中国海洋石油"),
    ("0941.HK", "中国移动"), ("1398.HK", "工商银行"),
    ("1810.HK", "小米集团-W"), ("2269.HK", "药明生物"),
    ("2318.HK", "中国平安"), ("9988.HK", "阿里巴巴-W"),
]


class HKEXProvider(DataProvider):
    """港交所数据提供者。"""

    def __init__(self, cache_dir: str | Path = "data/raw/hk_stock/hkex"):
        super().__init__("hkex")
        self._client = httpx.Client(timeout=20)
        self._stock_cache: dict[str, str] = {}
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_stock_basic(self) -> list[StockBasic]:
        results = []
        for code, name in self._fetch_active_stocks():
            results.append(
                StockBasic(
                    code=code,
                    name=name,
                    market="HK_CONNECT",
                    currency="HKD",
                )
            )
        return results

    def get_security_basics(self, symbols: list[str]) -> list[StockBasic]:
        """Return only officially listed rows for explicitly tracked symbols."""

        wanted = set(symbols)
        return [
            StockBasic(
                code=code,
                name=name,
                market="HK_CONNECT",
                currency="HKD",
            )
            for code, name in self._fetch_active_stocks()
            if code in wanted
        ]

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[TradeCalendar]:
        return []

    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list[DailyPrice]:
        # 港股日线通过 yfinance 获取更可靠
        return []

    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None,
    ) -> list[FinancialData]:
        return []

    def get_news(
        self, symbol: str = "", start_date: str = "", end_date: str = "",
    ) -> list[NewsEvent]:
        """获取港交所披露易公告。"""
        stock_code = symbol.replace(".HK", "").lstrip("0") if symbol else ""
        params: dict[str, Any] = {
            "lang": "EN",
            "category": "0",
            "market": "SEHK",
            "documentType": "-1",
            "from": (start_date or (datetime.now(timezone.utc).strftime("%Y%m%d"))),
            "to": (end_date or (datetime.now(timezone.utc).strftime("%Y%m%d"))),
            "sortDir": "desc",
            "sortByOptions": "DateTime",
            "titleSearchClause": "",
        }
        if stock_code:
            params["stockId"] = self._resolve_stock_id(stock_code)

        try:
            resp = self._client.post(SEARCH_URL, data=params, timeout=20)
            data = resp.json()
            raw_items = json.loads(data.get("result", "[]")) if data.get("result") else []
        except Exception:
            return []

        results: list[NewsEvent] = []
        for item in raw_items[:50]:
            title = item.get("TITLE", "")
            if not title:
                continue
            results.append(NewsEvent(
                published_at=item.get("DATE_TIME", ""),
                title=title,
                content=item.get("SHORT_TEXT", "") or item.get("LONG_TEXT", "") or "",
                symbol=symbol,
                source="港交所披露易",
                source_url=HKEX_BASE + (item.get("FILE_LINK") or ""),
            ))
        return results

    def health_check(self) -> ProviderHealth:
        try:
            resp = self._client.get(HKEX_BASE, timeout=10)
            ok = resp.status_code == 200
            return ProviderHealth(
                provider_name=self.name,
                status="healthy" if ok else "degraded",
                api_available=ok,
                message="HKEX 可访问" if ok else f"HTTP {resp.status_code}",
            )
        except Exception as e:
            return ProviderHealth(
                provider_name=self.name,
                status="error",
                api_available=False,
                message=str(e),
            )

    def _fetch_active_stocks(self) -> list[tuple[str, str]]:
        """获取港交所活跃股票列表。"""
        try:
            resp = self._client.get(ACTIVE_STOCK_URL, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("HKEX active-stock response is not a list")
            now = datetime.now(timezone.utc)
            archive_dir = self.cache_dir / now.strftime("%Y/%m/%d")
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / (
                f"{now.strftime('%H%M%S.%fZ')}_active_stocks_{uuid4().hex}.json"
            )
            with archive_path.open("x", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": "foundf.raw.hkex_active_stocks.v1",
                        "source_url": ACTIVE_STOCK_URL,
                        "fetched_at": now.isoformat(),
                        "rows": data,
                    },
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            rows = []
            for item in data:
                raw_code = str(item.get("c") or "").strip()
                name = str(item.get("n") or "").strip()
                if raw_code.isdigit() and name:
                    code = str(int(raw_code)).zfill(4)
                    rows.append((f"{code}.HK", name))
            if rows:
                return rows
        except Exception:
            pass
        return []

    def _resolve_stock_id(self, code: str) -> str:
        """解析股票代码到 HKEX stock_id。"""
        if code in self._stock_cache:
            return self._stock_cache[code]
        try:
            resp = self._client.get(
                f"{HKEX_BASE}/ncms/script/eds/activestock_sehk_c.json",
                timeout=10,
            )
            data = resp.json()
            for item in data.get("data", []):
                sc = str(item.get("stock_code", ""))
                sid = str(item.get("stock_id", ""))
                if sc == code.lstrip("0"):
                    self._stock_cache[code] = sid
                    return sid
        except Exception:
            pass
        return code
