"""
cost_basis.py — 成本核算引擎（Decimal精度）。

支持双成本法：
    WEIGHTED_AVERAGE: 加权平均成本
    FIFO: 先进先出（逐批记录，Decimal精度）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# 精度配置
MONEY_PRECISION = Decimal("0.01")
QTY_PRECISION = Decimal("0.0001")
TOLERANCE = Decimal("0.01")


class InsufficientPositionError(Exception):
    pass


@dataclass
class CostLot:
    """FIFO 批次。"""
    qty: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")

    def consume(self, qty: Decimal) -> tuple[Decimal, Decimal]:
        """消耗指定数量，返回 (消耗数量, 消耗成本)。"""
        take = min(qty, self.qty)
        consumed_cost = (self.total_cost / self.qty * take) if self.qty > 0 else Decimal("0")
        consumed_cost = consumed_cost.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
        self.qty -= take
        self.total_cost -= consumed_cost
        # 清理浮点残差
        if self.qty < QTY_PRECISION:
            self.qty = Decimal("0")
            self.total_cost = Decimal("0")
        if abs(self.total_cost) < MONEY_PRECISION:
            self.total_cost = Decimal("0")
        return take, consumed_cost


class WeightedAverageCost:
    """加权平均成本法（Decimal精度）。"""

    def __init__(self):
        self.shares = Decimal("0")
        self.total_cost = Decimal("0")
        self.realized_pnl = Decimal("0")

    def buy(self, qty: float, price: float, fee: float = 0.0) -> dict[str, Any]:
        q = Decimal(str(qty)).quantize(QTY_PRECISION)
        p = Decimal(str(price)).quantize(MONEY_PRECISION)
        f = Decimal(str(fee)).quantize(MONEY_PRECISION)
        trade_value = (q * p).quantize(MONEY_PRECISION)
        new_cost = trade_value + f

        old_shares = self.shares
        old_cost = self.total_cost

        self.shares += q
        self.total_cost += new_cost
        avg = self.total_cost / self.shares if self.shares > 0 else Decimal("0")

        return {
            "qty_after": float(self.shares),
            "cost_basis_after": float(self.total_cost),
            "cost_basis_change": float(new_cost),
            "avg_cost": float(avg),
            "realized_pnl": 0.0,
        }

    def sell(self, qty: float, price: float, fee: float = 0.0) -> dict[str, Any]:
        q = Decimal(str(qty)).quantize(QTY_PRECISION)
        p = Decimal(str(price)).quantize(MONEY_PRECISION)
        f = Decimal(str(fee)).quantize(MONEY_PRECISION)

        if self.shares <= 0 or q <= 0:
            return {"qty_after": 0, "cost_basis_after": 0,
                    "cost_basis_change": 0, "avg_cost": 0, "realized_pnl": 0.0}

        sell_qty = min(q, self.shares)
        disposed = (self.total_cost / self.shares * sell_qty).quantize(MONEY_PRECISION)
        proceeds = (sell_qty * p - f).quantize(MONEY_PRECISION)
        realized = proceeds - disposed

        self.realized_pnl += realized
        self.shares -= sell_qty
        self.total_cost -= disposed

        if self.shares < QTY_PRECISION:
            self.shares = Decimal("0")
            self.total_cost = Decimal("0")

        avg = self.total_cost / self.shares if self.shares > 0 else Decimal("0")
        return {
            "qty_after": float(self.shares),
            "cost_basis_after": float(self.total_cost),
            "cost_basis_change": float(-disposed),
            "avg_cost": float(avg),
            "realized_pnl": float(realized),
        }

    def get_state(self) -> dict[str, Any]:
        return {
            "shares": float(self.shares),
            "avg_cost": float(self.total_cost / self.shares) if self.shares > 0 else 0,
            "total_cost": float(self.total_cost),
            "realized_pnl": float(self.realized_pnl),
        }


class FIFOCost:
    """FIFO 成本法（Decimal 精度，批次管理）。

    每个买入批次独立记录。
    卖出时从最早的批次开始扣减。
    """

    def __init__(self):
        self._lots: list[CostLot] = []
        self.realized_pnl = Decimal("0")
        self._total_qty = Decimal("0")
        self._total_cost = Decimal("0")

    def buy(self, qty: float, price: float, fee: float = 0.0) -> dict[str, Any]:
        q = Decimal(str(qty)).quantize(QTY_PRECISION)
        p = Decimal(str(price)).quantize(MONEY_PRECISION)
        f = Decimal(str(fee)).quantize(MONEY_PRECISION)
        trade_value = (q * p).quantize(MONEY_PRECISION)
        lot_cost = trade_value + f
        unit = lot_cost / q if q > 0 else Decimal("0")

        lot = CostLot(qty=q, unit_cost=unit, total_cost=lot_cost)
        self._lots.append(lot)
        self._total_qty += q
        self._total_cost += lot_cost

        self._assert_invariants()
        return {
            "qty_after": float(self._total_qty),
            "cost_basis_after": float(self._total_cost),
            "cost_basis_change": float(lot_cost),
            "avg_cost": float(self._total_cost / self._total_qty) if self._total_qty > 0 else 0,
            "realized_pnl": 0.0,
        }

    def sell(self, qty: float, price: float, fee: float = 0.0) -> dict[str, Any]:
        q = Decimal(str(qty)).quantize(QTY_PRECISION)
        p = Decimal(str(price)).quantize(MONEY_PRECISION)
        f = Decimal(str(fee)).quantize(MONEY_PRECISION)

        if self._total_qty <= 0 or q <= 0:
            return {"qty_after": 0, "cost_basis_after": 0,
                    "cost_basis_change": 0, "avg_cost": 0, "realized_pnl": 0.0}

        sell_qty = min(q, self._total_qty)
        remaining = sell_qty
        disposed_cost = Decimal("0")

        while remaining > QTY_PRECISION and self._lots:
            lot = self._lots[0]
            consumed, cost = lot.consume(remaining)
            disposed_cost += cost
            remaining -= consumed
            if lot.qty <= QTY_PRECISION:
                self._lots.pop(0)

        if remaining > QTY_PRECISION:
            raise InsufficientPositionError(
                f"需要卖出 {sell_qty} 但批次总可卖不足"
            )

        proceeds = (sell_qty * p - f).quantize(MONEY_PRECISION)
        realized = proceeds - disposed_cost

        self.realized_pnl += realized
        self._total_qty -= sell_qty
        self._total_cost -= disposed_cost

        if self._total_qty < QTY_PRECISION:
            self._total_qty = Decimal("0")
            self._total_cost = Decimal("0")
            self._lots = []

        self._assert_invariants()
        avg = self._total_cost / self._total_qty if self._total_qty > 0 else Decimal("0")
        return {
            "qty_after": float(self._total_qty),
            "cost_basis_after": float(self._total_cost),
            "cost_basis_change": float(-disposed_cost),
            "avg_cost": float(avg),
            "realized_pnl": float(realized),
        }

    def get_state(self) -> dict[str, Any]:
        lot_cost = sum(l.total_cost for l in self._lots)
        lot_qty = sum(l.qty for l in self._lots)
        return {
            "shares": float(self._total_qty),
            "avg_cost": float(self._total_cost / self._total_qty) if self._total_qty > 0 else 0,
            "total_cost": float(self._total_cost),
            "lots": len(self._lots),
            "realized_pnl": float(self.realized_pnl),
            "_invariants": {
                "sum_lot_qty": float(lot_qty),
                "sum_lot_cost": float(lot_cost),
                "total_qty": float(self._total_qty),
                "total_cost": float(self._total_cost),
            },
        }

    def _assert_invariants(self) -> None:
        """保留不变式断言。"""
        lot_qty = sum(l.qty for l in self._lots)
        lot_cost = sum(l.total_cost for l in self._lots)
        assert abs(lot_qty - self._total_qty) <= QTY_PRECISION, \
            f"批次数量合计({lot_qty}) != 总数量({self._total_qty})"
        assert abs(lot_cost - self._total_cost) <= TOLERANCE, \
            f"批次成本合计({lot_cost}) != 总成本({self._total_cost})"
        for lot in self._lots:
            assert lot.qty >= -QTY_PRECISION, f"批次负数量: {lot.qty}"
            assert lot.total_cost >= -TOLERANCE, f"批次负成本: {lot.total_cost}"

    def get_lot_trace(self) -> list[dict[str, Any]]:
        """返回批次追踪信息。"""
        return [
            {
                "qty": float(l.qty),
                "unit_cost": float(l.unit_cost),
                "total_cost": float(l.total_cost),
            }
            for l in self._lots
        ]
