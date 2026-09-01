"""
daily_run.py — Phase L: Daily Automation Pipeline.

Runs every day:
1. Fetch market data (akshare)
2. Update portfolio state with market values
3. Compute risk metrics
4. Generate daily report
5. AI analysis (when API key configured)

Usage:
    python -m portfolio_manager.daily_run
    # Or via cron:
    # 0 18 * * * cd /path/to/foundf && python -m portfolio_manager.daily_run
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPORTS = Path("reports/reconciliation")
DATA = Path("data")
DATA.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ── Step 1: Fetch Market Data ──────────────────────

def step_fetch_prices() -> dict[str, float]:
    """Fetch current prices for all holdings."""
    from .market_data import MarketDataFetcher, is_hk_symbol
    from .state_engine import PortfolioStateEngine
    
    log("Step 1: Fetching market prices...")
    
    engine = PortfolioStateEngine()
    v = engine.verify_against_broker()
    snap = engine.snapshot(v["snapshot_date"])
    
    holdings = snap["stocks"]
    symbols = []
    for h in holdings:
        market = "HK" if is_hk_symbol(h["symbol"]) else "CN"
        symbols.append({"symbol": h["symbol"], "market": market})
    
    fetcher = MarketDataFetcher(use_cache=True, intraday=True)
    prices = fetcher.fetch_prices(symbols)
    
    found = sum(1 for s in symbols if prices.get(s["symbol"], 0) > 0)
    log(f"  Prices: {found}/{len(symbols)} holdings priced")
    return prices


# ── Step 2: Compute Portfolio ──────────────────────

def _trusted_performance() -> dict[str, Any]:
    """Read performance only from validated cash-flow-adjusted unit NAV."""

    from .nav_tracker import load_trusted_nav_history
    from .portfolio_performance import calculate_performance

    snapshots = load_trusted_nav_history(DATA / "portfolio_nav_history.json")
    return calculate_performance(snapshots)


def _validated_daily_portfolio() -> dict[str, Any] | None:
    """Use the externally reconciled multi-currency snapshot when available."""
    from .daily_position_contract import validate_daily_position_update

    path = DATA / "daily_position_update.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    validation = validate_daily_position_update(payload)
    if not validation["valid"]:
        log(
            "  ⚠️ Daily position update rejected: "
            + ", ".join(validation["errors"][:5])
        )
        return None

    holdings = []
    total_cost = 0.0
    for row in payload["positions"]:
        value = float(row["market_value_cny"])
        raw_cost = row.get("total_cost_cny")
        cost = float(raw_cost) if raw_cost is not None else None
        pnl = value - cost if cost is not None else None
        pnl_rate = pnl / cost * 100 if cost and pnl is not None else None
        if cost is not None:
            total_cost += cost
        holdings.append(
            {
                "symbol": str(row["symbol"]),
                "name": str(row.get("name", "")),
                "shares": float(row["shares"]),
                "cost": cost or 0.0,
                "price": float(row["close_price_native"])
                * float(row["fx_to_cny"]),
                "native_price": float(row["close_price_native"]),
                "currency": str(row["currency"]),
                "fx_to_cny": float(row["fx_to_cny"]),
                "market_value": round(value, 2),
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_rate": round(pnl_rate, 2) if pnl_rate is not None else None,
                "priced": True,
                "valuation_source": "VALIDATED_DAILY_UPDATE",
            }
        )
    total_market = float(payload["totals"]["positions_market_value_cny"])
    cash = float(payload["totals"]["cash_cny"])
    total_asset = float(payload["totals"]["total_asset_cny"])
    return {
        "date": str(payload["as_of"]),
        "cash": round(cash, 2),
        "total_cost": round(total_cost, 2),
        "total_market": round(total_market, 2),
        "total_asset_market": round(total_asset, 2),
        "total_asset_book": round(
            float(payload["totals"]["broker_total_asset_cny"]), 2
        ),
        "holdings_count": len(holdings),
        "priced_holdings_count": len(holdings),
        "unpriced_holdings_count": 0,
        "unpriced_cost_reference": 0.0,
        "valuation_coverage_pct": 100.0,
        "holdings": sorted(
            holdings,
            key=lambda item: (
                item["pnl_rate"] is not None,
                item["pnl_rate"] or 0,
            ),
            reverse=True,
        ),
        "performance": _trusted_performance(),
        "valuation_ready": True,
        "allow_trade_amounts": False,
        "unrealized_pnl": round(
            sum(item["pnl"] or 0 for item in holdings), 2
        ),
        "unrealized_pnl_scope": "positions_with_cost_only",
        "valuation_source": "VALIDATED_DAILY_UPDATE",
    }


def step_portfolio(prices: dict[str, float]) -> dict[str, Any]:
    """Compute portfolio with market values."""
    from .state_engine import PortfolioStateEngine
    
    log("Step 2: Computing portfolio state...")
    
    engine = PortfolioStateEngine()
    v = engine.verify_against_broker()
    snap = engine.snapshot(v["snapshot_date"])

    daily_portfolio = _validated_daily_portfolio()
    if daily_portfolio is not None:
        log("  Using validated multi-currency daily position update")
        return daily_portfolio
    
    holdings = []
    total_market = 0.0
    total_cost = 0.0
    priced_cost = 0.0
    unpriced_cost = 0.0
    priced_holdings = 0
    
    for h in snap["stocks"]:
        sym = h["symbol"]
        price = prices.get(sym, 0.0)
        shares = h["shares"]
        cost = h["total_cost"]
        is_foreign_currency = len(sym) == 5 and sym.startswith("0")
        is_priced = price > 0 and not is_foreign_currency
        market_value = round(price * shares, 2) if is_priced else 0.0
        pnl = round(market_value - cost, 2) if is_priced else None
        pnl_rate = (
            round(pnl / cost * 100, 2)
            if is_priced and cost > 0 and pnl is not None
            else None
        )
        
        total_market += market_value
        total_cost += cost
        if is_priced:
            priced_holdings += 1
            priced_cost += cost
        else:
            unpriced_cost += cost
        
        holdings.append({
            "symbol": sym, "name": h["name"], "shares": shares,
            "cost": cost, "price": price,
            "market_value": market_value,
            "pnl": pnl, "pnl_rate": pnl_rate,
            "priced": is_priced,
            "currency": "HKD" if is_foreign_currency else "CNY",
            "valuation_blocker": (
                "FX_TO_CNY_MISSING" if is_foreign_currency else None
            ),
        })
    
    total_asset = round(v["cash"] + total_market, 2)
    coverage_denominator = total_asset + unpriced_cost
    valuation_coverage = (
        total_asset / coverage_denominator if coverage_denominator > 0 else 0.0
    )
    
    return {
        "date": v["snapshot_date"],
        "cash": v["cash"],
        "total_cost": round(total_cost, 2),
        "total_market": round(total_market, 2),
        "total_asset_market": total_asset,
        "total_asset_book": v["total_asset"],
        "holdings_count": len(holdings),
        "priced_holdings_count": priced_holdings,
        "unpriced_holdings_count": len(holdings) - priced_holdings,
        "unpriced_cost_reference": round(unpriced_cost, 2),
        "valuation_coverage_pct": round(valuation_coverage * 100, 1),
        "holdings": sorted(
            holdings,
            key=lambda x: (x["pnl_rate"] is not None, x["pnl_rate"] or 0),
            reverse=True,
        ),
        "performance": _trusted_performance(),
        "valuation_source": "INCOMPLETE_LEGACY_PRICE_CACHE",
        "valuation_ready": False,
        "allow_trade_amounts": False,
        "unrealized_pnl": round(total_market - priced_cost, 2),
        "unrealized_pnl_scope": "priced_holdings_only",
    }


# ── Step 3: Compute Risk and Allocation ────────────

def step_risk(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Compute risk metrics."""
    from .market_data import is_hk_symbol

    log("Step 3: Computing risk metrics...")
    
    holdings = portfolio["holdings"]
    
    # Concentration
    if holdings:
        top3 = sorted(holdings, key=lambda x: x["market_value"], reverse=True)[:3]
        total_asset = float(portfolio.get("total_asset_market", 0) or 0)
        top3_weight = (
            sum(h["market_value"] for h in top3) / total_asset * 100
            if total_asset > 0
            else 0.0
        )
    else:
        top3 = []
        top3_weight = 0.0
    
    # Market exposure
    cn_market = sum(h["market_value"] for h in holdings if not is_hk_symbol(h["symbol"]))
    hk_market = sum(h["market_value"] for h in holdings if is_hk_symbol(h["symbol"]))
    total_market = portfolio["total_market"]
    
    # Winners/losers
    winners = [h for h in holdings if h["pnl"] is not None and h["pnl"] > 0]
    losers = [h for h in holdings if h["pnl"] is not None and h["pnl"] < 0]
    
    # Largest single position
    largest = max(holdings, key=lambda x: x["market_value"]) if holdings else {}
    
    return {
        "data_ready": bool(portfolio.get("valuation_ready")),
        "weight_source": (
            "VALIDATED_CNY_MARKET_VALUE"
            if portfolio.get("valuation_ready")
            else "PARTIAL_PRICE_REFERENCE"
        ),
        "holdings_count": len(holdings),
        "top3_concentration_pct": round(top3_weight, 1),
        "top3_holdings": [{"symbol": h["symbol"], "name": h["name"], 
                          "weight": (
                              round(
                                  h["market_value"]
                                  / portfolio["total_asset_market"]
                                  * 100,
                                  1,
                              )
                              if portfolio["total_asset_market"] > 0
                              else 0.0
                          )}
                         for h in top3],
        "cn_exposure_pct": round(cn_market / total_market * 100, 1) if total_market > 0 else 0,
        "hk_exposure_pct": round(hk_market / total_market * 100, 1) if total_market > 0 else 0,
        "cash_pct": (
            round(
                portfolio["cash"] / portfolio["total_asset_market"] * 100,
                1,
            )
            if portfolio["total_asset_market"] > 0
            else 0.0
        ),
        "winners": len(winners),
        "losers": len(losers),
        "largest_position": largest.get("symbol", ""),
        "largest_position_weight": (
            round(
                largest.get("market_value", 0)
                / portfolio["total_asset_market"]
                * 100,
                1,
            )
            if largest and portfolio["total_asset_market"] > 0
            else 0
        ),
        "total_profit": round(sum(h["pnl"] for h in winners), 2),
        "total_loss": round(sum(h["pnl"] for h in losers), 2),
    }


