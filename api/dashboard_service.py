"""Read-only customer dashboard data projection.

The dashboard intentionally prefers explainable ledger weights over invented
market values when live prices are unavailable.  Every response includes the
valuation mode so the UI can clearly distinguish a risk preview from a fully
priced portfolio.
"""

from __future__ import annotations

import csv
import json
import logging
import statistics
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_manager.asset_allocation import (
    AllocationPolicy,
    AssetAllocationEngine,
)
from portfolio_manager.concentration_guard import (
    ConcentrationGuard,
    ConcentrationPolicy,
    DrawdownGuard,
)
from portfolio_manager.daily_position_contract import validate_daily_position_update
from portfolio_manager.long_term_strategy import (
    LongTermStrategyEngine,
    LongTermStrategyPolicy,
    calculate_trailing_turnover,
)
from portfolio_manager.investor_profile import load_profile_assessment
from portfolio_manager.compliance_lint import lint_advice_batch
from portfolio_manager.ips import InvestmentPolicyStatement, explain_violation
from portfolio_manager.portfolio_performance import calculate_performance
from portfolio_manager.nav_tracker import load_trusted_nav_history


LOGGER = logging.getLogger("foundf.dashboard")


ASSET_LABELS = {
    "LIQUIDITY": "现金与存款",
    "FIXED_INCOME": "债券与固收",
    "GOLD": "黄金",
    "EQUITY": "股票与权益基金",
}


