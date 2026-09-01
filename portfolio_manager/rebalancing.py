"""只读、成本约束的组合再平衡模拟器。

算法优先使用显式新增现金补低配资产；只有新增现金不足且用户允许模拟卖出时，
才从超配资产中选择预计成本率较低的持仓。输出是模拟方案，不会写账户或连接券商。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


ASSET_CLASSES = ("LIQUIDITY", "FIXED_INCOME", "GOLD", "EQUITY")


@dataclass(frozen=True)
class CostModel:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    sell_tax_rate: float = 0.001
    impact_rate: float = 0.0005

    def estimate(self, amount: float, action: str) -> dict[str, float]:
        amount = max(0.0, float(amount))
        commission = (
            max(self.min_commission, amount * self.commission_rate)
            if amount > 0
            else 0.0
        )
        tax = amount * self.sell_tax_rate if action.upper() == "SELL" else 0.0
        impact = amount * self.impact_rate
        return {
            "commission": round(commission, 2),
            "tax": round(tax, 2),
            "impact": round(impact, 2),
            "total": round(commission + tax + impact, 2),
        }


def _values(
    holdings: Sequence[Mapping[str, Any]], cash: float
) -> tuple[dict[str, float], float]:
    result = {key: 0.0 for key in ASSET_CLASSES}
    result["LIQUIDITY"] = max(0.0, float(cash))
    for holding in holdings:
        asset_class = str(holding.get("asset_class", "UNKNOWN")).upper()
        if asset_class in result:
            result[asset_class] += max(0.0, float(holding.get("market_value", 0)))
    return result, sum(result.values())


def _allocation_rows(
    values: Mapping[str, float], total: float, targets: Mapping[str, float]
) -> list[dict[str, Any]]:
    rows = []
    for asset_class in ASSET_CLASSES:
        current = values.get(asset_class, 0.0)
        target = float(targets[asset_class])
        target_value = total * target
        rows.append(
            {
                "asset_class": asset_class,
                "value": round(current, 2),
                "weight": round(current / total, 6) if total else 0.0,
                "target_weight": target,
                "gap": round(target_value - current, 2),
                "status": (
                    "UNDER"
                    if current < target_value - 0.01
                    else "OVER"
                    if current > target_value + 0.01
                    else "ON_TARGET"
                ),
            }
        )
    return rows


def simulate_trades(
    holdings: Sequence[Mapping[str, Any]],
    cash: float,
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """在内存中模拟成交，不修改输入。"""

    simulated = deepcopy(list(holdings))
    index = {str(row.get("symbol")): row for row in simulated}
    simulated_cash = float(cash)
    for trade in trades:
        amount = max(0.0, float(trade.get("amount", 0)))
        cost = float(trade.get("expected_cost", {}).get("total", 0) or 0)
        action = str(trade.get("action", "")).upper()
        symbol = str(trade.get("symbol", ""))
        if action == "BUY":
            row = index.get(symbol)
            if row is None:
                row = {
                    "symbol": symbol,
                    "name": str(trade.get("name", symbol)),
                    "asset_class": str(trade.get("asset_class", "UNKNOWN")),
                    "market_value": 0.0,
                }
                simulated.append(row)
                index[symbol] = row
            row["market_value"] = round(
                float(row.get("market_value", 0)) + amount, 2
            )
            simulated_cash -= amount + cost
        elif action == "SELL" and symbol in index:
            index[symbol]["market_value"] = round(
                max(0.0, float(index[symbol].get("market_value", 0)) - amount),
                2,
            )
            simulated_cash += amount - cost
    return {"holdings": simulated, "cash": round(simulated_cash, 2)}


class RebalancingEngine:
    def __init__(self, targets: Mapping[str, float], cost_model: CostModel | None = None):
        self.targets = {str(key).upper(): float(value) for key, value in targets.items()}
        if set(self.targets) != set(ASSET_CLASSES):
            raise ValueError("targets must contain all economic asset classes")
        if abs(sum(self.targets.values()) - 1.0) > 1e-6:
            raise ValueError("targets must sum to 1")
        self.cost_model = cost_model or CostModel()

    def plan(
        self,
        holdings: Sequence[Mapping[str, Any]],
        cash: float,
        *,
        new_cash: float = 0.0,
        buy_candidates: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        allow_sell_simulation: bool = False,
        data_ready: bool = False,
        ips_confirmed: bool = False,
    ) -> dict[str, Any]:
        values, current_total = _values(holdings, cash)
        before = _allocation_rows(values, current_total, self.targets)
        blockers = []
        if not data_ready:
            blockers.append("VALUATION_DATA_NOT_READY")
        if not ips_confirmed:
            blockers.append("IPS_NOT_CONFIRMED")
        if current_total <= 0:
            blockers.append("NO_VALUED_ASSETS")
        if blockers:
            return {
                "status": "DATA_REVIEW",
                "blockers": blockers,
                "before": before,
                "trades": [],
                "after": before,
                "total_expected_cost": 0.0,
                "simulation_only": True,
            }

        candidates = buy_candidates or {}
        trades: list[dict[str, Any]] = []
        deployable = max(0.0, float(new_cash))
        projected_total = current_total + deployable
        projected = dict(values)
        projected["LIQUIDITY"] += deployable
        gaps = {
            key: projected_total * self.targets[key] - projected[key]
            for key in ASSET_CLASSES
        }

        # 新增现金只补低配，现金本身低配时先保留为流动性。
        liquidity_reserve = max(0.0, gaps["LIQUIDITY"])
        deployable = max(0.0, deployable - liquidity_reserve)
        for asset_class in sorted(
            (key for key in ASSET_CLASSES if key != "LIQUIDITY"),
            key=lambda key: gaps[key],
            reverse=True,
        ):
            gap = max(0.0, gaps[asset_class])
            amount = min(deployable, gap)
            available = list(candidates.get(asset_class, []))
            if amount <= 0 or not available:
                continue
            candidate = min(
                available,
                key=lambda row: float(row.get("estimated_cost_rate", 1.0)),
            )
            expected_cost = self.cost_model.estimate(amount, "BUY")
            trades.append(
                self._trade(
                    "BUY", candidate, asset_class, amount, expected_cost,
                    rule_id=f"ALLOCATION.{asset_class}.UNDERWEIGHT",
                )
            )
            projected[asset_class] += amount
            projected["LIQUIDITY"] -= amount + expected_cost["total"]
            deployable -= amount + expected_cost["total"]

        if allow_sell_simulation:
            excess_by_class = {
                key: max(
                    0.0,
                    projected[key] - projected_total * self.targets[key],
                )
                for key in ASSET_CLASSES
            }
            sellable = sorted(
                (
                    row
                    for row in holdings
                    if row.get("sellable", True)
                    and excess_by_class.get(
                        str(row.get("asset_class", "")).upper(), 0
                    )
                    > 0
                ),
                key=lambda row: float(row.get("estimated_cost_rate", 0)),
            )
            for row in sellable:
                asset_class = str(row.get("asset_class", "")).upper()
                amount = min(
                    float(row.get("market_value", 0)),
                    excess_by_class[asset_class],
                )
                if amount <= 0:
                    continue
                expected_cost = self.cost_model.estimate(amount, "SELL")
                trades.append(
                    self._trade(
                        "SELL", row, asset_class, amount, expected_cost,
                        rule_id=f"ALLOCATION.{asset_class}.OVERWEIGHT",
                    )
                )
                excess_by_class[asset_class] -= amount

        simulated = simulate_trades(holdings, cash + float(new_cash), trades)
        after_values, after_total = _values(
            simulated["holdings"], simulated["cash"]
        )
        after = _allocation_rows(after_values, after_total, self.targets)
        return {
            "status": "SIMULATION_READY",
            "blockers": [],
            "before": before,
            "trades": trades,
            "after": after,
            "violations_before": [
                row["asset_class"] for row in before if row["status"] != "ON_TARGET"
            ],
            "violations_after": [
                row["asset_class"] for row in after if row["status"] != "ON_TARGET"
            ],
            "total_expected_cost": round(
                sum(item["expected_cost"]["total"] for item in trades), 2
            ),
            "simulation_only": True,
            "requires_user_confirmation": True,
            "disclaimer": "仅供决策支持的模拟结果，不构成自动交易指令。",
        }

    @staticmethod
    def _trade(
        action: str,
        instrument: Mapping[str, Any],
        asset_class: str,
        amount: float,
        expected_cost: Mapping[str, float],
        *,
        rule_id: str,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "symbol": str(instrument.get("symbol", "")),
            "name": str(instrument.get("name", "")),
            "asset_class": asset_class,
            "amount": round(amount, 2),
            "amount_range": [
                round(amount * 0.95, 2),
                round(amount * 1.05, 2),
            ],
            "expected_cost": dict(expected_cost),
            "rules_corrected": [rule_id],
            "consequence_if_skipped": "配置偏离和对应风险预算占用将继续保留。",
        }


def staged_reduction_plan(
    *,
    symbol: str,
    current_value: float,
    portfolio_total: float,
    limit_weight: float,
    batches: int = 3,
    cadence_days: int = 10,
) -> dict[str, Any]:
    """生成等额分批降险计划；结果仍需人工确认。"""

    if batches < 1 or cadence_days < 1:
        raise ValueError("batches and cadence_days must be positive")
    target_value = max(0.0, float(portfolio_total) * float(limit_weight))
    reduction = max(0.0, float(current_value) - target_value)
    per_batch = reduction / batches if batches else 0.0
    return {
        "symbol": symbol,
        "current_value": round(float(current_value), 2),
        "target_value": round(target_value, 2),
        "total_reduction": round(reduction, 2),
        "batches": [
            {
                "batch": index,
                "amount": round(per_batch, 2),
                "earliest_day_offset": (index - 1) * cadence_days,
                "requires_confirmation": True,
            }
            for index in range(1, batches + 1)
        ],
        "simulation_only": True,
    }

