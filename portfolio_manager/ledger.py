"""
ledger.py — 现金账本 + 持仓账本（复式记账）。

逐笔回溯：
    - 现金余额：expected_balance = prev_balance + cash_delta
    - 持仓数量：position_after = position_before + position_delta
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from foundf_db import Warehouse
from .cost_basis import WeightedAverageCost, FIFOCost


@dataclass
class CashRow:
    """现金账本的一行。"""
    seq: int
    trade_date: str
    event_type: str
    cash_delta: float
    broker_balance: float | None
    expected_balance: float
    balance_error: float
    passed: bool


@dataclass
class PositionRow:
    """持仓账本的一行。"""
    seq: int
    trade_date: str
    symbol: str
    event_type: str
    qty_change: float
    qty_after: float
    price: float
    cost_basis_change: float
    cost_basis_after: float
    avg_cost: float
    realized_pnl: float


class CashLedger:
    """现金账本 — 逐笔现金余额回溯。

    使用方式:
        ledger = CashLedger(warehouse)
        result = ledger.reconcile(transactions)
    """

    def __init__(self, warehouse: Warehouse | None = None,
                 start_balance: float = 0.0):
        self.warehouse = warehouse
        self.start_balance = start_balance

    def reconcile(self, txns: list[dict[str, Any]]) -> dict[str, Any]:
        """执行现金余额回溯。"""
        rows: list[CashRow] = []
        balance = self.start_balance
        max_error = 0.0
        failed = 0
        total = 0

        for i, t in enumerate(txns):
            total += 1
            cash_delta = t.get("cash_delta", 0) or 0
            broker_balance = t.get("broker_cash_balance")

            expected = round(balance + cash_delta, 2)
            error = 0.0
            passed = True

            if broker_balance is not None:
                error = round(broker_balance - expected, 2)
                passed = abs(error) <= 0.01
                if not passed:
                    failed += 1
                    max_error = max(max_error, abs(error))

            rows.append(CashRow(
                seq=i + 1,
                trade_date=str(t["trade_date"])[:10],
                event_type=t.get("event_type", "?"),
                cash_delta=cash_delta,
                broker_balance=broker_balance,
                expected_balance=expected,
                balance_error=error,
                passed=passed,
            ))
            balance = expected

        # 保存到数据库
        if self.warehouse:
            for row in rows:
                self.warehouse.execute(
                    "INSERT OR REPLACE INTO portfolio_ledger "
                    "(entry_id, trade_date, transaction_id, event_type, "
                    "cash_change, cash_balance) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [row.seq, row.trade_date, row.seq, row.event_type,
                     row.cash_delta, row.expected_balance],
                )

        pass_rate = (total - failed) / total if total > 0 else 1.0
        return {
            "total_rows": total,
            "failed_rows": failed,
            "pass_rate": round(pass_rate, 4),
            "max_error": round(max_error, 2),
            "opening_balance": round(self.start_balance, 2),
            "ending_balance": round(balance, 2),
            "failed_details": [
                {"seq": r.seq, "date": r.trade_date,
                 "expected": r.expected_balance,
                 "broker": r.broker_balance, "error": r.balance_error}
                for r in rows if not r.passed
            ],
        }


class PositionLedger:
    """持仓账本 — 逐笔持仓回溯。

    支持双成本法：
        - WEIGHTED_AVERAGE: 加权平均成本
        - FIFO: 先进先出
    """

    def __init__(self, warehouse: Warehouse | None = None,
                 cost_method: str = "WEIGHTED_AVERAGE"):
        self.warehouse = warehouse
        self.cost_method = cost_method
        self._positions: dict[str, Any] = {}  # symbol -> cost engine

    def process(self, txns: list[dict[str, Any]]) -> dict[str, Any]:
        """逐笔处理交易，重建持仓。"""
        # 重置
        self._positions = {}
        all_rows: list[PositionRow] = []
        hard_errors: list[dict] = []

        for i, t in enumerate(txns):
            event_type = t.get("event_type", "")
            symbol = t.get("symbol", "")
            qty = t.get("quantity", 0) or 0
            price = t.get("price", 0) or 0
            pos_delta = t.get("position_delta", 0) or 0

            # 非证券事件跳过
            if event_type not in ("BUY", "SELL"):
                continue
            if not symbol:
                continue

            # 初始化成本引擎
            if symbol not in self._positions:
                if self.cost_method == "FIFO":
                    self._positions[symbol] = FIFOCost()
                else:
                    self._positions[symbol] = WeightedAverageCost()

            engine = self._positions[symbol]

            # 处理交易
            if event_type == "BUY":
                fee = (t.get("commission", 0) or 0) + (t.get("transfer_fee", 0) or 0)
                row = engine.buy(qty, price, fee)
            elif event_type == "SELL":
                fee = (t.get("commission", 0) or 0) + (t.get("stamp_duty", 0) or 0) + \
                      (t.get("transfer_fee", 0) or 0)
                row = engine.sell(qty, price, fee)
            else:
                continue

            # 检查负持仓
            if row and row.get("qty_after", 0) < -0.001:
                hard_errors.append({
                    "seq": i + 1,
                    "symbol": symbol,
                    "date": str(t["trade_date"])[:10],
                    "qty_after": row["qty_after"],
                    "message": "负持仓",
                })

            if row:
                all_rows.append(PositionRow(
                    seq=i + 1,
                    trade_date=str(t["trade_date"])[:10],
                    symbol=symbol,
                    event_type=event_type,
                    qty_change=pos_delta,
                    qty_after=row.get("qty_after", 0),
                    price=price,
                    cost_basis_change=row.get("cost_basis_change", 0),
                    cost_basis_after=row.get("cost_basis_after", 0),
                    avg_cost=row.get("avg_cost", 0),
                    realized_pnl=row.get("realized_pnl", 0),
                ))

        # 最终持仓
        final_positions = {}
        for sym, engine in self._positions.items():
            state = engine.get_state()
            if state.get("shares", 0) > 0:
                final_positions[sym] = state

        return {
            "cost_method": self.cost_method,
            "total_entries": len(all_rows),
            "hard_errors": hard_errors,
            "final_positions": final_positions,
        }


class PortfolioLedger:
    """组合账本 — 整合现金 + 持仓。"""

    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse
        self.cash_ledger = CashLedger(warehouse)
        self.position_ledger_wavg = PositionLedger(warehouse, "WEIGHTED_AVERAGE")
        self.position_ledger_fifo = PositionLedger(warehouse, "FIFO")

    def run(self, txns: list[dict[str, Any]],
            start_balance: float = 0.0) -> dict[str, Any]:
        """运行完整账本。"""
        cash_result = self.cash_ledger.reconcile(txns)
        wavg_result = self.position_ledger_wavg.process(txns)
        fifo_result = self.position_ledger_fifo.process(txns)

        return {
            "cash": cash_result,
            "positions_weighted_average": wavg_result,
            "positions_fifo": fifo_result,
        }
