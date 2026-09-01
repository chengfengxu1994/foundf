"""Trusted, fail-closed daily portfolio review.

This public entry point never refreshes legacy market values, computes
Simple Return/XIRR, calls an LLM, changes strategy weights, or emits trade
amounts.  It projects only validated daily positions and shared governance
evidence into a structured report for human review.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db.health import inspect_data_assets
from foundf_db.runtime_scheduler import load_runtime_status
from portfolio_manager.asset_allocation import (
    AllocationPolicy,
    AssetAllocationEngine,
)
from portfolio_manager.concentration_guard import (
    ConcentrationGuard,
    ConcentrationPolicy,
)
from portfolio_manager.daily_position_contract import (
    validate_daily_position_update,
)
from portfolio_manager.investor_profile import load_profile_assessment
from portfolio_manager.nav_tracker import load_trusted_nav_history
from portfolio_manager.portfolio_performance import calculate_performance
from strategy_manager.governance_status import load_governance_status


SCHEMA_VERSION = "foundf.trusted_daily_review.v1"


def _load_position_input(
    path: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not path.exists():
        return None, {
            "valid": False,
            "errors": ["DAILY_POSITION_UPDATE_MISSING"],
            "warnings": [],
            "as_of": None,
            "positions_count": 0,
            "priced_positions_count": 0,
            "valuation_coverage": 0.0,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {
            "valid": False,
            "errors": ["DAILY_POSITION_UPDATE_INVALID_JSON"],
            "warnings": [],
            "as_of": None,
            "positions_count": 0,
            "priced_positions_count": 0,
            "valuation_coverage": 0.0,
        }
    if not isinstance(payload, dict):
        return None, {
            "valid": False,
            "errors": ["DAILY_POSITION_UPDATE_INVALID_TYPE"],
            "warnings": [],
            "as_of": None,
            "positions_count": 0,
            "priced_positions_count": 0,
            "valuation_coverage": 0.0,
        }
    validation = validate_daily_position_update(payload)
    return (payload if validation["valid"] else None), validation


def _trusted_holdings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    holdings = []
    for row in payload["positions"]:
        market_value = float(row["market_value_cny"])
        total_cost_raw = row.get("total_cost_cny")
        total_cost = (
            float(total_cost_raw) if total_cost_raw is not None else None
        )
        holdings.append(
            {
                "symbol": str(row["symbol"]),
                "name": str(row.get("name") or row["symbol"]),
                "shares": float(row["shares"]),
                "market_value": market_value,
                "total_cost": total_cost,
                "currency": str(row["currency"]),
                "price_date": str(row["price_date"]),
                "price_source": str(row["price_source"]),
                "fx_to_cny": float(row["fx_to_cny"]),
                "fx_source": str(row["fx_source"]),
                "freshness": str(row.get("freshness", "T_CLOSE")),
                "instrument_status": str(
                    row.get("instrument_status", "ACTIVE")
                ),
                "underlying_holdings": row.get("underlying_holdings", []),
                "lookthrough_source": str(
                    row.get("lookthrough_source", "")
                ),
                "lookthrough_as_of": str(row.get("lookthrough_as_of", "")),
            }
        )
    return holdings


def _health_projection(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": raw.get("status", "CRITICAL"),
        "decision_data_ready": raw.get("decision_data_ready") is True,
        "latest_market_date": (
            raw.get("latest_market_date").isoformat()
            if hasattr(raw.get("latest_market_date"), "isoformat")
            else raw.get("latest_market_date")
        ),
        "blockers": list(raw.get("blockers", [])),
        "warnings": list(raw.get("warnings", [])),
        "coverage": raw.get("coverage", {}),
    }


class DailyPortfolioIntelligence:
    """Build deterministic evidence for a future human-supervised AI review."""

    def __init__(
        self,
        duckdb_path: str | Path = "data/finance.duckdb",
        report_dir: str | Path = "reports",
        *,
        data_root: str | Path | None = None,
        config_root: str | Path = "config",
        investor_profile_path: str | Path = ".secrets/investor_profile.json",
    ):
        self.duckdb_path = Path(duckdb_path)
        self.data_root = Path(data_root or self.duckdb_path.parent)
        self.report_dir = Path(report_dir)
        self.config_root = Path(config_root)
        self.investor_profile_path = Path(investor_profile_path)

    def generate(self) -> dict[str, Any]:
        """Generate a structured review without external calls or suggestions."""

        generated_at = datetime.now(timezone.utc)
        payload, position_validation = _load_position_input(
            self.data_root / "daily_position_update.json"
        )
        health = _health_projection(
            inspect_data_assets(
                data_root=self.data_root,
                db_path=self.duckdb_path,
            )
        )
        governance = load_governance_status(
            self.data_root / "governance" / "strategy_evolution_status.json"
        )
        runtime = load_runtime_status(data_root=self.data_root)
        nav_rows = load_trusted_nav_history(
            self.data_root / "portfolio_nav_history.json"
        )
        performance = calculate_performance(nav_rows)

        holdings: list[dict[str, Any]] = []
        portfolio: dict[str, Any] | None = None
        allocation: dict[str, Any] | None = None
        concentration: dict[str, Any] | None = None
        total_asset = 0.0
        if payload is not None:
            holdings = _trusted_holdings(payload)
            totals = payload["totals"]
            total_asset = float(totals["total_asset_cny"])
            cash = float(totals["cash_cny"])
            portfolio = {
                "as_of": str(payload["as_of"]),
                "base_currency": "CNY",
                "total_asset_cny": total_asset,
                "positions_market_value_cny": float(
                    totals["positions_market_value_cny"]
                ),
                "cash_cny": cash,
                "broker_total_asset_cny": float(
                    totals["broker_total_asset_cny"]
                ),
                "reconciliation_difference_cny": float(
                    totals["reconciliation_difference_cny"]
                ),
                "positions_count": len(holdings),
                "holdings": holdings,
            }
            allocation = AssetAllocationEngine(
                AllocationPolicy.load(
                    self.config_root / "asset_allocation.json"
                )
            ).assess(holdings, cash)
            concentration = ConcentrationGuard(
                ConcentrationPolicy.load(
                    self.config_root / "portfolio_risk_limits.json"
                )
            ).assess(holdings, cash)

        profile = load_profile_assessment(
            self.investor_profile_path, total_asset
        )
        input_blockers = list(position_validation.get("errors", []))
        blockers = list(
            dict.fromkeys(
                input_blockers
                + list(health["blockers"])
                + (
                    ["TRUSTED_NAV_HISTORY_INSUFFICIENT"]
                    if performance.get("status") != "READY"
                    else []
                )
                + (
                    ["INVESTOR_PROFILE_NOT_ACTIVATED"]
                    if not profile["activation_ready"]
                    else []
                )
            )
        )

        risk_review_eligible = (
            payload is not None and health["decision_data_ready"]
        )
        personalized_review_eligible = (
            risk_review_eligible
            and performance.get("status") == "READY"
            and profile["activation_ready"]
            and governance.get("evidence_ready") is True
        )
        if not risk_review_eligible:
            status = "BLOCKED_DATA"
        elif not personalized_review_eligible:
            status = "DATA_BUILDING"
        else:
            status = "READY_FOR_HUMAN_REVIEW"

        return {
            "schema_version": SCHEMA_VERSION,
            "report_date": generated_at.date().isoformat(),
            "generated_at": generated_at.isoformat(),
            "status": status,
            "portfolio_input": position_validation,
            "portfolio": portfolio,
            "data_assets": health,
            "performance": performance,
            "allocation": allocation,
            "concentration": concentration,
            "investor_profile": {
                "activation_ready": profile["activation_ready"],
                "profile_level": profile["profile_level"],
                "review_due": profile["review_due"],
                "errors": profile["errors"],
            },
            "strategy_governance": governance,
            "runtime_automation": runtime,
            "review_gate": {
                "risk_review_eligible": risk_review_eligible,
                "personalized_review_eligible": personalized_review_eligible,
                "blockers": blockers,
            },
            "ai_execution": {
                "llm_called": False,
                "llm_call_allowed": False,
                "reason": "TRUSTED_PROMPT_AND_PRIVACY_APPROVAL_NOT_ESTABLISHED",
            },
            "recommendations": [],
            "decision_boundary": {
                "allow_trade_amounts": False,
                "automatic_trade_allowed": False,
                "production_strategy_change_allowed": False,
                "human_review_required": True,
            },
            "disclaimer": (
                "本报告只整理可审计数据与门禁，不构成投资建议，"
                "不授权生成交易金额、修改生产策略或提交订单。"
            ),
        }

    def to_markdown(self, report: dict[str, Any]) -> str:
        gate = report["review_gate"]
        position = report["portfolio_input"]
        assets = report["data_assets"]
        governance = report["strategy_governance"]
        performance = report["performance"]
        lines = [
            f"# FoundF 可信每日复盘 — {report['report_date']}",
            "",
            f"- 状态：`{report['status']}`",
            f"- 组合输入：{'READY' if position['valid'] else 'BLOCKED'}",
            f"- 数据资产：`{assets['status']}`",
            f"- 策略治理：`{governance.get('stage', 'BLOCKED_DATA')}`",
            "- LLM 调用：false",
            "- 自动交易：false",
            "- 交易金额：禁止生成",
            "",
            "## 复盘门禁",
            f"- 风险复盘可用：{gate['risk_review_eligible']}",
            f"- 个性化复盘可用：{gate['personalized_review_eligible']}",
        ]
        if gate["blockers"]:
            lines.append("- 阻断项：" + "、".join(gate["blockers"]))
        lines.extend(
            [
                "",
                "## 数据证据",
                f"- 数据库行情截至：{assets.get('latest_market_date') or '缺失'}",
                f"- 已校验持仓数：{position.get('priced_positions_count', 0)}/"
                f"{position.get('positions_count', 0)}",
                f"- 估值覆盖率：{position.get('valuation_coverage', 0):.1%}",
                f"- 可信单位净值状态：`{performance.get('status', 'DATA_BUILDING')}`",
                f"- 可信单位净值观测：{performance.get('observations', 0)}",
            ]
        )
        portfolio = report.get("portfolio")
        if portfolio is not None:
            lines.extend(
                [
                    "",
                    "## 已校验组合快照",
                    f"- 组合日期：{portfolio['as_of']}",
                    f"- 总资产（CNY）：{portfolio['total_asset_cny']:,.2f}",
                    f"- 持仓市值（CNY）："
                    f"{portfolio['positions_market_value_cny']:,.2f}",
                    f"- 现金（CNY）：{portfolio['cash_cny']:,.2f}",
                    f"- 券商核对差额（CNY）："
                    f"{portfolio['reconciliation_difference_cny']:+,.2f}",
                ]
            )
        lines.extend(
            [
                "",
                "## 建议与执行",
                "- 当前不生成投资建议。",
                "- 当前不生成交易金额。",
                "- 当前不调用外部 LLM。",
                "",
                f"> {report['disclaimer']}",
            ]
        )
        return "\n".join(lines) + "\n"

    def save(self, report: dict[str, Any]) -> Path:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        stem = f"portfolio_{report['report_date']}"
        json_path = self.report_dir / f"{stem}.json"
        markdown_path = self.report_dir / f"{stem}.md"
        json_tmp = json_path.with_suffix(".json.tmp")
        markdown_tmp = markdown_path.with_suffix(".md.tmp")
        json_tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        markdown_tmp.write_text(
            self.to_markdown(report),
            encoding="utf-8",
        )
        os.replace(json_tmp, json_path)
        os.replace(markdown_tmp, markdown_path)
        return markdown_path


def main() -> None:
    parser = argparse.ArgumentParser(description="FoundF 可信每日复盘")
    parser.add_argument("--data-root", default=os.getenv("FOUNDF_DATA_ROOT", "data"))
    parser.add_argument("--duckdb-path", default=os.getenv("DUCKDB_PATH"))
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--config-root", default="config")
    parser.add_argument(
        "--investor-profile",
        default=".secrets/investor_profile.json",
    )
    args = parser.parse_args()
    data_root = Path(args.data_root)
    intelligence = DailyPortfolioIntelligence(
        duckdb_path=args.duckdb_path or data_root / "finance.duckdb",
        report_dir=args.report_dir,
        data_root=data_root,
        config_root=args.config_root,
        investor_profile_path=args.investor_profile,
    )
    report = intelligence.generate()
    path = intelligence.save(report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_path": str(path),
                "blockers": report["review_gate"]["blockers"],
                "llm_called": False,
                "automatic_trade_allowed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
