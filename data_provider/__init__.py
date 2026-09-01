"""
data_provider — 生产级数据供应链。

统一接口 + 5个 Provider 实现 + 验证器 + 调度器。
"""

from .base import (
    DailyPrice,
    DataProvider,
    FinancialData,
    NewsEvent,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)
from .validator import DataValidator, ValidationResult

__all__ = [
    "DataProvider", "StockBasic", "DailyPrice", "TradeCalendar",
    "FinancialData", "NewsEvent", "ProviderHealth",
    "DataValidator", "ValidationResult",
    "CollectorScheduler",
]


def __getattr__(name: str):
    """延迟加载调度器，避免只使用数据模型时强制导入全部网络依赖。"""

    if name == "CollectorScheduler":
        from .scheduler import CollectorScheduler

        return CollectorScheduler
    raise AttributeError(name)
