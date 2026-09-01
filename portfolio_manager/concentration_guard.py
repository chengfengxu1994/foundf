"""Portfolio concentration and drawdown guardrails.

The concentration guard aggregates economic exposure across wrappers.  A
technology stock and a technology ETF therefore consume the same sector risk
budget.  The drawdown guard operates only on a cash-flow-adjusted NAV series;
raw account balances must never be used because deposits and withdrawals would
be mistaken for investment performance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


UNKNOWN_SECTOR = "UNKNOWN"

_TECH_WORDS = (
    "科技",
    "互联网",
    "软件",
    "芯片",
    "半导体",
    "人工智能",
    "纳斯达克",
    "恒生科",
    "港股科技",
    "tech",
    "technology",
    "software",
    "semiconductor",
    "nasdaq",
)


def _weight(value: Any) -> float:
    result = float(value or 0)
    return result / 100.0 if result > 1 else result


@dataclass(frozen=True)
class ConcentrationPolicy:
    """Validated portfolio risk limits."""

    policy_id: str
    policy_version: str
    max_single_position: float
    max_sector_weight: float
    max_unknown_sector_weight: float
    warning_ratio: float
    sector_overrides: dict[str, str]
    drawdown_tiers: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ConcentrationPolicy":
        max_single = _weight(raw.get("max_single_position", 0.10))
        max_sector = _weight(raw.get("max_sector_weight", 0.25))
        max_unknown = _weight(raw.get("max_unknown_sector_weight", 0.05))
        warning_ratio = _weight(raw.get("warning_ratio", 0.80))
        for label, value in (
            ("max_single_position", max_single),
            ("max_sector_weight", max_sector),
            ("max_unknown_sector_weight", max_unknown),
            ("warning_ratio", warning_ratio),
        ):
            if not 0 < value <= 1:
                raise ValueError(f"{label} must be in (0, 1]")
        if max_single > max_sector:
            raise ValueError("max_single_position cannot exceed max_sector_weight")

        tiers = []
        previous = 0.0
        for item in raw.get("drawdown_tiers", []):
            threshold = _weight(item["threshold"])
            multiplier = _weight(item["risk_budget_multiplier"])
            if not previous < threshold < 1:
                raise ValueError("drawdown thresholds must be strictly increasing")
            if not 0 < multiplier <= 1:
                raise ValueError("risk_budget_multiplier must be in (0, 1]")
            tiers.append(
                {
                    "threshold": threshold,
                    "level": str(item["level"]).upper(),
                    "risk_budget_multiplier": multiplier,
                    "allow_new_risk": bool(item.get("allow_new_risk", False)),
                }
            )
            previous = threshold

        return cls(
            policy_id=str(raw.get("policy_id", "portfolio_risk_limits")),
            policy_version=str(raw.get("policy_version", "1")),
            max_single_position=max_single,
            max_sector_weight=max_sector,
            max_unknown_sector_weight=max_unknown,
            warning_ratio=warning_ratio,
            sector_overrides={
                str(symbol).upper(): str(sector).upper()
                for symbol, sector in raw.get("sector_overrides", {}).items()
            },
            drawdown_tiers=tuple(tiers),
        )

    @classmethod
    def load(
        cls, path: str | Path = "config/portfolio_risk_limits.json"
    ) -> "ConcentrationPolicy":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


def classify_sector(
    holding: Mapping[str, Any],
    policy: ConcentrationPolicy,
) -> str:
    """Return a conservative sector classification."""

    symbol = str(holding.get("symbol", "")).upper()
    explicit = (
        holding.get("sector")
        or holding.get("industry")
        or policy.sector_overrides.get(symbol)
    )
    if explicit:
        return str(explicit).upper()

    text = f"{symbol} {holding.get('name', '')}".lower()
    if any(word in text for word in _TECH_WORDS):
        return "TECHNOLOGY"
    return UNKNOWN_SECTOR


class ConcentrationGuard:
    """Aggregate sector exposure and enforce pre-trade risk budgets."""

    def __init__(self, policy: ConcentrationPolicy):
        self.policy = policy

    def assess(
        self,
        holdings: Sequence[Mapping[str, Any]],
        cash: float = 0.0,
    ) -> dict[str, Any]:
        market_total = sum(
            max(0.0, float(h.get("market_value", 0) or 0)) for h in holdings
        )
        known_total = max(0.0, float(cash or 0)) + market_total
        use_market_values = market_total > 0 and known_total > 0
        provided_weight_total = sum(
            max(0.0, _weight(h.get("weight", 0))) for h in holdings
        )
        weight_source = (
            "MARKET_VALUE"
            if use_market_values
            else "PROVIDED_WEIGHT"
            if provided_weight_total > 0
            else "UNAVAILABLE"
        )

        positions = []
        direct_sector_weights: dict[str, float] = {}
        lookthrough_sector_weights: dict[str, float] = {}
        sector_contributions: dict[str, list[dict[str, Any]]] = {}
        lookthrough_value_weight = 0.0
        unweighted_positions = 0
        for holding in holdings:
            if use_market_values:
                weight = max(0.0, float(holding.get("market_value", 0) or 0)) / known_total
                if weight == 0 and float(
                    holding.get("cost", holding.get("total_cost", 0)) or 0
                ) > 0:
                    unweighted_positions += 1
            else:
                weight = max(0.0, _weight(holding.get("weight", 0)))
                if weight == 0:
                    unweighted_positions += 1
            sector = classify_sector(holding, self.policy)
            direct_sector_weights[sector] = (
                direct_sector_weights.get(sector, 0.0) + weight
            )
            underlying = holding.get("underlying_holdings")
            parsed_underlying: list[tuple[Mapping[str, Any], float, str]] = []
            if (
                isinstance(underlying, Sequence)
                and not isinstance(underlying, (str, bytes))
            ):
                for component in underlying:
                    if not isinstance(component, Mapping):
                        continue
                    component_weight = max(
                        0.0, _weight(component.get("weight", 0))
                    )
                    if component_weight <= 0:
                        continue
                    parsed_underlying.append(
                        (
                            component,
                            component_weight,
                            str(component.get("sector", UNKNOWN_SECTOR)).upper(),
                        )
                    )
            underlying_total = sum(item[1] for item in parsed_underlying)
            if not 0.95 <= underlying_total <= 1.000001:
                parsed_underlying = []
                underlying_total = 0.0
            if parsed_underlying:
                lookthrough_value_weight += weight
                for component, component_weight, component_sector in parsed_underlying:
                    contribution = weight * component_weight
                    lookthrough_sector_weights[component_sector] = (
                        lookthrough_sector_weights.get(component_sector, 0.0)
                        + contribution
                    )
                    sector_contributions.setdefault(component_sector, []).append(
                        {
                            "fund_symbol": str(holding.get("symbol", "")),
                            "underlying_symbol": str(component.get("symbol", "")),
                            "underlying_name": str(component.get("name", "")),
                            "contribution": round(contribution, 6),
                            "source": str(
                                component.get(
                                    "source", holding.get("lookthrough_source", "")
                                )
                            ),
                            "as_of": str(
                                component.get(
                                    "as_of", holding.get("lookthrough_as_of", "")
                                )
                            ),
                        }
                    )
            else:
                lookthrough_sector_weights[sector] = (
                    lookthrough_sector_weights.get(sector, 0.0) + weight
                )
                sector_contributions.setdefault(sector, []).append(
                    {
                        "fund_symbol": None,
                        "underlying_symbol": str(holding.get("symbol", "")),
                        "underlying_name": str(holding.get("name", "")),
                        "contribution": round(weight, 6),
                        "source": str(
                            holding.get("sector_source", "DIRECT_CLASSIFICATION")
                        ),
                        "as_of": str(holding.get("price_date", "")),
                    }
                )
            positions.append(
                {
                    "symbol": str(holding.get("symbol", "")),
                    "name": str(holding.get("name", "")),
                    "sector": sector,
                    "weight": round(weight, 6),
                    "lookthrough_applied": bool(parsed_underlying),
                }
            )

        alerts: list[dict[str, Any]] = []
        if holdings and (
            weight_source == "UNAVAILABLE" or unweighted_positions > 0
        ):
            alerts.append(
                {
                    "type": "WEIGHT_DATA_INCOMPLETE",
                    "rule_id": "DATA.WEIGHT.COMPLETENESS",
                    "severity": "CRITICAL",
                    "key": "PORTFOLIO",
                    "weight": 0.0,
                    "limit": 0.0,
                    "current_value": unweighted_positions,
                    "threshold": 0,
                    "excess": unweighted_positions,
                    "affected_holdings": [
                        item["symbol"] for item in positions if item["weight"] == 0
                    ],
                    "recommended_action": "补齐估值后重新计算，禁止把缺失价格当作零。",
                    "unweighted_positions": unweighted_positions,
                }
            )
        for position in positions:
            weight = position["weight"]
            if weight > self.policy.max_single_position:
                alerts.append(
                    {
                        "type": "SINGLE_POSITION_LIMIT",
                        "rule_id": "RISK.SINGLE_POSITION.MAX",
                        "severity": "CRITICAL",
                        "key": position["symbol"],
                        "weight": weight,
                        "limit": self.policy.max_single_position,
                        "current_value": weight,
                        "threshold": self.policy.max_single_position,
                        "excess": round(
                            weight - self.policy.max_single_position, 6
                        ),
                        "affected_holdings": [position["symbol"]],
                        "recommended_action": "停止新增并在成本评估后制定分批降险计划。",
                    }
                )
            elif weight >= self.policy.max_single_position * self.policy.warning_ratio:
                alerts.append(
                    {
                        "type": "SINGLE_POSITION_NEAR_LIMIT",
                        "rule_id": "RISK.SINGLE_POSITION.WARNING",
                        "severity": "WARNING",
                        "key": position["symbol"],
                        "weight": weight,
                        "limit": self.policy.max_single_position,
                        "current_value": weight,
                        "threshold": self.policy.max_single_position,
                        "excess": 0.0,
                        "affected_holdings": [position["symbol"]],
                        "recommended_action": "暂停新增并持续监控。",
                    }
                )

        sector_weights = lookthrough_sector_weights
        sectors = []
        for sector, weight in sorted(
            sector_weights.items(), key=lambda item: item[1], reverse=True
        ):
            limit = (
                self.policy.max_unknown_sector_weight
                if sector == UNKNOWN_SECTOR
                else self.policy.max_sector_weight
            )
            status = (
                "BREACH"
                if weight > limit
                else "NEAR_LIMIT"
                if weight >= limit * self.policy.warning_ratio
                else "OK"
            )
            sectors.append(
                {
                    "sector": sector,
                    "weight": round(weight, 6),
                    "limit": limit,
                    "status": status,
                }
            )
            if status != "OK":
                alerts.append(
                    {
                        "type": (
                            "UNKNOWN_SECTOR_LIMIT"
                            if sector == UNKNOWN_SECTOR
                            else "SECTOR_LIMIT"
                        ),
                        "rule_id": (
                            "RISK.SECTOR.UNKNOWN.MAX"
                            if sector == UNKNOWN_SECTOR
                            else "RISK.SECTOR.MAX"
                        ),
                        "severity": "CRITICAL" if status == "BREACH" else "WARNING",
                        "key": sector,
                        "weight": round(weight, 6),
                        "limit": limit,
                        "current_value": round(weight, 6),
                        "threshold": limit,
                        "excess": round(max(0.0, weight - limit), 6),
                        "affected_holdings": sorted(
                            {
                                str(item["fund_symbol"] or item["underlying_symbol"])
                                for item in sector_contributions.get(sector, [])
                            }
                        ),
                        "recommended_action": (
                            "补齐底层持仓穿透数据。"
                            if sector == UNKNOWN_SECTOR
                            else "停止新增该风险桶，并模拟分批降至边界内。"
                        ),
                    }
                )

        def _sector_rows(values: Mapping[str, float]) -> list[dict[str, Any]]:
            return [
                {"sector": key, "weight": round(value, 6)}
                for key, value in sorted(
                    values.items(), key=lambda item: item[1], reverse=True
                )
            ]

        return {
            "policy_id": self.policy.policy_id,
            "policy_version": self.policy.policy_version,
            "weight_source": weight_source,
            "unweighted_positions": unweighted_positions,
            "positions": positions,
            "sectors": sectors,
            "direct_sectors": _sector_rows(direct_sector_weights),
            "lookthrough_sectors": _sector_rows(lookthrough_sector_weights),
            "sector_contributions": {
                key: sorted(
                    values,
                    key=lambda item: item["contribution"],
                    reverse=True,
                )
                for key, values in sector_contributions.items()
            },
            "lookthrough_coverage": round(lookthrough_value_weight, 6),
            "alerts": alerts,
            "violations": alerts,
            "hard_limit_breached": any(
                alert["severity"] == "CRITICAL" for alert in alerts
            ),
        }

    def pre_trade_check(
        self,
        holdings: Sequence[Mapping[str, Any]],
        symbol: str,
        action: str = "BUY",
        proposed_weight: float = 0.0,
        name: str = "",
        sector: str = "",
    ) -> dict[str, Any]:
        """Check projected single-name and sector exposure before adding risk.

        ``proposed_weight`` is the order value as a fraction of total portfolio
        assets, not a fraction of the current cash balance.
        """

        action = action.upper()
        if action not in ("BUY", "ADD"):
            return {
                "verdict": "PASS",
                "sector": classify_sector(
                    {"symbol": symbol, "name": name, "sector": sector}, self.policy
                ),
                "violations": [],
            }

        proposed_weight = max(0.0, _weight(proposed_weight))
        current = self.assess(holdings)
        target = next(
            (p for p in current["positions"] if p["symbol"] == symbol),
            {"weight": 0.0},
        )
        target_sector = classify_sector(
            {"symbol": symbol, "name": name, "sector": sector}, self.policy
        )
        sector_current = next(
            (s["weight"] for s in current["sectors"] if s["sector"] == target_sector),
            0.0,
        )
        projected_single = float(target["weight"]) + proposed_weight
        projected_sector = float(sector_current) + proposed_weight
        sector_limit = (
            self.policy.max_unknown_sector_weight
            if target_sector == UNKNOWN_SECTOR
            else self.policy.max_sector_weight
        )

        violations = []
        if projected_single > self.policy.max_single_position:
            violations.append(
                {
                    "type": "SINGLE_POSITION_LIMIT",
                    "projected": round(projected_single, 6),
                    "limit": self.policy.max_single_position,
                }
            )
        if projected_sector > sector_limit:
            violations.append(
                {
                    "type": (
                        "UNKNOWN_SECTOR_LIMIT"
                        if target_sector == UNKNOWN_SECTOR
                        else "SECTOR_LIMIT"
                    ),
                    "projected": round(projected_sector, 6),
                    "limit": sector_limit,
                }
            )

        if violations:
            verdict = "BLOCK"
        elif (
            projected_single >= self.policy.max_single_position * self.policy.warning_ratio
            or projected_sector >= sector_limit * self.policy.warning_ratio
        ):
            verdict = "CAUTION"
        else:
            verdict = "PASS"
        return {
            "verdict": verdict,
            "sector": target_sector,
            "current_single_weight": round(float(target["weight"]), 6),
            "projected_single_weight": round(projected_single, 6),
            "current_sector_weight": round(float(sector_current), 6),
            "projected_sector_weight": round(projected_sector, 6),
            "violations": violations,
        }


class DrawdownGuard:
    """High-water-mark risk tiers for a cash-flow-adjusted NAV series."""

    def __init__(self, policy: ConcentrationPolicy):
        self.policy = policy

    def assess(self, nav_history: Sequence[float]) -> dict[str, Any]:
        values = [float(value) for value in nav_history]
        if not values or any(value <= 0 for value in values):
            return {
                "status": "DATA_REQUIRED",
                "reason": "需要严格为正的现金流调整后净值序列",
                "allow_new_risk": False,
            }

        peak = max(values)
        current = values[-1]
        drawdown = max(0.0, 1 - current / peak)
        recovery = peak / current - 1 if current > 0 else float("inf")
        active = {
            "level": "NORMAL",
            "threshold": 0.0,
            "risk_budget_multiplier": 1.0,
            "allow_new_risk": True,
        }
        for tier in self.policy.drawdown_tiers:
            if drawdown >= tier["threshold"]:
                active = tier

        return {
            "status": active["level"],
            "peak_nav": round(peak, 6),
            "current_nav": round(current, 6),
            "drawdown": round(drawdown, 6),
            "recovery_to_peak": round(recovery, 6),
            "risk_budget_multiplier": active["risk_budget_multiplier"],
            "allow_new_risk": active["allow_new_risk"],
            "trigger_threshold": active["threshold"],
        }
