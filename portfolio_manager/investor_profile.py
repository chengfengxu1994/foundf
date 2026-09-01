"""Investor goal, risk-capacity and risk-willingness assessment.

The effective profile is always the more conservative of objective capacity
and stated willingness.  No allocation policy becomes active until the client
explicitly confirms a complete, non-expired profile.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping


REQUIRED_FIELDS = (
    "goal_type",
    "horizon_years",
    "monthly_essential_expense_cny",
    "emergency_fund_months",
    "known_cash_need_12m_cny",
    "income_stability",
    "max_acceptable_drawdown",
    "loss_reaction",
    "investment_experience",
    "planned_monthly_contribution_cny",
    "assessed_at",
)

MODEL_PORTFOLIOS = {
    "CAPITAL_PRESERVATION": {
        "LIQUIDITY": 0.30,
        "FIXED_INCOME": 0.50,
        "GOLD": 0.10,
        "EQUITY": 0.10,
    },
    "CONSERVATIVE": {
        "LIQUIDITY": 0.25,
        "FIXED_INCOME": 0.45,
        "GOLD": 0.10,
        "EQUITY": 0.20,
    },
    "BALANCED": {
        "LIQUIDITY": 0.20,
        "FIXED_INCOME": 0.30,
        "GOLD": 0.10,
        "EQUITY": 0.40,
    },
    "GROWTH": {
        "LIQUIDITY": 0.15,
        "FIXED_INCOME": 0.20,
        "GOLD": 0.05,
        "EQUITY": 0.60,
    },
    "AGGRESSIVE_GROWTH": {
        "LIQUIDITY": 0.10,
        "FIXED_INCOME": 0.10,
        "GOLD": 0.05,
        "EQUITY": 0.75,
    },
}
PROFILE_ORDER = tuple(MODEL_PORTFOLIOS)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bucket(score: float) -> str:
    if score < 0.20:
        return "CAPITAL_PRESERVATION"
    if score < 0.40:
        return "CONSERVATIVE"
    if score < 0.65:
        return "BALANCED"
    if score < 0.85:
        return "GROWTH"
    return "AGGRESSIVE_GROWTH"


def _bands(targets: Mapping[str, float]) -> dict[str, list[float]]:
    return {
        key: [round(max(0.0, value - 0.05), 6), round(min(1.0, value + 0.05), 6)]
        for key, value in targets.items()
    }


class InvestorProfileEngine:
    """Validate a profile and propose, but never silently activate, a policy."""

    def assess(
        self,
        raw: Mapping[str, Any],
        total_asset_cny: float,
        as_of: str | date | None = None,
    ) -> dict[str, Any]:
        today = (
            as_of
            if isinstance(as_of, date)
            else date.fromisoformat(str(as_of)[:10])
            if as_of
            else date.today()
        )
        missing = [field for field in REQUIRED_FIELDS if raw.get(field) is None]
        errors: list[str] = []
        if missing:
            errors.append("REQUIRED_FIELDS_MISSING")

        horizon = _number(raw.get("horizon_years"))
        monthly_expense = _number(raw.get("monthly_essential_expense_cny"))
        emergency_months = _number(raw.get("emergency_fund_months"))
        cash_need = _number(raw.get("known_cash_need_12m_cny"))
        max_drawdown = _number(raw.get("max_acceptable_drawdown"))
        contribution = _number(raw.get("planned_monthly_contribution_cny"))
        total_asset = max(0.0, float(total_asset_cny or 0))

        if horizon is not None and not 0 < horizon <= 80:
            errors.append("HORIZON_INVALID")
        if monthly_expense is not None and monthly_expense < 0:
            errors.append("MONTHLY_EXPENSE_INVALID")
        if emergency_months is not None and not 0 <= emergency_months <= 36:
            errors.append("EMERGENCY_MONTHS_INVALID")
        if cash_need is not None and cash_need < 0:
            errors.append("CASH_NEED_INVALID")
        if max_drawdown is not None and not 0 < max_drawdown <= 0.60:
            errors.append("MAX_DRAWDOWN_INVALID")
        if contribution is not None and contribution < 0:
            errors.append("CONTRIBUTION_INVALID")

        income = str(raw.get("income_stability", "")).upper()
        reaction = str(raw.get("loss_reaction", "")).upper()
        experience = str(raw.get("investment_experience", "")).upper()
        goal_type = str(raw.get("goal_type", "")).upper()
        if income and income not in {"LOW", "MEDIUM", "HIGH"}:
            errors.append("INCOME_STABILITY_INVALID")
        if reaction and reaction not in {"SELL_ALL", "SELL_SOME", "HOLD", "BUY_MORE"}:
            errors.append("LOSS_REACTION_INVALID")
        if experience and experience not in {"LOW", "MEDIUM", "HIGH"}:
            errors.append("INVESTMENT_EXPERIENCE_INVALID")
        if goal_type and goal_type not in {
            "CAPITAL_PRESERVATION",
            "LONG_TERM_WEALTH",
            "RETIREMENT",
            "MAJOR_PURCHASE",
            "INCOME",
        }:
            errors.append("GOAL_TYPE_INVALID")

        try:
            assessed_at = date.fromisoformat(str(raw.get("assessed_at", ""))[:10])
            review_due = assessed_at + timedelta(days=365)
            if assessed_at > today:
                errors.append("ASSESSED_AT_IN_FUTURE")
        except ValueError:
            assessed_at = None
            review_due = None
            if "assessed_at" not in missing:
                errors.append("ASSESSED_AT_INVALID")
        expired = bool(review_due and today > review_due)
        if expired:
            errors.append("PROFILE_REVIEW_OVERDUE")

        horizon_points = (
            0 if horizon is None or horizon < 2
            else 1 if horizon < 5
            else 2 if horizon < 10
            else 3 if horizon < 15
            else 4
        )
        income_points = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(income, 0)
        contribution_points = 1 if contribution and contribution > 0 else 0
        capacity_score = (horizon_points + income_points + contribution_points) / 7

        drawdown_points = (
            0 if max_drawdown is None or max_drawdown < 0.08
            else 1 if max_drawdown < 0.12
            else 2 if max_drawdown < 0.20
            else 3 if max_drawdown < 0.30
            else 4
        )
        reaction_points = {
            "SELL_ALL": 0,
            "SELL_SOME": 1,
            "HOLD": 2,
            "BUY_MORE": 3,
        }.get(reaction, 0)
        experience_points = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(experience, 0)
        willingness_score = (
            drawdown_points + reaction_points + experience_points
        ) / 9
        effective_score = min(capacity_score, willingness_score)
        level = _bucket(effective_score)

        liquidity_need = max(
            0.0,
            (monthly_expense or 0) * (emergency_months or 0) + (cash_need or 0),
        )
        liquidity_need_pct = (
            liquidity_need / total_asset if total_asset > 0 else 1.0
        )
        if horizon is not None and horizon < 3:
            level = PROFILE_ORDER[min(PROFILE_ORDER.index(level), 1)]
        if liquidity_need_pct >= 0.40:
            level = "CAPITAL_PRESERVATION"
        targets = dict(MODEL_PORTFOLIOS[level])
        required_liquidity_weight = max(
            targets["LIQUIDITY"], min(0.60, liquidity_need_pct)
        )
        excess = required_liquidity_weight - targets["LIQUIDITY"]
        targets["LIQUIDITY"] = required_liquidity_weight
        for asset_class in ("EQUITY", "FIXED_INCOME", "GOLD"):
            reduction = min(excess, targets[asset_class])
            targets[asset_class] -= reduction
            excess -= reduction
            if excess <= 0:
                break
        total = sum(targets.values())
        targets = {key: round(value / total, 6) for key, value in targets.items()}

        confirmed = raw.get("confirmed") is True
        activation_ready = confirmed and not errors
        if not confirmed:
            errors.append("CLIENT_CONFIRMATION_REQUIRED")
        policy = {
            "policy_id": f"investor_profile_{level.lower()}",
            "policy_version": (
                assessed_at.isoformat() if assessed_at else today.isoformat()
            ),
            "profile_confirmed": activation_ready,
            "targets": targets,
            "bands": _bands(targets),
            "min_valuation_coverage": 0.95,
            "max_unknown_weight": 0.05,
            "min_liquidity_amount": round(liquidity_need, 2),
            "min_liquidity_pct": targets["LIQUIDITY"],
            "cashflow_first": True,
            "symbol_overrides": {},
        }
        return {
            "schema_version": "foundf.investor_profile_assessment.v1",
            "activation_ready": activation_ready,
            "confirmed": confirmed,
            "profile_level": level,
            "capacity_score": round(capacity_score, 6),
            "willingness_score": round(willingness_score, 6),
            "effective_score": round(effective_score, 6),
            "liquidity_need_cny": round(liquidity_need, 2),
            "liquidity_need_pct": round(liquidity_need_pct, 6),
            "assessed_at": assessed_at.isoformat() if assessed_at else None,
            "review_due": review_due.isoformat() if review_due else None,
            "missing_fields": missing,
            "errors": list(dict.fromkeys(errors)),
            "proposed_allocation_policy": policy,
            "method": "LOWER_OF_CAPACITY_AND_WILLINGNESS",
            "disclaimer": (
                "画像结果用于选择风险预算，不保证收益；"
                "客户必须核对全部输入并显式确认后才能激活。"
            ),
        }


def load_profile_assessment(
    profile_path: str | Path,
    total_asset_cny: float,
    as_of: str | date | None = None,
) -> dict[str, Any]:
    path = Path(profile_path)
    if not path.exists():
        raw: dict[str, Any] = {}
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
    return InvestorProfileEngine().assess(raw, total_asset_cny, as_of=as_of)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate an investor profile")
    parser.add_argument("path")
    parser.add_argument("--total-assets", type=float, required=True)
    args = parser.parse_args()
    result = load_profile_assessment(args.path, args.total_assets)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["activation_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