def step_allocation(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Assess strategic allocation with data-quality and profile guardrails."""
    from .asset_allocation import (
        AllocationPolicy,
        AssetAllocationEngine,
        assess_portfolio_allocation,
    )
    from .investor_profile import load_profile_assessment

    log("Step 3b: Assessing strategic asset allocation...")
    profile = load_profile_assessment(
        ".secrets/investor_profile.json",
        portfolio.get("total_asset_market", 0),
    )
    if profile["activation_ready"]:
        base_policy = AllocationPolicy.load()
        proposed = dict(profile["proposed_allocation_policy"])
        proposed["symbol_overrides"] = base_policy.symbol_overrides or {}
        result = AssetAllocationEngine(
            AllocationPolicy.from_mapping(proposed)
        ).assess(
            portfolio.get("holdings", []),
            float(portfolio.get("cash", 0) or 0),
        )
    else:
        result = assess_portfolio_allocation(portfolio)
    result["investor_profile"] = {
        "activation_ready": profile["activation_ready"],
        "profile_level": profile["profile_level"],
        "review_due": profile["review_due"],
        "errors": profile["errors"],
    }

    # Append-only history preserves the inputs and blockers behind every daily
    # allocation decision. Repeated intraday runs remain separately auditable.
    history_path = DATA / "asset_allocation_history.jsonl"
    snapshot = {
        "as_of": portfolio.get("date", date.today().isoformat()),
        "recorded_at": datetime.now().isoformat(),
        **result,
    }
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return result


def step_concentration(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Aggregate cross-wrapper sector exposure and enforce concentration caps."""
    from .concentration_guard import ConcentrationGuard, ConcentrationPolicy

    log("Step 3c: Assessing position and sector concentration...")
    policy = ConcentrationPolicy.load()
    result = ConcentrationGuard(policy).assess(
        portfolio.get("holdings", []),
        portfolio.get("cash", 0),
    )
    history_path = DATA / "concentration_risk_history.jsonl"
    snapshot = {
        "as_of": portfolio.get("date", date.today().isoformat()),
        "recorded_at": datetime.now().isoformat(),
        **result,
    }
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return result


def step_nav(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Persist a cash-flow-adjusted unit NAV only from reliable valuations."""
    from .nav_tracker import PortfolioNAVTracker

    log("Step 3d: Recording cash-flow-adjusted unit NAV...")
    return PortfolioNAVTracker().record(
        as_of=portfolio["date"],
        total_asset=portfolio["total_asset_market"],
        valuation_coverage=portfolio.get("valuation_coverage_pct", 0) / 100,
        priced_holdings=portfolio.get("priced_holdings_count", 0),
        total_holdings=portfolio.get("holdings_count", 0),
    )


def step_long_term_strategy(
    portfolio: dict[str, Any],
    allocation: dict[str, Any],
    concentration: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the low-turnover strategy state and persist its evidence."""
    from .concentration_guard import ConcentrationPolicy, DrawdownGuard
    from .long_term_strategy import (
        LongTermStrategyEngine,
        LongTermStrategyPolicy,
        calculate_trailing_turnover,
    )
    from .nav_tracker import load_trusted_nav_history

    log("Step 3e: Evaluating long-term strategy governance...")
    market_dates = []
    for name in ("cn_prices.json", "hk_prices.json"):
        path = DATA / "market_cache" / name
        if not path.exists():
            continue
        try:
            market_dates.append(
                date.fromisoformat(
                    str(json.loads(path.read_text(encoding="utf-8"))["_date"])
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    market_date = min(market_dates).isoformat() if market_dates else None

    nav_path = DATA / "portfolio_nav_history.json"
    nav_rows = load_trusted_nav_history(nav_path)
    if len(nav_rows) >= 2:
        drawdown = DrawdownGuard(ConcentrationPolicy.load()).assess(
            [row["unit_nav"] for row in nav_rows]
        )
        drawdown.update({"data_ready": True, "observations": len(nav_rows)})
    else:
        drawdown = {
            "data_ready": False,
            "observations": len(nav_rows),
            "allow_new_risk": False,
        }

    turnover = calculate_trailing_turnover(
        REPORTS / "broker_economic_event_v4.csv",
        portfolio.get("total_asset_market", 0),
        market_date or date.today(),
    )
    result = LongTermStrategyEngine(LongTermStrategyPolicy.load()).evaluate(
        as_of=date.today(),
        market_data_date=market_date,
        allocation=allocation,
        concentration=concentration,
        turnover=turnover,
        drawdown=drawdown,
    )
    history_path = DATA / "long_term_strategy_history.jsonl"
    snapshot = {
        "as_of": date.today().isoformat(),
        "recorded_at": datetime.now().isoformat(),
        **result,
    }
    with open(history_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return result


def step_data_asset_health() -> dict[str, Any]:
    """Persist the unified fail-closed data-asset gate for this run."""

    from foundf_db.health import inspect_data_assets

    log("Step 3f: Inspecting unified data-asset health...")
    result = inspect_data_assets(data_root=DATA)
    status_path = DATA / "governance" / "data_asset_health_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = status_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(status_path)
    return result


def step_strategy_evolution() -> dict[str, Any]:
    """Update the canonical strategy gate without changing production weights."""

    from strategy_manager.daily_evaluation import load_baseline
    from strategy_manager.evolution import run_current_governance

    log("Step 3g: Updating canonical strategy-evolution evidence gates...")
    baseline = load_baseline("config/multifactor_baseline.json")
    result = run_current_governance(
        policy_path="config/strategy_evolution.json",
        factor_report_path="reports/factor_research/factor_research.json",
        walk_forward_dir="strategy_report",
        candidate_config={"factor_weights": baseline["factor_weights"]},
        lifecycle_path=DATA / "governance" / "factor_lifecycle.json",
        status_path=DATA / "governance" / "strategy_evolution_status.json",
    )
    if (
        result.get("production_change_allowed") is not False
        or result.get("human_approval_required") is not True
    ):
        raise ValueError("strategy evolution safety invariant failed")
    return result


# ── Step 4: Generate Report ────────────────────────

def step_report(
    portfolio: dict[str, Any],
    risk: dict[str, Any],
    fundamentals: dict | None = None,
    allocation: dict[str, Any] | None = None,
    concentration: dict[str, Any] | None = None,
    long_term_strategy: dict[str, Any] | None = None,
    data_asset_health: dict[str, Any] | None = None,
    strategy_evolution: dict[str, Any] | None = None,
) -> str:
    """Generate daily summary report with fundamentals."""
    log("Step 4: Generating report...")
    
    today = date.today().isoformat()
    p = portfolio
    r = risk
    performance = p.get(
        "performance", {"status": "DATA_BUILDING", "observations": 0}
    )
    valuation_ready = p.get("valuation_ready") is True
    asset_label = (
        "Total Asset (validated CNY market value)"
        if valuation_ready
        else "Partial priced asset reference (not NAV)"
    )
    
    # Load fundamentals if not provided or empty
    if not fundamentals:
        fund_path = REPORTS / "fundamental_data.csv"
        if fund_path.exists():
            fundamentals = {}
            with open(fund_path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sym = row.get("symbol", "")
                    if not sym:
                        continue
                    entry = {}
                    for k, v in row.items():
                        if k in ("symbol", "source", "fetched_at", "report_period"):
                            entry[k] = v
                        else:
                            try:
                                entry[k] = float(v) if v and v != "" else 0.0
                            except ValueError:
                                entry[k] = 0.0
                    fundamentals[sym] = entry
    
    lines = [
        f"# FoundF Daily Report — {today}",
        f"",
        f"## Summary",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| {asset_label} | {p['total_asset_market']:,.2f} |",
        f"| Book Value | {p['total_asset_book']:,.2f} |",
        f"| Cash | {p['cash']:,.2f} |",
        f"| Unrealized P&L | {p['unrealized_pnl']:+,.2f} |",
        f"| Holdings | {p['holdings_count']} |",
        f"| Valuation Coverage | {p.get('valuation_coverage_pct', 0):.1f}% |",
        f"| Performance Evidence | {performance['status']} |",
        f"| Unit NAV Observations | {performance.get('observations', 0)} |",
        f"| Trade Amounts Allowed | false |",
        f"",
        f"## Risk",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Top 3 Concentration | {r['top3_concentration_pct']}% |",
        f"| Cash % | {r['cash_pct']}% |",
        f"| CN/HK Split | {r['cn_exposure_pct']}% / {r['hk_exposure_pct']}% |",
        f"| Winners/Losers | {r['winners']}/{r['losers']} |",
    ]
    if performance.get("status") == "READY":
        lines.extend([
            f"| Unit NAV TWR | {performance['total_return']:.2%} |",
            f"| Max Drawdown | {performance['max_drawdown']:.2%} |",
        ])
    else:
        lines.append(
            "| Return Metrics | Hidden until at least two trusted unit-NAV snapshots |"
        )
    if not valuation_ready:
        lines.append(
            "| Valuation Decision | BLOCKED — incomplete or unconverted prices |"
        )
    
    for h in r["top3_holdings"]:
        lines.append(f"| Top holding: {h['symbol']} {h['name']} | {h['weight']}% |")

    if p.get("unpriced_holdings_count", 0):
        lines.extend([
            "",
            "### Data Quality Guard",
            f"- {p['unpriced_holdings_count']} holdings are unpriced; "
            f"cost reference {p['unpriced_cost_reference']:,.2f}.",
            "- Total Asset (market) contains cash and priced holdings only. "
            "Do not use it as a complete net-worth figure.",
        ])

    if allocation:
        lines.extend([
            "",
            "## Strategic Asset Allocation",
            f"- Policy: `{allocation['policy_id']}` "
            f"(version {allocation['policy_version']})",
            f"- Decision status: "
            f"{'ACTIONABLE' if allocation['actionable'] else 'BLOCKED — data/profile review required'}",
            f"- Valuation coverage: {allocation['valuation_coverage']:.1%}",
            f"- Unknown economic exposure: {allocation['unknown_exposure_weight']:.1%}",
        ])
        if allocation["blockers"]:
            lines.append("- Blockers: " + ", ".join(allocation["blockers"]))
        lines.extend([
            "",
            "| Economic exposure | Current | Target | Allowed band | Drift | Status | Action |",
            "|---|---:|---:|---:|---:|---|---|",
        ])
        for row in allocation["economic_allocation"]:
            lines.append(
                f"| {row['asset_class']} | {row['current_weight']:.1%} | "
                f"{row['target_weight']:.1%} | "
                f"{row['lower_bound']:.1%}–{row['upper_bound']:.1%} | "
                f"{row['drift']:+.1%} | {row['status']} | {row['action']} |"
            )
        lines.extend([
            "",
            "### Instrument mix",
            "| Instrument | Weight | Value |",
            "|---|---:|---:|",
        ])
        for row in allocation["instrument_allocation"]:
            lines.append(
                f"| {row['instrument_type']} | {row['weight']:.1%} | "
                f"{row['value']:,.2f} |"
            )
        for warning in allocation["warnings"]:
            lines.append(f"- ⚠️ {warning}")
        lines.append(f"- {allocation['disclaimer']}")

    if concentration:
        lines.extend([
            "",
            "## Concentration Guard",
            f"- Weight source: {concentration['weight_source']}",
            f"- Hard limit: "
            f"{'BREACHED — no additional risk in breached buckets' if concentration['hard_limit_breached'] else 'OK'}",
            "",
            "| Sector | Weight | Limit | Status |",
            "|---|---:|---:|---|",
        ])
        for row in concentration["sectors"]:
            lines.append(
                f"| {row['sector']} | {row['weight']:.1%} | "
                f"{row['limit']:.1%} | {row['status']} |"
            )
        critical = [
            alert for alert in concentration["alerts"]
            if alert["severity"] == "CRITICAL"
        ]
        for alert in critical:
            if alert["type"] == "WEIGHT_DATA_INCOMPLETE":
                lines.append(
                    f"- ⛔ WEIGHT_DATA_INCOMPLETE: "
                    f"{alert.get('unweighted_positions', 0)} holdings lack usable weights"
                )
            else:
                lines.append(
                    f"- ⛔ {alert['type']} `{alert['key']}`: "
                    f"{alert['weight']:.1%} > {alert['limit']:.1%}"
                )

    if long_term_strategy:
        turnover = long_term_strategy.get("trailing_turnover", {})
        lines.extend([
            "",
            "## Long-Term Strategy Governance",
            f"- State: `{long_term_strategy['state']}`",
            f"- Execution ready: {long_term_strategy['execution_ready']}",
            f"- Allow new risk: {long_term_strategy['allow_new_risk']}",
            f"- Review due: {long_term_strategy['review_due']}",
        ])
        if turnover.get("available"):
            lines.append(
                f"- Trailing 12-month turnover proxy: "
                f"{turnover['turnover']:.1%} "
                f"({turnover['trade_count']} audited trades; "
                f"method `{turnover['method']}`)"
            )
        if long_term_strategy["blockers"]:
            lines.append(
                "- Blockers: " + ", ".join(long_term_strategy["blockers"])
            )
        for action in long_term_strategy["actions"]:
            lines.append(
                f"- [{action['severity']}] **{action['title']}** — "
                f"{action['detail']}"
            )
        lines.append(f"- {long_term_strategy['disclaimer']}")

    if data_asset_health:
        lines.extend([
            "",
            "## Unified Data-Asset Gate",
            f"- Status: `{data_asset_health.get('status', 'CRITICAL')}`",
            f"- Decision data ready: "
            f"{data_asset_health.get('decision_data_ready') is True}",
            f"- Database market date: "
            f"{data_asset_health.get('latest_market_date') or 'missing'}",
        ])
        if data_asset_health.get("blockers"):
            lines.append(
                "- Blockers: "
                + ", ".join(str(item) for item in data_asset_health["blockers"])
            )

    if strategy_evolution:
        lines.extend([
            "",
            "## Canonical Strategy-Evolution Gate",
            f"- Stage: `{strategy_evolution.get('stage', 'BLOCKED_DATA')}`",
            "- Production weight change: forbidden",
            "- Automatic trading: forbidden",
            f"- Human approval required: "
            f"{strategy_evolution.get('human_approval_required') is True}",
            f"- Data gate: "
            f"{strategy_evolution.get('data_gate', {}).get('passed') is True}",
            f"- Factor gate: "
            f"{strategy_evolution.get('factor_gate', {}).get('passed') is True}",
            f"- Walk-Forward gate: "
            f"{strategy_evolution.get('backtest_gate', {}).get('passed') is True}",
            f"- Forward research observation: "
            f"{strategy_evolution.get('paper_gate', {}).get('days', 0)}/"
            f"{strategy_evolution.get('paper_gate', {}).get('minimum_days', 90)} days",
            "- Forward research observation is not Guosen simulation execution.",
        ])
    
    lines.extend([
        f"",
        f"## Holdings Detail",
        f"| Symbol | Name | Price | Mkt Val | P&L% | ROE | PE | Rev(亿) |",
        f"|--------|------|-------|---------|------|-----|----|---------|",
    ])
    
    for h in p["holdings"]:
        sym = h["symbol"]
        mkt = f"{h['market_value']:,.0f}" if h['market_value'] > 0 else "-"
        if h["pnl_rate"] is None:
            pnl = "N/A"
        else:
            pnl = f"{h['pnl_rate']:+.1f}%" if h["pnl_rate"] != 0 else "0%"
        fd = fundamentals.get(sym, {}) if fundamentals else {}
        roe = f"{fd.get('roe', 0)*100:.1f}%" if fd.get('roe', 0) else "-"
        pe = f"{fd.get('pe', 0):.1f}" if fd.get('pe', 0) else "-"
        rev = f"{fd.get('revenue', 0)/1e8:.1f}" if fd.get('revenue', 0) else "-"
        lines.append(f"| {sym} | {h['name'][:12]} | {h['price']:.2f} | {mkt} | {pnl} | {roe} | {pe} | {rev} |")
    
    lines.append("")
    
    # Fundamental flags
    if fundamentals:
        lines.append("## Quality Flags (Fundamental)")
        flags = []
        for h in p["holdings"]:
            sym = h["symbol"]
            fd = fundamentals.get(sym, {})
            if fd:
                roe = fd.get("roe", 0)
                pe = fd.get("pe", 0)
                rev_growth = fd.get("revenue_growth", 0)
                debt = fd.get("debt_ratio", 0)
                if roe < -0.02:
                    hname = h["name"][:10]
                    flags.append(f"  ⚠️  {sym} {hname}: ROE={roe*100:.1f}% (negative)")
                if pe < 0:
                    flags.append(f"  ⚠️  {sym}: PE={pe:.1f} (negative earnings)")
                if debt > 0.7:
                    hname = h["name"][:10]
                    flags.append(f"  ⚠️  {sym} {hname}: debt_ratio={debt*100:.0f}% (high leverage)")
                if roe > 0.15:
                    hname = h["name"][:10]
                    flags.append(f"  ✅ {sym} {hname}: ROE={roe*100:.1f}% (strong)")
        if flags:
            lines.extend(flags)
        else:
            lines.append("  No quality flags for current holdings.")
        lines.append("")
    
    # AI Assessment
    lines.append("## AI Assessment")
    try:
        from .asset_quality import assess_portfolio_quality
        quality = assess_portfolio_quality(portfolio)
        lines.append(f"  Overall: {quality.get('assessment', 'Data insufficient')}")
        for rec in quality.get("recommendations", []):
            lines.append(f"  💡 {rec}")
    except Exception:
        lines.append("  (AI assessment module not available)")
    lines.append("")
    
    lines.extend([
        f"---",
        f"_Generated by FoundF Daily Automation_",
        f"_Valuation source: {p.get('valuation_source', 'INCOMPLETE_OR_LEDGER_REFERENCE')} | "
        "Missing or stale prices are never labelled real-time_",
    ])
    
    report = "\n".join(lines)
    
    report_path = REPORTS / f"daily_report_{today}.md"
    report_path.write_text(report, encoding="utf-8")
    log(f"  Report saved: {report_path.name}")
    
    return report


# ── Step 5: Fetch Fundamentals (weekly) ─────────────

def step_fundamentals() -> dict:
    """Fetch fundamental data (weekly). Skips if already done today."""
    from .fundamental_engine import FundamentalEngine
    from .market_data import is_hk_symbol
    from .state_engine import PortfolioStateEngine
    
    # Check if already fetched today
    path = REPORTS / "fundamental_data.csv"
    if path.exists():
        from datetime import date as dt
        mtime = path.stat().st_mtime
        import time
        days_old = (time.time() - mtime) / 86400
        if days_old < 7:  # refresh weekly
            log("  Step 5: Fundamentals up-to-date (skipping)")
            return {}
    
    log("Step 5: Fetching fundamentals...")
    engine = PortfolioStateEngine()
    v = engine.verify_against_broker()
    snap = engine.snapshot(v["snapshot_date"])
    
    symbols = []
    for h in snap.get("stocks", []):
        market = "HK" if is_hk_symbol(h["symbol"]) else "CN"
        symbols.append({"symbol": h["symbol"], "market": market})
    
    fe = FundamentalEngine()
    data = fe.fetch_all(symbols)
    log(f"  Fundamentals: {len(data)} stocks updated")
    return data


# ── Data Growth Tracker ──────────────────────────────

def track_data_growth() -> None:
    """Track daily data accumulation in DuckDB."""
    from datetime import date as dt
    log("  Tracking data growth...")
    
    growth_path = DATA / "growth_tracker.csv"
    today = dt.today().isoformat()
    
    # Check DuckDB row counts
    try:
        import duckdb
        db = DATA / "finance.duckdb"
        if db.exists():
            con = duckdb.connect(str(db))
            tables = con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
            
            row_counts = {}
            for t in tables:
                name = t[0]
                if name in ("daily_price", "financial_statement", "minute_price"):
                    try:
                        cnt = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                        row_counts[name] = cnt
                    except Exception:
                        pass
            con.close()
            
            # Write to tracker
            import csv
            from pathlib import Path
            file_exists = growth_path.exists()
            with open(growth_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not file_exists:
                    w.writerow(["date", "daily_price_rows", "financial_statement_rows", "minute_price_rows"])
                w.writerow([
                    today,
                    row_counts.get("daily_price", 0),
                    row_counts.get("financial_statement", 0),
                    row_counts.get("minute_price", 0),
                ])
            log(f"  Growth: daily_price={row_counts.get('daily_price', 0):,} rows")
    except Exception as e:
        log(f"  Growth tracking skipped: {e}")


# ── Main ───────────────────────────────────────────

# ── Pipeline Job Log ──────────────────────────────

PIPELINE_LOG = DATA / "pipeline_job_log.csv"


def init_pipeline_log() -> None:
    """Initialize pipeline job log if not exists."""
    if not PIPELINE_LOG.exists():
        with open(PIPELINE_LOG, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "step", "status", "duration_s", "holdings_priced",
                        "total_asset", "error"])


def log_pipeline_step(step: str, status: str, duration_s: float = 0,
                       holdings_priced: int = 0, total_asset: float = 0,
                       error: str = "") -> None:
    """Log a pipeline step result."""
    init_pipeline_log()
    with open(PIPELINE_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            date.today().isoformat(),
            step, status, round(duration_s, 1),
            holdings_priced, round(total_asset, 2),
            error[:200] if error else "",
        ])


def run_step(name: str, fn, *args, **kwargs) -> tuple[Any, str]:
    """Run a pipeline step with logging.
    
    Returns: (result, status) where status is "ok" or "failed"
    """
    import time
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - t0
        log_pipeline_step(name, "ok", elapsed)
        return result, "ok"
    except Exception as e:
        elapsed = time.time() - t0
        err_msg = f"{type(e).__name__}: {e}"
        log(f"  ❌ {name} failed: {err_msg[:80]}")
        log_pipeline_step(name, "failed", elapsed, error=err_msg)
        return None, "failed"


# ── Main ───────────────────────────────────────────

def run() -> dict[str, Any]:
    """Run the full daily pipeline with reliability."""
    log("=== FoundF Daily Automation Pipeline ===")
    log(f"Date: {date.today().isoformat()}")
    init_pipeline_log()
    
    import time
    t_start = time.time()
    
    # Step 1: Prices (required)
    prices, s1 = run_step("fetch_prices", step_fetch_prices)
    holdings_priced = sum(1 for v in (prices or {}).values() if v > 0) if prices else 0
    
    # Step 2: Portfolio (requires prices but degrades)
    portfolio, s2 = run_step("portfolio", step_portfolio, prices or {})
    
    # Step 3: Risk (requires portfolio)
    risk, s3 = run_step("risk", step_risk, portfolio or {})

    # Step 3b: Strategic allocation (requires portfolio)
    allocation, s3b = run_step("asset_allocation", step_allocation, portfolio or {})

    # Step 3c: Cross-wrapper concentration (requires portfolio)
    concentration, s3c = run_step(
        "concentration_guard", step_concentration, portfolio or {}
    )

    # Step 3d: flow-adjusted NAV (strictly rejects incomplete valuations)
    nav_result, s3d = run_step("portfolio_nav", step_nav, portfolio or {})

    # Step 3e: long-term strategy state (never invents price predictions)
    long_term_strategy, s3e = run_step(
        "long_term_strategy",
        step_long_term_strategy,
        portfolio or {},
        allocation or {},
        concentration or {},
    )

    # Step 3f: unified data health (read-only inspection + atomic status)
    data_asset_health, s3f = run_step(
        "data_asset_health", step_data_asset_health
    )

    # Step 3g: canonical strategy evidence gates; never changes production.
    strategy_evolution, s3g = run_step(
        "strategy_evolution", step_strategy_evolution
    )
    
    # Step 5: Fundamentals (independent)
    fundamentals, s5 = run_step("fundamentals", step_fundamentals)
    
    # Step 4: Report (always tries)
    try:
        step_report(
            portfolio or {},
            risk or {},
            fundamentals,
            allocation,
            concentration,
            long_term_strategy,
            data_asset_health,
            strategy_evolution,
        )
        log_pipeline_step("report", "ok")
    except Exception as e:
        log(f"  ❌ Report failed: {e}")
        # Minimal fallback
        fallback = [
            f"# FoundF Daily Report — {date.today().isoformat()}",
            f"",
            f"## Status: Partial Failure",
            f"| Step | Status |",
            f"|------|--------|",
            f"| Prices | {s1} |",
            f"| Portfolio | {s2} |",
            f"| Risk | {s3} |",
            f"| Asset Allocation | {s3b} |",
            f"| Concentration Guard | {s3c} |",
            f"| Portfolio NAV | {s3d} |",
            f"| Long-Term Strategy | {s3e} |",
            f"| Data Asset Health | {s3f} |",
            f"| Strategy Evolution | {s3g} |",
            f"| Fundamentals | {s5} |",
            f"| Report | ❌ |",
        ]
        path = REPORTS / f"daily_report_{date.today().isoformat()}.md"
        path.write_text("\n".join(fallback), encoding="utf-8")
        log_pipeline_step("report", "failed", error=str(e))
    
    # Save run log
    run_log = {
        "date": date.today().isoformat(),
        "time": datetime.now().isoformat(),
        "holdings_priced": holdings_priced,
        "total_holdings": portfolio.get("holdings_count", 0) if portfolio else 0,
        "total_asset": portfolio.get("total_asset_market", 0) if portfolio else 0,
        "cash": portfolio.get("cash", 0) if portfolio else 0,
        "unrealized_pnl": portfolio.get("unrealized_pnl", 0) if portfolio else 0,
        "steps": {
            "prices": s1,
            "portfolio": s2,
            "risk": s3,
            "asset_allocation": s3b,
            "concentration_guard": s3c,
            "portfolio_nav": s3d,
            "long_term_strategy": s3e,
            "data_asset_health": s3f,
            "strategy_evolution": s3g,
            "fundamentals": s5,
        },
    }
    log_path = DATA / "daily_run_log.json"
    runs = []
    if log_path.exists():
        runs = json.loads(log_path.read_text(encoding="utf-8"))
    runs.append(run_log)
    log_path.write_text(json.dumps(runs[-30:], ensure_ascii=False, indent=2), encoding="utf-8")
    
    # Track data growth
    track_data_growth()
    
    # Re-populate financial_statement from fresh data
    try:
        from foundf_db.migration.populate_financial_statement import run as populate_fin
        populate_fin()
    except Exception:
        pass
    
    total_time = time.time() - t_start
    log(f"=== Pipeline Complete in {total_time:.0f}s ===")
    log(f"Holdings Priced: {holdings_priced}/20")
    if portfolio:
        log(f"Total Asset: {portfolio.get('total_asset_market', 0):,.2f}")
    
    return portfolio or {}


if __name__ == "__main__":
    run()
