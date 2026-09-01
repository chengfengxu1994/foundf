"""Strategic asset allocation and rebalancing guardrails.

This module separates two concepts that are often incorrectly mixed:

* instrument type: deposit, bond, gold, stock, fund, ETF
* economic exposure: liquidity, fixed income, gold, equity, unknown

An ETF is a wrapper, not an economic asset class.  A gold ETF therefore belongs
to the GOLD exposure bucket while its instrument type remains ETF.  Unknown
fund/ETF exposures are never silently treated as equities.

The engine is deliberately advisory.  It blocks rebalancing instructions when
valuation coverage is poor, exposure classification is incomplete, or the
investor profile has not been confirmed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ECONOMIC_ASSET_CLASSES = ("LIQUIDITY", "FIXED_INCOME", "GOLD", "EQUITY")
INSTRUMENT_TYPES = ("DEPOSIT", "BOND", "GOLD", "STOCK", "FUND", "ETF")

_LIQUIDITY_WORDS = ("现金", "存款", "货币", "cash", "deposit", "money market")
_BOND_WORDS = ("债券", "国债", "纯债", "短债", "中债", "可转债", "bond", "treasury")
_GOLD_WORDS = ("黄金", "gold")
_FUND_WORDS = ("基金", "fund", "lof")
_ETF_WORDS = ("etf",)


def _normalise_weight(value: Any) -> float:
    weight = float(value)
    return weight / 100.0 if weight > 1.0 else weight


@dataclass(frozen=True)
class AllocationPolicy:
    """Validated strategic allocation policy."""

    policy_id: str
    policy_version: str
    profile_confirmed: bool
    targets: dict[str, float]
    bands: dict[str, tuple[float, float]]
    min_valuation_coverage: float = 0.95
    max_unknown_weight: float = 0.05
    min_liquidity_amount: float = 0.0
    min_liquidity_pct: float = 0.10
    cashflow_first: bool = True
    symbol_overrides: dict[str, dict[str, str]] | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AllocationPolicy":
        targets = {
            str(key).upper(): _normalise_weight(value)
            for key, value in raw.get("targets", {}).items()
        }
        missing = set(ECONOMIC_ASSET_CLASSES) - set(targets)
        extra = set(targets) - set(ECONOMIC_ASSET_CLASSES)
        if missing or extra:
            raise ValueError(
                f"targets must contain exactly {ECONOMIC_ASSET_CLASSES}; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        if abs(sum(targets.values()) - 1.0) > 1e-6:
            raise ValueError("target weights must sum to 1.0")

        raw_bands = raw.get("bands", {})
        bands: dict[str, tuple[float, float]] = {}
        for asset_class, target in targets.items():
            values = raw_bands.get(asset_class, raw_bands.get(asset_class.lower()))
            if values is None:
                lower, upper = max(0.0, target - 0.05), min(1.0, target + 0.05)
            else:
                lower, upper = (_normalise_weight(v) for v in values)
            if not 0 <= lower <= target <= upper <= 1:
                raise ValueError(
                    f"invalid band for {asset_class}: {lower}, {target}, {upper}"
                )
            bands[asset_class] = (lower, upper)

        return cls(
            policy_id=str(raw.get("policy_id", "balanced_reference")),
            policy_version=str(raw.get("policy_version", "1")),
            profile_confirmed=bool(raw.get("profile_confirmed", False)),
            targets=targets,
            bands=bands,
            min_valuation_coverage=_normalise_weight(
                raw.get("min_valuation_coverage", 0.95)
            ),
            max_unknown_weight=_normalise_weight(raw.get("max_unknown_weight", 0.05)),
            min_liquidity_amount=max(0.0, float(raw.get("min_liquidity_amount", 0))),
            min_liquidity_pct=_normalise_weight(raw.get("min_liquidity_pct", 0.10)),
            cashflow_first=bool(raw.get("cashflow_first", True)),
            symbol_overrides={
                str(symbol).upper(): {
                    str(k): str(v).upper() for k, v in values.items()
                }
                for symbol, values in raw.get("symbol_overrides", {}).items()
            },
        )

    @classmethod
    def load(cls, path: str | Path = "config/asset_allocation.json") -> "AllocationPolicy":
        policy_path = Path(path)
        return cls.from_mapping(json.loads(policy_path.read_text(encoding="utf-8")))


def classify_holding(
    holding: Mapping[str, Any],
    policy: AllocationPolicy,
) -> dict[str, str]:
    """Classify a holding without inventing unknown fund/ETF exposure."""

    symbol = str(holding.get("symbol", "")).upper()
    name = str(holding.get("name", ""))
    text = f"{symbol} {name}".lower()
    override = (policy.symbol_overrides or {}).get(symbol, {})

    explicit_instrument = holding.get("instrument_type") or override.get("instrument_type")
    explicit_exposure = (
        holding.get("asset_class")
        or holding.get("economic_asset_class")
        or override.get("economic_asset_class")
        or override.get("asset_class")
    )

    if explicit_instrument:
        instrument = str(explicit_instrument).upper()
    elif any(word in text for word in _ETF_WORDS):
        instrument = "ETF"
    elif any(word in text for word in _LIQUIDITY_WORDS):
        instrument = "DEPOSIT"
    elif any(word in text for word in _BOND_WORDS):
        instrument = "BOND"
    elif any(word in text for word in _GOLD_WORDS):
        instrument = "GOLD"
    elif any(word in text for word in _FUND_WORDS):
        instrument = "FUND"
    else:
        instrument = "STOCK"

    if explicit_exposure:
        exposure = str(explicit_exposure).upper()
    elif any(word in text for word in _LIQUIDITY_WORDS):
        exposure = "LIQUIDITY"
    elif any(word in text for word in _BOND_WORDS):
        exposure = "FIXED_INCOME"
    elif any(word in text for word in _GOLD_WORDS):
        exposure = "GOLD"
    elif instrument == "STOCK":
        exposure = "EQUITY"
    else:
        # A generic fund or ETF can contain stocks, bonds, gold, or a mixture.
        exposure = "UNKNOWN"

    if instrument not in INSTRUMENT_TYPES:
        instrument = "FUND"
    if exposure not in (*ECONOMIC_ASSET_CLASSES, "UNKNOWN"):
        exposure = "UNKNOWN"
    return {"instrument_type": instrument, "economic_asset_class": exposure}


class AssetAllocationEngine:
    """Assess allocation drift and decide whether advice is safe to emit."""

    def __init__(self, policy: AllocationPolicy):
        self.policy = policy

    def assess(
        self,
        holdings: Sequence[Mapping[str, Any]],
        cash: float,
    ) -> dict[str, Any]:
        cash = max(0.0, float(cash or 0))
        exposure_values = {key: 0.0 for key in (*ECONOMIC_ASSET_CLASSES, "UNKNOWN")}
        instrument_values = {key: 0.0 for key in INSTRUMENT_TYPES}
        exposure_values["LIQUIDITY"] = cash
        instrument_values["DEPOSIT"] = cash

        classified: list[dict[str, Any]] = []
        unpriced: list[dict[str, Any]] = []
        priced_value = 0.0
        unpriced_cost = 0.0

        for holding in holdings:
            classification = classify_holding(holding, self.policy)
            value = max(0.0, float(holding.get("market_value", 0) or 0))
            cost = max(0.0, float(holding.get("cost", holding.get("total_cost", 0)) or 0))
            is_priced = value > 0
            item = {
                "symbol": str(holding.get("symbol", "")),
                "name": str(holding.get("name", "")),
                **classification,
                "market_value": round(value, 2),
                "priced": is_priced,
            }
            classified.append(item)
            if is_priced:
                priced_value += value
                exposure_values[classification["economic_asset_class"]] += value
                instrument_values[classification["instrument_type"]] += value
            else:
                unpriced_cost += cost
                unpriced.append(
                    {
                        "symbol": item["symbol"],
                        "name": item["name"],
                        "cost_reference": round(cost, 2),
                    }
                )

        known_total = cash + priced_value
        coverage_denominator = known_total + unpriced_cost
        coverage = known_total / coverage_denominator if coverage_denominator > 0 else 0.0
        unknown_weight = (
            exposure_values["UNKNOWN"] / known_total if known_total > 0 else 0.0
        )

        blockers: list[str] = []
        if not self.policy.profile_confirmed:
            blockers.append("INVESTOR_PROFILE_UNCONFIRMED")
        if coverage < self.policy.min_valuation_coverage:
            blockers.append("VALUATION_COVERAGE_LOW")
        if unknown_weight > self.policy.max_unknown_weight:
            blockers.append("UNKNOWN_EXPOSURE_HIGH")
        if known_total <= 0:
            blockers.append("NO_VALUED_ASSETS")

        actionable = not blockers
        allocations: list[dict[str, Any]] = []
        for asset_class in ECONOMIC_ASSET_CLASSES:
            value = exposure_values[asset_class]
            current = value / known_total if known_total > 0 else 0.0
            target = self.policy.targets[asset_class]
            lower, upper = self.policy.bands[asset_class]
            status = "OVERWEIGHT" if current > upper else "UNDERWEIGHT" if current < lower else "IN_RANGE"
            amount = target * known_total - value
            if not actionable:
                action = "DATA_REVIEW"
            elif status == "OVERWEIGHT":
                action = "REDUCE"
            elif status == "UNDERWEIGHT":
                action = "ADD_WITH_NEW_CASH" if self.policy.cashflow_first else "ADD"
            else:
                action = "HOLD"
            allocations.append(
                {
                    "asset_class": asset_class,
                    "value": round(value, 2),
                    "current_weight": round(current, 6),
                    "target_weight": round(target, 6),
                    "lower_bound": round(lower, 6),
                    "upper_bound": round(upper, 6),
                    "drift": round(current - target, 6),
                    "status": status,
                    "amount_to_target": round(amount, 2),
                    "action": action,
                }
            )

        liquidity_floor = max(
            self.policy.min_liquidity_amount,
            self.policy.min_liquidity_pct * known_total,
        )
        liquidity_shortfall = max(0.0, liquidity_floor - cash)
        warnings = []
        if liquidity_shortfall > 0:
            warnings.append(
                f"流动性安全垫不足：缺口 {liquidity_shortfall:,.2f}"
            )
        if unpriced:
            warnings.append(f"{len(unpriced)} 个持仓缺少有效价格")
        if unknown_weight > 0:
            warnings.append(
                f"未知底层风险暴露占已估值资产 {unknown_weight:.1%}"
            )

        instrument_allocation = []
        for instrument in INSTRUMENT_TYPES:
            value = instrument_values[instrument]
            instrument_allocation.append(
                {
                    "instrument_type": instrument,
                    "value": round(value, 2),
                    "weight": round(value / known_total, 6) if known_total > 0 else 0.0,
                }
            )

        return {
            "policy_id": self.policy.policy_id,
            "policy_version": self.policy.policy_version,
            "profile_confirmed": self.policy.profile_confirmed,
            "actionable": actionable,
            "blockers": blockers,
            "known_total_value": round(known_total, 2),
            "priced_holdings_value": round(priced_value, 2),
            "unpriced_cost_reference": round(unpriced_cost, 2),
            "valuation_coverage": round(coverage, 6),
            "unknown_exposure_weight": round(unknown_weight, 6),
            "liquidity_floor": round(liquidity_floor, 2),
            "liquidity_shortfall": round(liquidity_shortfall, 2),
            "economic_allocation": allocations,
            "instrument_allocation": instrument_allocation,
            "classified_holdings": classified,
            "unpriced_holdings": unpriced,
            "warnings": warnings,
            "disclaimer": (
                "配置结果用于风险管理和决策支持，不承诺收益；"
                "在数据或投资者画像未确认时不得据此交易。"
            ),
        }


def assess_portfolio_allocation(
    portfolio: Mapping[str, Any],
    policy_path: str | Path = "config/asset_allocation.json",
) -> dict[str, Any]:
    """Convenience entry point for the daily pipeline."""

    policy = AllocationPolicy.load(policy_path)
    return AssetAllocationEngine(policy).assess(
        portfolio.get("holdings", []),
        float(portfolio.get("cash", 0) or 0),
    )
