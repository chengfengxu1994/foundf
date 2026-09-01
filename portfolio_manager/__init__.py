"""
portfolio_manager — 账户账务核验与投资记录引擎。

分层架构：
    broker_statement_raw          原始流水（只读不修改）
    broker_transaction_normalized 规范化事件
    portfolio_ledger              逐笔记账结果

核心原则：
    1. 原始数据不可修改
    2. 现金余额逐笔闭合
    3. 持仓从流水重建（不依赖截图）
    4. 成本法可配置（加权平均 / FIFO）
    5. 费用完整计入成本和收益
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cost_basis import FIFOCost, WeightedAverageCost
    from .event_classifier import EventClassifier, EventType
    from .fee_model import FeeModel, MarketFeeConfig
    from .importer import StatementImporter
    from .ledger import CashLedger, PortfolioLedger, PositionLedger
    from .reconciliation import ReconciliationEngine


_LAZY_IMPORTS = {
    "StatementImporter": (".importer", "StatementImporter"),
    "EventClassifier": (".event_classifier", "EventClassifier"),
    "EventType": (".event_classifier", "EventType"),
    "CashLedger": (".ledger", "CashLedger"),
    "PositionLedger": (".ledger", "PositionLedger"),
    "PortfolioLedger": (".ledger", "PortfolioLedger"),
    "WeightedAverageCost": (".cost_basis", "WeightedAverageCost"),
    "FIFOCost": (".cost_basis", "FIFOCost"),
    "FeeModel": (".fee_model", "FeeModel"),
    "MarketFeeConfig": (".fee_model", "MarketFeeConfig"),
    "ReconciliationEngine": (".reconciliation", "ReconciliationEngine"),
}


def __getattr__(name: str):
    """Load optional database-backed components only when requested."""
    try:
        module_name, attribute = _LAZY_IMPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    "StatementImporter", "EventClassifier", "EventType",
    "CashLedger", "PositionLedger", "PortfolioLedger",
    "WeightedAverageCost", "FIFOCost", "FeeModel", "MarketFeeConfig",
    "ReconciliationEngine",
]
