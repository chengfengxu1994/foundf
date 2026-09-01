"""
data_provider — 生产级数据供应链。

统一接口定义，所有数据源必须实现 DataProvider 抽象基类。

接口：
    get_stock_basic()   — 股票基础信息
    get_trade_calendar() — 交易日历
    get_daily_price()   — 日线行情
    get_financial_data() — 财务数据
    get_news()          — 新闻事件
    health_check()      — 健康检查

缓存策略：
    原始API返回数据保存到 data/raw/{provider_name}/ 目录下，
    永不修改，只追加。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class StockBasic:
    """股票基础信息。"""
    code: str
    name: str
    market: str           # 'A', 'HK', 'US'
    industry: str = ""
    list_date: str = ""
    status: str = "active"
    currency: str = "CNY"
    asset_type: str = "STOCK"


@dataclass
class DailyPrice:
    """日线行情。"""
    date: str             # YYYY-MM-DD
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0


@dataclass
class TradeCalendar:
    """交易日。"""
    date: str
    is_open: bool         # True=交易日, False=休市
    pretrade_date: str | None = None  # 前一交易日


@dataclass
class FinancialData:
    """财务数据（核心指标）。"""
    symbol: str
    report_date: str      # 财报截止日
    report_type: str      # 'annual', 'semi_annual', 'quarterly'
    filed_at: str = ""    # 实际披露日期
    revenue: float | None = None
    profit: float | None = None
    cashflow: float | None = None
    roe: float | None = None
    roic: float | None = None
    gross_margin: float | None = None


@dataclass
class NewsEvent:
    """新闻事件。"""
    published_at: str
    title: str
    content: str = ""
    symbol: str = ""
    source: str = ""
    source_url: str = ""
    sentiment: float | None = None  # -1~1


@dataclass
class ProviderHealth:
    """Provider 健康状态。"""
    provider_name: str
    status: str               # 'healthy', 'degraded', 'error'
    last_success: str = ""    # ISO timestamp
    error_count: int = 0
    data_latency_days: int = 0  # 数据滞后天数
    message: str = ""
    api_available: bool = True


class DataProvider(ABC):
    """数据提供者抽象基类。

    所有具体 Provider 必须实现此接口。
    每个 Provider 必须：
    1. token/密钥从环境变量读取，禁止硬编码
    2. 缓存到 data/raw/{provider_name}/
    3. 支持增量更新
    """

    def __init__(self, name: str):
        self.name = name
        self._error_count = 0
        self._last_success: str | None = None

    @abstractmethod
    def get_stock_basic(self) -> list[StockBasic]:
        """获取股票基础信息。"""
        ...

    @abstractmethod
    def get_trade_calendar(self, start_date: str, end_date: str) -> list[TradeCalendar]:
        """获取交易日历。"""
        ...

    @abstractmethod
    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list[DailyPrice]:
        """获取日线行情。"""
        ...

    @abstractmethod
    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None,
    ) -> list[FinancialData]:
        """获取财务数据。"""
        ...

    def get_news(
        self, symbol: str = "", start_date: str = "", end_date: str = "",
    ) -> list[NewsEvent]:
        """获取新闻事件（可选实现）。"""
        return []

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """健康检查。"""
        ...

    # ── 帮助方法 ──────────────────────────────────────

    def record_success(self) -> None:
        self._last_success = datetime.now(timezone.utc).isoformat()
        self._error_count = 0

    def record_error(self) -> None:
        self._error_count += 1

    @property
    def error_rate(self) -> float:
        return min(1.0, self._error_count / max(self._error_count + 1, 1))
