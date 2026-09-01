"""Low-turnover, policy-driven long-term portfolio strategy.

This module does not predict prices or create new factors.  It converts
validated allocation, concentration, turnover and drawdown evidence into a
small set of auditable portfolio states.  Exact trade budgets are emitted only
when the investor profile and all required data are ready.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping


ASSET_LABELS = {
    "LIQUIDITY": "现金与存款",
    "FIXED_INCOME": "债券与固收",
    "GOLD": "黄金",
    "EQUITY": "股票与权益基金",
}
SECTOR_LABELS = {
    "TECHNOLOGY": "科技与互联网",
    "TELECOMMUNICATIONS": "通信",
    "CONSUMER_DISCRETIONARY": "可选消费",
    "CONSUMER_STAPLES": "必选消费",
    "INDUSTRIALS": "工业与新能源",
    "FINANCIALS": "金融",
    "BROAD_EQUITY": "宽基权益",
    "HEALTHCARE": "医疗健康",
    "UNKNOWN": "待穿透",
}


def _normalise_weight(value: Any) -> float:
    result = float(value)
    return result / 100 if result > 1 else result


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    return date.fromisoformat(raw[:10])


@dataclass(frozen=True)
class LongTermStrategyPolicy:
    policy_id: str = "long_term_governance_reference"
    policy_version: str = "1"
    review_interval_days: int = 30
    max_market_age_days: int = 1
    max_rebalance_turnover: float = 0.05
    max_trailing_12m_turnover: float = 1.0
    min_trade_amount: float = 5000.0
    min_nav_observations: int = 20
    require_turnover_data: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LongTermStrategyPolicy":
        policy = cls(
            policy_id=str(raw.get("policy_id", cls.policy_id)),
            policy_version=str(raw.get("policy_version", cls.policy_version)),
            review_interval_days=int(
                raw.get("review_interval_days", cls.review_interval_days)
            ),
            max_market_age_days=int(
                raw.get("max_market_age_days", cls.max_market_age_days)
            ),
            max_rebalance_turnover=_normalise_weight(
                raw.get("max_rebalance_turnover", cls.max_rebalance_turnover)
            ),
            max_trailing_12m_turnover=_normalise_weight(
                raw.get(
                    "max_trailing_12m_turnover",
                    cls.max_trailing_12m_turnover,
                )
            ),
            min_trade_amount=float(
                raw.get("min_trade_amount", cls.min_trade_amount)
            ),
            min_nav_observations=int(
                raw.get("min_nav_observations", cls.min_nav_observations)
            ),
            require_turnover_data=bool(
                raw.get("require_turnover_data", cls.require_turnover_data)
            ),
        )
        if policy.review_interval_days < 1:
            raise ValueError("review_interval_days must be positive")
        if policy.max_market_age_days < 0:
            raise ValueError("max_market_age_days cannot be negative")
        if not 0 < policy.max_rebalance_turnover <= 1:
            raise ValueError("max_rebalance_turnover must be in (0, 1]")
        if not 0 < policy.max_trailing_12m_turnover <= 10:
            raise ValueError("max_trailing_12m_turnover must be in (0, 10]")
        if policy.min_trade_amount < 0 or policy.min_nav_observations < 2:
            raise ValueError("invalid trade amount or NAV observation threshold")
        return policy

    @classmethod
    def load(
        cls, path: str | Path = "config/long_term_strategy.json"
    ) -> "LongTermStrategyPolicy":
        policy_path = Path(path)
        if not policy_path.exists():
            return cls()
        return cls.from_mapping(json.loads(policy_path.read_text(encoding="utf-8")))


def calculate_trailing_turnover(
    events_path: str | Path,
    total_asset: float,
    as_of: str | date,
    window_days: int = 365,
) -> dict[str, Any]:
    """Estimate one-way turnover from audited BUY/SELL transaction notional.

    Until a full daily average-NAV history exists, current total assets are used
    as the denominator and the result is explicitly labelled as a proxy.
    """

    path = Path(events_path)
    total_asset = float(total_asset)
    if not path.exists() or total_asset <= 0:
        return {
            "available": False,
            "reason": "EVENTS_OR_ASSET_VALUE_MISSING",
            "method": "HALF_GROSS_OVER_CURRENT_ASSET_PROXY",
        }
    end = _parse_date(as_of)
    start = end - timedelta(days=window_days)
    buys = 0.0
    sells = 0.0
    trade_count = 0
    with open(path, encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("event_type") not in {"BUY", "SELL"}:
                continue
            try:
                trade_date = _parse_date(row.get("date", ""))
                notional = abs(float(row.get("total_amount", 0) or 0))
            except (TypeError, ValueError):
                continue
            if not start < trade_date <= end:
                continue
            trade_count += 1
            if row["event_type"] == "BUY":
                buys += notional
            else:
                sells += notional
    one_way = (buys + sells) / 2
    return {
        "available": True,
        "as_of": end.isoformat(),
        "window_days": window_days,
        "trade_count": trade_count,
        "buy_notional": round(buys, 2),
        "sell_notional": round(sells, 2),
        "one_way_notional": round(one_way, 2),
        "turnover": round(one_way / total_asset, 6),
        "denominator": round(total_asset, 2),
        "method": "HALF_GROSS_OVER_CURRENT_ASSET_PROXY",
        "provisional": True,
    }


class LongTermStrategyEngine:
    """Convert portfolio evidence into an execution-safe long-term state."""

    def __init__(self, policy: LongTermStrategyPolicy):
        self.policy = policy

    def evaluate(
        self,
        *,
        as_of: str | date,
        market_data_date: str | date | None,
        allocation: Mapping[str, Any],
        concentration: Mapping[str, Any],
        turnover: Mapping[str, Any] | None = None,
        drawdown: Mapping[str, Any] | None = None,
        last_review_date: str | date | None = None,
    ) -> dict[str, Any]:
        today = _parse_date(as_of)
        turnover = dict(turnover or {})
        drawdown = dict(drawdown or {})
        blockers = list(allocation.get("blockers", []))
        warnings: list[str] = []

        market_age_days = None
        if market_data_date is None:
            blockers.append("MARKET_DATA_DATE_MISSING")
        else:
            market_day = _parse_date(market_data_date)
            market_age_days = max(0, (today - market_day).days)
            if market_age_days > self.policy.max_market_age_days:
                blockers.append("MARKET_DATA_STALE")

        turnover_available = bool(turnover.get("available"))
        turnover_value = (
            float(turnover.get("turnover", 0)) if turnover_available else None
        )
        turnover_breached = bool(
            turnover_available
            and turnover_value is not None
            and turnover_value > self.policy.max_trailing_12m_turnover
        )
        if not turnover_available and self.policy.require_turnover_data:
            blockers.append("TURNOVER_DATA_MISSING")
        if turnover_breached:
            warnings.append("TRAILING_TURNOVER_LIMIT_BREACHED")

        nav_observations = int(drawdown.get("observations", 0) or 0)
        if nav_observations < self.policy.min_nav_observations:
            warnings.append("NAV_HISTORY_BUILDING")

        review_due = True
        next_review_date = today
        if last_review_date is not None:
            last_review = _parse_date(last_review_date)
            next_review_date = last_review + timedelta(
                days=self.policy.review_interval_days
            )
            review_due = today >= next_review_date

        breached_sectors = [
            row["sector"]
            for row in concentration.get("sectors", [])
            if row.get("status") == "BREACH"
        ]
        hard_limit_breached = bool(
            concentration.get("hard_limit_breached") or breached_sectors
        )
        drawdown_blocks_risk = (
            drawdown.get("data_ready", False)
            and drawdown.get("allow_new_risk") is False
        )
        allow_new_risk = not (
            blockers
            or turnover_breached
            or hard_limit_breached
            or drawdown_blocks_risk
        )

        allocation_data_blockers = {
            "VALUATION_COVERAGE_LOW",
            "UNKNOWN_EXPOSURE_HIGH",
            "NO_VALUED_ASSETS",
        }
        allocation_reliable = not (
            allocation_data_blockers.intersection(blockers)
        )
        drift_rows = (
            [
                row
                for row in allocation.get("economic_allocation", [])
                if row.get("status") in {"UNDERWEIGHT", "OVERWEIGHT"}
            ]
            if allocation_reliable
            else []
        )
        underweight = sorted(
            (
                row
                for row in drift_rows
                if row.get("status") == "UNDERWEIGHT"
                and float(row.get("amount_to_target", 0)) > 0
            ),
            key=lambda row: (
                row.get("asset_class") != "LIQUIDITY",
                -float(row.get("amount_to_target", 0)),
            ),
        )
        total_gap = sum(float(row["amount_to_target"]) for row in underweight)
        contribution_priority = [
            {
                "asset_class": row["asset_class"],
                "label": ASSET_LABELS.get(row["asset_class"], row["asset_class"]),
                "gap_to_target": round(float(row["amount_to_target"]), 2),
                "reference_share_of_new_money": (
                    round(float(row["amount_to_target"]) / total_gap, 6)
                    if total_gap > 0
                    else 0.0
                ),
                "executable": False,
            }
            for row in underweight
        ]

        profile_missing = "INVESTOR_PROFILE_UNCONFIRMED" in blockers
        data_blockers = [
            item
            for item in blockers
            if item != "INVESTOR_PROFILE_UNCONFIRMED"
        ]
        if data_blockers:
            state = "DATA_REVIEW"
        elif profile_missing:
            state = "PROFILE_REQUIRED"
        elif turnover_breached:
            state = "TURNOVER_COOLDOWN"
        elif drawdown_blocks_risk:
            state = "DRAWDOWN_DEFENSIVE"
        elif hard_limit_breached:
            state = "CONCENTRATION_REDUCTION"
        elif not review_due:
            state = "MONITOR"
        elif drift_rows:
            state = "CASHFLOW_REBALANCE"
        else:
            state = "HOLD"

        execution_ready = not blockers and review_due
        if execution_ready:
            for item in contribution_priority:
                item["executable"] = allow_new_risk
        total_value = float(allocation.get("known_total_value", 0) or 0)
        trade_budget = (
            max(
                0.0,
                total_value * self.policy.max_rebalance_turnover,
            )
            if execution_ready
            else 0.0
        )
        if trade_budget < self.policy.min_trade_amount:
            trade_budget = 0.0

        actions: list[dict[str, str]] = []
        if market_age_days is None or "MARKET_DATA_STALE" in blockers:
            actions.append(
                {
                    "severity": "BLOCK",
                    "title": "先更新完整行情",
                    "detail": "行情日期未达到策略要求，禁止生成交易金额。",
                }
            )
        if profile_missing:
            actions.append(
                {
                    "severity": "BLOCK",
                    "title": "确认长期投资者画像",
                    "detail": "目标期限、最大可承受回撤与流动性需求未确认，当前配置仅作参考。",
                }
            )
        if turnover_breached:
            actions.append(
                {
                    "severity": "BLOCK",
                    "title": "进入换手冷静期",
                    "detail": (
                        f"近 12 个月换手代理值 {turnover_value:.1%}，"
                        f"超过治理上限 {self.policy.max_trailing_12m_turnover:.0%}；"
                        "暂停新增主动风险。"
                    ),
                }
            )
        if breached_sectors:
            actions.append(
                {
                    "severity": "BLOCK",
                    "title": "停止增加超限行业",
                    "detail": "已超限风险桶："
                    + "、".join(
                        SECTOR_LABELS.get(item, item) for item in breached_sectors
                    ),
                }
            )
        liquidity_shortfall = float(
            allocation.get("liquidity_shortfall", 0) or 0
        )
        if liquidity_shortfall > 0:
            actions.append(
                {
                    "severity": "PRIORITY",
                    "title": "优先恢复流动性安全垫",
                    "detail": f"参考缺口 {liquidity_shortfall:,.0f} 元；优先使用未来新增现金补足。",
                }
            )
        if underweight:
            names = "、".join(
                ASSET_LABELS.get(row["asset_class"], row["asset_class"])
                for row in underweight
                if row["asset_class"] != "LIQUIDITY"
            )
            if names:
                actions.append(
                    {
                        "severity": "INFO",
                        "title": "新增资金只投向低配资产",
                        "detail": f"参考优先级覆盖 {names}；不追涨当前超配风险。",
                    }
                )

        return {
            "policy_id": self.policy.policy_id,
            "policy_version": self.policy.policy_version,
            "state": state,
            "execution_ready": execution_ready,
            "allow_new_risk": allow_new_risk,
            "review_due": review_due,
            "next_review_date": next_review_date.isoformat(),
            "market_age_days": market_age_days,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": warnings,
            "blocked_sectors": breached_sectors,
            "trailing_turnover": turnover,
            "trade_budget": round(trade_budget, 2),
            "contribution_priority": contribution_priority,
            "actions": actions,
            "disclaimer": (
                "本策略是风险治理与再平衡框架，不预测市场、"
                "不承诺收益，未满足执行条件时不得据此交易。"
            ),
        }
