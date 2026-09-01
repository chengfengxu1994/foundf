"""
investment_agent — AI 投资分析 Agent。

流程：数据库 → Factor Engine → Risk Engine → News Analysis → 投资报告

注意：AI 不能直接执行交易，只能输出分析/风险/建议。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import Warehouse
from factor_engine import FactorRegistry
from portfolio_ai import PortfolioAnalyzer
from risk_engine import RiskEngine
from foundf_db import DataProvider


class InvestmentAgent:
    """投资分析 Agent。

    使用方式:
        agent = InvestmentAgent("data/finance.duckdb")
        report = agent.analyze()
        agent.save_report(report)
    """

    def __init__(self, duckdb_path: str | Path = "data/finance.duckdb",
                 report_dir: str | Path = "reports"):
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        self.dp = DataProvider(warehouse=self.warehouse)
        self.portfolio_analyzer = PortfolioAnalyzer(self.dp)
        self.risk_engine = RiskEngine(self.dp)
        self.factor_registry = FactorRegistry(self.warehouse)
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def analyze(self) -> dict[str, Any]:
        """执行综合分析。"""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # 1. 持仓分析
        portfolio = self.portfolio_analyzer.analyze_all()

        # 2. 风险评估
        positions = self.dp.portfolio_positions()
        risk = self.risk_engine.assess_portfolio(positions)

        # 3. 因子评分
        factor_scores = {}
        for pos in positions:
            scores = self.factor_registry.compute_all(pos["symbol"])
            if scores:
                factor_scores[pos["symbol"]] = scores

        # 4. 综合建议（AI 不能直接交易）
        suggestions = []
        if risk.risk_level in ("high", "extreme"):
            suggestions.append("⚠ 风险等级过高，建议评估是否需要减仓")
        if portfolio.concentration_risk == "high":
            suggestions.append("⚠ 持仓集中度过高，建议分散化")
        if factor_scores:
            for sym, scores in factor_scores.items():
                avg = sum(scores.values()) / max(len(scores), 1)
                if avg < 0.3:
                    suggestions.append(f"💡 {sym}: 因子综合评分偏低 ({avg:.2f})，建议关注")
                elif avg > 0.7:
                    suggestions.append(f"⭐ {sym}: 因子综合评分优秀 ({avg:.2f})")

        return {
            "generated_at": now.isoformat(),
            "date": today,
            "portfolio": {
                "total_positions": portfolio.total_positions,
                "top_holdings": portfolio.top_holdings,
                "weighted_scores": {
                    "trend": portfolio.weighted_trend,
                    "valuation": portfolio.weighted_valuation,
                    "growth": portfolio.weighted_growth,
                    "risk": portfolio.weighted_risk,
                },
            },
            "risk": {
                "level": risk.risk_level,
                "score": risk.market_risk,
                "warnings": risk.warnings,
            },
            "suggestions": suggestions,
        }

    def to_markdown(self, report: dict[str, Any]) -> str:
        date = report.get("date", "unknown")
        lines = [
            f"# AI 投资分析报告 — {date}",
            f"",
            f"## 持仓概览",
            f"- 持仓: {report['portfolio']['total_positions']} 只",
            f"- 风险等级: {report['risk']['level'].upper()}",
            f"- 风险评分: {report['risk']['score']:.0f}/100",
            f"",
            f"### 加权评分",
        ]
        for dim, score in report["portfolio"]["weighted_scores"].items():
            lines.append(f"- {dim}: {score:.0f}/100")
        lines.extend(["", "### 风险预警"])
        for w in report["risk"]["warnings"]:
            lines.append(f"- ⚠ {w}")
        if report["suggestions"]:
            lines.extend(["", "### 投资建议"])
            for s in report["suggestions"]:
                lines.append(f"- {s}")
        lines.extend(["", "---", "*此报告由 AI 自动生成，仅供参考，不构成投资建议。*"])
        return "\n".join(lines)

    def save_report(self, report: dict[str, Any]) -> Path:
        md = self.to_markdown(report)
        date = report.get("date", datetime.now().strftime("%Y-%m-%d"))
        path = self.report_dir / f"ai_report_{date}.md"
        path.write_text(md, encoding="utf-8")
        return path