def legacy_untrusted_projection(
    kind: str,
    *,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return a stable fail-closed response for retired portfolio projections."""

    if kind == "summary":
        return {
            "status": "LEGACY_UNTRUSTED",
            "decision_ready": False,
            "summary": None,
            "migration": (
                "使用 /api/dashboard 的 decision_gate 与 "
                "VALIDATED_DAILY_UPDATE；旧混币种账本不得用于收益或交易决策。"
            ),
        }
    if kind == "returns":
        return {
            "status": "LEGACY_UNTRUSTED",
            "decision_ready": False,
            "performance": None,
            "migration": (
                "使用 /api/dashboard.performance；只有可信现金流调整单位净值"
                "达到要求时才返回 READY。"
            ),
        }
    if kind == "snapshots":
        return {
            "status": "LEGACY_UNTRUSTED",
            "decision_ready": False,
            "total": 0,
            "offset": offset,
            "limit": limit,
            "snapshots": [],
            "migration": (
                "使用通过估值覆盖率门禁的 data/portfolio_nav_history.json；"
                "旧资产总额序列不得解释为投资收益。"
            ),
        }
    raise ValueError(f"unknown legacy projection: {kind}")


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _weight(value: Any) -> float:
    result = _number(value)
    return result / 100 if result > 1 else result


def _load_holdings(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    holdings = []
    for row in rows:
        holdings.append(
            {
                "symbol": row.get("symbol", ""),
                "name": row.get("name", ""),
                "shares": _number(row.get("shares")),
                "total_cost": _number(row.get("total_cost")),
                "market_value": _number(row.get("market_value")),
                "weight": _weight(row.get("weight")),
                "profit_loss": _number(row.get("profit_loss")),
                "profit_rate": _number(row.get("profit_rate")),
                "price_date": row.get("price_date") or None,
                "price_source": row.get("price_source") or None,
                "freshness": (
                    "T_CLOSE"
                    if _number(row.get("market_value")) > 0
                    else "VALUATION_MISSING"
                ),
            }
        )
    return holdings


def _infer_ledger_total(holdings: list[dict[str, Any]]) -> float:
    """Infer the account total represented by stored ledger weights."""

    estimates = [
        item["total_cost"] / item["weight"]
        for item in holdings
        if item["total_cost"] > 0 and item["weight"] > 0
    ]
    return statistics.median(estimates) if estimates else 0.0


def _freshness(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"updated_at": None, "age_hours": None, "stale": True}
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 3600)
    return {
        "updated_at": modified.isoformat(),
        "age_hours": round(age, 1),
        "stale": age > 36,
    }


def _latest_cash(path: Path) -> float | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return _number(rows[-1].get("broker_bal")) if rows else None


def _market_data_date(data_dir: Path | None) -> date | None:
    if data_dir is None:
        return None
    dates = []
    for name in ("cn_prices.json", "hk_prices.json"):
        path = data_dir / "market_cache" / name
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            dates.append(date.fromisoformat(str(raw["_date"])))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return min(dates) if dates else None


def _nav_history(path: Path | None) -> list[dict[str, Any]]:
    """Compatibility wrapper around the shared trusted NAV read boundary."""

    return load_trusted_nav_history(path) if path is not None else []


def _validated_daily_update(
    data_dir: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if data_dir is None:
        return None, None
    path = data_dir / "daily_position_update.json"
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, {"valid": False, "errors": ["DAILY_UPDATE_JSON_INVALID"]}
    validation = validate_daily_position_update(payload)
    return (payload if validation["valid"] else None), validation


def _holdings_from_daily_update(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": str(row.get("symbol", "")),
            "name": str(row.get("name", "")),
            "shares": _number(row.get("shares")),
            "total_cost": _number(row.get("total_cost_cny")),
            "market_value": _number(row.get("market_value_cny")),
            "weight": 0.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
            "currency": str(row.get("currency", "")),
            "price_date": str(row.get("price_date", "")),
            "price_source": str(row.get("price_source", "")),
            "freshness": str(row.get("freshness", "T_CLOSE")),
            "instrument_status": str(row.get("instrument_status", "ACTIVE")),
            "underlying_holdings": row.get("underlying_holdings", []),
            "lookthrough_source": str(row.get("lookthrough_source", "")),
            "lookthrough_as_of": str(row.get("lookthrough_as_of", "")),
        }
        for row in payload.get("positions", [])
    ]


def build_customer_dashboard(
    reports_dir: str | Path,
    allocation_policy_path: str | Path,
    risk_policy_path: str | Path,
    data_dir: str | Path | None = None,
    strategy_policy_path: str | Path | None = None,
    investor_profile_path: str | Path | None = None,
    ips_path: str | Path | None = None,
    data_assets_health: dict[str, Any] | None = None,
    strategy_governance: dict[str, Any] | None = None,
    runtime_automation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable JSON projection for the customer-facing dashboard."""

    started = time.perf_counter()
    reports_path = Path(reports_dir)
    data_path = Path(data_dir) if data_dir is not None else None
    ledger_path = reports_path / "portfolio_holdings_latest.csv"
    market_path = reports_path / "portfolio_with_market_values.csv"
    daily_path = data_path / "daily_position_update.json" if data_path else None
    daily_update, daily_validation = _validated_daily_update(data_path)
    market_holdings = _load_holdings(market_path)
    legacy_has_unconverted_hk = any(
        len(item["symbol"]) == 5 and item["symbol"].startswith("0")
        for item in market_holdings
    )
    if daily_update is not None and daily_path is not None:
        holdings_path = daily_path
        holdings = _holdings_from_daily_update(daily_update)
    else:
        # Legacy market-value CSVs do not carry the currency, FX, source,
        # valuation date and broker reconciliation contract required for a
        # trusted CNY valuation.  Keep them as diagnostic input only.
        holdings_path = ledger_path
        holdings = _load_holdings(holdings_path)
    allocation_policy = AllocationPolicy.load(allocation_policy_path)
    risk_policy = ConcentrationPolicy.load(risk_policy_path)

    market_total = sum(item["market_value"] for item in holdings)
    ledger_invested = sum(item["total_cost"] for item in holdings)
    ledger_total = _infer_ledger_total(holdings)
    explicit_weight_total = sum(item["weight"] for item in holdings)

    if daily_update is not None:
        valuation_mode = "VALIDATED_DAILY_UPDATE"
        cash = _number(daily_update["totals"].get("cash_cny"))
        total_asset = _number(daily_update["totals"].get("total_asset_cny"))
        market_total = sum(item["market_value"] for item in holdings)
        for item in holdings:
            item["weight"] = (
                item["market_value"] / total_asset if total_asset > 0 else 0.0
            )
        explicit_weight_total = sum(item["weight"] for item in holdings)
    else:
        valuation_mode = "LEDGER_WEIGHT"
        authoritative_cash = _latest_cash(
            reports_path / "broker_balance_chain_v4.csv"
        )
        cash = (
            max(0.0, authoritative_cash)
            if authoritative_cash is not None
            else max(0.0, ledger_total - ledger_invested)
        )
        total_asset = ledger_invested + cash
        for item in holdings:
            item["weight"] = (
                item["total_cost"] / total_asset if total_asset > 0 else 0.0
            )
        explicit_weight_total = sum(item["weight"] for item in holdings)

    profile_assessment = load_profile_assessment(
        investor_profile_path or Path("__investor_profile_missing__"),
        total_asset,
    )
    if profile_assessment["activation_ready"]:
        proposed_policy = dict(
            profile_assessment["proposed_allocation_policy"]
        )
        proposed_policy["symbol_overrides"] = (
            allocation_policy.symbol_overrides or {}
        )
        allocation_policy = AllocationPolicy.from_mapping(proposed_policy)

    concentration = ConcentrationGuard(risk_policy).assess(holdings, cash=cash)
    allocation_assessment = AssetAllocationEngine(allocation_policy).assess(
        holdings, cash
    )
    sector_by_symbol = {
        item["symbol"]: item["sector"] for item in concentration["positions"]
    }

    # Current holdings in this account are listed equity instruments. Unknown
    # sector metadata stays UNKNOWN instead of being silently called diversified.
    unknown_weight = sum(
        item["weight"]
        for item in holdings
        if sector_by_symbol.get(item["symbol"]) == "UNKNOWN"
    )
    current_weights = {
        "LIQUIDITY": max(0.0, 1.0 - explicit_weight_total),
        "FIXED_INCOME": 0.0,
        "GOLD": 0.0,
        "EQUITY": max(0.0, explicit_weight_total - unknown_weight),
    }

    allocation = []
    for asset_class in ("LIQUIDITY", "FIXED_INCOME", "GOLD", "EQUITY"):
        current = current_weights[asset_class]
        target = allocation_policy.targets[asset_class]
        lower, upper = allocation_policy.bands[asset_class]
        status = (
            "OVER"
            if current > upper
            else "UNDER"
            if current < lower
            else "ON_TRACK"
        )
        allocation.append(
            {
                "asset_class": asset_class,
                "label": ASSET_LABELS[asset_class],
                "current_weight": round(current, 6),
                "target_weight": target,
                "lower_bound": lower,
                "upper_bound": upper,
                "status": status,
            }
        )

    critical = [
        alert for alert in concentration["alerts"] if alert["severity"] == "CRITICAL"
    ]
    warnings = [
        alert for alert in concentration["alerts"] if alert["severity"] == "WARNING"
    ]
    risk_level = "CRITICAL" if critical else "WATCH" if warnings else "STABLE"
    tech = next(
        (
            sector
            for sector in concentration["sectors"]
            if sector["sector"] == "TECHNOLOGY"
        ),
        None,
    )
    if tech and tech["status"] == "BREACH":
        headline = (
            f"科技风险占组合 {tech['weight']:.1%}，"
            f"超过 {tech['limit']:.0%} 风险边界"
        )
    elif critical:
        headline = "组合存在需要优先处理的集中度风险"
    elif warnings:
        headline = "组合接近部分风险边界"
    else:
        headline = "组合风险暂处于设定边界内"

    top_positions = sorted(holdings, key=lambda item: item["weight"], reverse=True)
    top_positions = [
        {
            **item,
            "sector": sector_by_symbol.get(item["symbol"], "UNKNOWN"),
            "over_limit": item["weight"] > risk_policy.max_single_position,
        }
        for item in top_positions[:8]
    ]

    data_quality = _freshness(holdings_path)
    if daily_update is not None:
        market_date = date.fromisoformat(str(daily_update["as_of"])[:10])
    else:
        market_date = _market_data_date(data_path)
    if market_date is not None:
        market_age_days = max(0, (date.today() - market_date).days)
        data_quality.update(
            {
                "market_data_date": market_date.isoformat(),
                "market_age_days": market_age_days,
                "stale": market_age_days > 1,
            }
        )
    else:
        data_quality.update({"market_data_date": None, "market_age_days": None})
    blockers = []
    if valuation_mode != "VALIDATED_DAILY_UPDATE":
        if legacy_has_unconverted_hk:
            blockers.append("港股原币市值缺少人民币汇率换算，已拒绝多币种混算")
        elif market_holdings:
            blockers.append("旧市值文件缺少每日仓位合同与多币种对账，已拒绝作为可信估值")
        blockers.append("当前为账本仓位预览，等待通过校验的每日仓位快照")
    if daily_validation is not None and not daily_validation.get("valid"):
        blockers.append(
            "每日仓位快照校验失败：" + "、".join(daily_validation.get("errors", [])[:3])
        )
    if data_quality["stale"]:
        if market_date is not None:
            blockers.append(
                f"最新完整行情截至 {market_date.isoformat()}，不可视为实时价格"
            )
        else:
            blockers.append("持仓数据超过 36 小时未更新")
    if unknown_weight > risk_policy.max_unknown_sector_weight:
        blockers.append("部分基金或持仓缺少可靠行业穿透")

    nav_rows = _nav_history(
        data_path / "portfolio_nav_history.json" if data_path else None
    )
    if len(nav_rows) >= 2:
        drawdown = DrawdownGuard(risk_policy).assess(
            [row["unit_nav"] for row in nav_rows]
        )
        drawdown.update(
            {
                "data_ready": True,
                "observations": len(nav_rows),
                "message": "回撤使用现金流调整后的单位净值计算。",
                "tiers": list(risk_policy.drawdown_tiers),
            }
        )
    else:
        drawdown = {
            "status": "DATA_BUILDING",
            "data_ready": False,
            "observations": len(nav_rows),
            "message": "正在积累现金流调整后的每日净值，暂不展示可能误导的账户回撤。",
            "tiers": list(risk_policy.drawdown_tiers),
        }

    turnover = calculate_trailing_turnover(
        reports_path / "broker_economic_event_v4.csv",
        total_asset,
        market_date or date.today(),
    )
    strategy_policy = (
        LongTermStrategyPolicy.load(strategy_policy_path)
        if strategy_policy_path is not None
        else LongTermStrategyPolicy()
    )
    long_term_strategy = LongTermStrategyEngine(strategy_policy).evaluate(
        as_of=date.today(),
        market_data_date=market_date,
        allocation=allocation_assessment,
        concentration=concentration,
        turnover=turnover,
        drawdown=drawdown,
    )
    market_as_of = market_date.isoformat() if market_date else None
    for action in long_term_strategy.get("actions", []):
        action.update(
            {
                "text": f"{action.get('title', '')}：{action.get('detail', '')}",
                "rule_ids": [
                    f"STRATEGY.STATE.{long_term_strategy['state']}"
                ],
                "data_as_of": market_as_of or "UNKNOWN",
                "uncertainty": "估值、基金披露或投资者信息不完整时建议自动降级。",
                "ips_clause_ids": ["IPS-1.1", "IPS-5.1"],
            }
        )
    advice_lint = lint_advice_batch(long_term_strategy.get("actions", []))

    ips = InvestmentPolicyStatement.load(
        ips_path or "config/ips.example.json"
    ).as_api()
    risk_explanations = []
    for violation in concentration.get("violations", []):
        key = str(violation.get("key", ""))
        risk_explanations.append(
            explain_violation(
                violation,
                contributions=concentration.get("sector_contributions", {}).get(
                    key, []
                ),
                data_as_of=market_as_of,
                lookthrough_method=(
                    "UNDERLYING_WEIGHT"
                    if concentration.get("lookthrough_coverage", 0) > 0
                    else "DIRECT_CLASSIFICATION"
                ),
                ips_clause_ids=["IPS-3.1"],
            )
        )

    performance = calculate_performance(nav_rows)
    missing_count = sum(
        item.get("freshness") == "VALUATION_MISSING" for item in holdings
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    calculation_metrics = {
        "duration_ms": duration_ms,
        "holdings_count": len(holdings),
        "valuation_missing_rate": (
            round(missing_count / len(holdings), 6) if holdings else 1.0
        ),
        "rules_triggered": len(concentration.get("violations", [])),
        "lookthrough_coverage": concentration.get("lookthrough_coverage", 0.0),
    }

    health = data_assets_health or {
        "status": "BLOCKED_DATA",
        "decision_data_ready": False,
        "latest_market_date": None,
        "blockers": ["DATA_ASSET_HEALTH_NOT_PROVIDED"],
        "warnings": [],
    }
    governance = strategy_governance or {
        "status": "UNAVAILABLE",
        "stage": "BLOCKED_DATA",
        "evidence_ready": False,
        "blockers": ["STRATEGY_GOVERNANCE_STATUS_NOT_PROVIDED"],
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
        "human_approval_required": True,
    }
    global_health_ready = (
        health.get("status") == "READY"
        and health.get("decision_data_ready") is True
    )
    valuation_ready = valuation_mode == "VALIDATED_DAILY_UPDATE"
    strategy_ready = long_term_strategy.get("execution_ready") is True
    governance_ready = (
        governance.get("evidence_ready") is True
        and governance.get("stage") == "PENDING_HUMAN_REVIEW"
        and governance.get("production_change_allowed") is False
        and governance.get("automatic_trade_allowed") is False
    )
    decision_ready = (
        global_health_ready
        and valuation_ready
        and strategy_ready
        and governance_ready
    )
    decision_gate = {
        "status": "READY_FOR_HUMAN_REVIEW" if decision_ready else "BLOCKED_DATA",
        "decision_ready": decision_ready,
        "allow_trade_amounts": False,
        "message": (
            "数据门禁通过，仍须人工复核；页面不执行交易。"
            if decision_ready
            else "数据复核中，禁止生成交易金额。"
        ),
        "database_market_as_of": health.get("latest_market_date"),
        "portfolio_as_of": market_as_of,
        "valuation_mode": valuation_mode,
        "blockers": list(
            dict.fromkeys(
                [
                    *[str(item) for item in health.get("blockers", [])],
                    *blockers,
                    *[
                        str(item)
                        for item in long_term_strategy.get("blockers", [])
                    ],
                    *(
                        []
                        if governance_ready
                        else [
                            f"STRATEGY_GOVERNANCE_{governance.get('stage', 'UNAVAILABLE')}",
                            *[
                                str(item)
                                for item in governance.get("blockers", [])
                            ],
                        ]
                    ),
                ]
            )
        ),
        "warnings": [str(item) for item in health.get("warnings", [])],
    }

    response = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_asset": round(total_asset, 2),
            "invested": round(
                market_total
                if valuation_mode == "VALIDATED_DAILY_UPDATE"
                else ledger_invested,
                2,
            ),
            "cash": round(cash, 2),
            "cash_weight": round(max(0.0, 1 - explicit_weight_total), 6),
            "holdings_count": len(holdings),
            "currency": "CNY",
            "valuation_mode": valuation_mode,
        },
        "risk": {
            "level": risk_level,
            "headline": headline,
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "block_new_technology_risk": bool(
                tech and tech["weight"] > tech["limit"]
            ),
            "technology_weight": tech["weight"] if tech else 0.0,
            "technology_limit": tech["limit"] if tech else risk_policy.max_sector_weight,
        },
        "allocation": allocation,
        "sectors": concentration["sectors"],
        "sector_views": {
            "direct": concentration.get("direct_sectors", []),
            "lookthrough": concentration.get("lookthrough_sectors", []),
            "lookthrough_coverage": concentration.get("lookthrough_coverage", 0),
        },
        "risk_explanations": risk_explanations,
        "top_positions": top_positions,
        "limits": {
            "single_position": risk_policy.max_single_position,
            "sector": risk_policy.max_sector_weight,
            "unknown_sector": risk_policy.max_unknown_sector_weight,
        },
        "drawdown": drawdown,
        "performance": performance,
        "long_term_strategy": long_term_strategy,
        "advice_lint": advice_lint,
        "ips": ips,
        "investor_profile": profile_assessment,
        "strategy_governance": governance,
        "runtime_automation": runtime_automation or {
            "status": "UNAVAILABLE",
            "tasks": [],
            "automatic_trade_allowed": False,
            "trusted_review_scheduled": False,
            "ai_review_scheduled": False,
        },
        "decision_gate": decision_gate,
        "data_quality": {
            **data_quality,
            "blockers": blockers,
            "unknown_weight": round(unknown_weight, 6),
            "holding_statuses": [
                {
                    "symbol": item["symbol"],
                    "freshness": item.get("freshness", "VALUATION_MISSING"),
                    "price_date": item.get("price_date"),
                    "source": item.get("price_source"),
                }
                for item in holdings
            ],
        },
        "calculation_metrics": calculation_metrics,
        "disclaimer": (
            "本页面用于资产风险管理与决策支持，不承诺收益，也不构成自动交易指令。"
        ),
    }
    LOGGER.info(
        json.dumps(
            {"event": "dashboard_calculated", **calculation_metrics},
            ensure_ascii=False,
        )
    )
    return response
