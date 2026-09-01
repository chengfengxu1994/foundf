"""
daily_review — 每日自动复盘系统。

每天收盘 16:00 执行：
    1. 获取今日行情
    2. 更新数据库
    3. 重新计算因子
    4. 分析持仓
    5. 分析新闻
    6. 生成 Markdown 报告
    7. 保存到 reports/YYYY-MM-DD.md

输出包含：
    - 今日市场总结
    - 我的持仓变化
    - 风险提醒
    - 策略变化
    - 明日观察
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import DataProvider
from portfolio_ai import PortfolioAnalyzer
from risk_engine import RiskEngine


class DailyReview:
    """每日复盘系统。

    使用方式:
        review = DailyReview(dp)
        report = review.generate()
        review.save(report)
    """

    def __init__(
        self,
        dp: DataProvider,
        report_dir: str | Path = "reports",
    ):
        self.dp = dp
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.portfolio_analyzer = PortfolioAnalyzer(dp)
        self.risk_engine = RiskEngine(dp)

    def generate(self) -> dict[str, Any]:
        """生成今日复盘数据。"""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # 1. 市场概览
        market_summary = self._market_summary()

        # 2. 持仓分析
        portfolio_analysis = self.portfolio_analyzer.analyze_all()

        # 3. 风险报告
        positions = self.dp.portfolio_positions()
        risk_report = self.risk_engine.assess_portfolio(positions)

        # 4. 策略信号
        strategy_signal = self._strategy_signal()

        return {
            "generated_at": now.isoformat(),
            "date": today,
            "market_summary": market_summary,
            "portfolio_analysis": {
                "total_positions": portfolio_analysis.total_positions,
                "invested_ratio": portfolio_analysis.invested_ratio,
                "top_holdings": portfolio_analysis.top_holdings,
                "weighted_trend": portfolio_analysis.weighted_trend,
                "weighted_growth": portfolio_analysis.weighted_growth,
                "weighted_valuation": portfolio_analysis.weighted_valuation,
                "weighted_risk": portfolio_analysis.weighted_risk,
                "concentration_risk": portfolio_analysis.concentration_risk,
                "suggestions": portfolio_analysis.suggestions,
            },
            "risk_report": {
                "market_risk": risk_report.market_risk,
                "risk_level": risk_report.risk_level,
                "warnings": risk_report.warnings,
                "top_risks": [
                    {"symbol": r.symbol, "name": r.name, "risk": r.total_risk, "reasons": r.reasons}
                    for r in risk_report.top_risks[:3]
                ],
            },
            "strategy_signal": strategy_signal,
        }

    def _market_summary(self) -> dict[str, Any]:
        """生成市场概览。"""
        # 获取基准指数最新数据
        benchmarks = {
            "CSI300": "沪深300",
            "HSI": "恒生指数",
            "SP500": "标普500",
            "NASDAQ": "纳斯达克",
        }
        # 从 daily_price 获取
        summary = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        for sym, name in benchmarks.items():
            bars = self.dp.daily_bars([sym])
            if bars:
                last = bars[-1]
                prev = bars[-2] if len(bars) >= 2 else None
                pct = (last["close"] / prev["close"] - 1) * 100 if prev else 0
                summary[sym] = {
                    "name": name,
                    "close": last["close"],
                    "change_pct": round(pct, 2),
                }
        return summary

    def _strategy_signal(self) -> dict[str, Any] | None:
        """获取最新策略信号。"""
        try:
            from quant_strategy import generate, FactorEngine
            all_symbols = self._get_all_symbols()
            if all_symbols:
                from quant_strategy import FactorConfig
                result = generate(self.dp, all_symbols)
                weights = result.get("weights", {})
                invested = {k: v for k, v in weights.items() if k != "CASH" and v > 0}
                return {
                    "model": "multifactor_v3",
                    "total_weights": {k: round(v, 4) for k, v in sorted(weights.items(), key=lambda x: -x[1]) if v > 0},
                    "selected_count": len(invested),
                    "cash_ratio": weights.get("CASH", 1.0),
                }
        except ImportError:
            return None
        return None

    def _get_all_symbols(self) -> list[str]:
        """获取所有可交易标的列表。"""
        return [r["code"] for r in self.dp.stock_basic()]

    def to_markdown(self, report: dict[str, Any]) -> str:
        """转换为 Markdown 报告。"""
        date = report["date"]
        lines = [
            f"# FoundF 每日复盘 — {date}",
            f"",
            f"_生成时间: {report['generated_at']}_",
            f"",
            f"---",
            f"",
            f"## 一、今日市场总结",
            f"",
        ]
        for sym, data in report.get("market_summary", {}).items():
            if isinstance(data, dict) and "change_pct" in data:
                arrow = "📈" if data["change_pct"] >= 0 else "📉"
                lines.append(f"- {arrow} **{data['name']}**: {data['close']:.2f} ({data['change_pct']:+.2f}%)")

        pa = report.get("portfolio_analysis", {})
        lines.extend([
            f"",
            f"---",
            f"",
            f"## 二、持仓分析",
            f"",
            f"- 持仓数量: {pa.get('total_positions', 0)} 只",
            f"- 仓位比例: {pa.get('invested_ratio', 0):.1%}",
            f"",
            f"### 前5大持仓",
        ])
        for h in pa.get("top_holdings", []):
            lines.append(f"- {h['name']}（{h['symbol']}）: {h['weight']:.1f}% — {h.get('value', 0):.2f}")

        lines.extend([
            f"",
            f"### 加权评分",
            f"- 趋势: {pa.get('weighted_trend', 0):.0f}/100",
            f"- 估值: {pa.get('weighted_valuation', 0):.0f}/100",
            f"- 成长: {pa.get('weighted_growth', 0):.0f}/100",
            f"- 风险: {pa.get('weighted_risk', 0):.0f}/100（越低越好）",
        ])

        if pa.get("suggestions"):
            lines.extend(["", "### 优化建议"])
            for s in pa["suggestions"]:
                lines.append(f"- 💡 {s}")

        rr = report.get("risk_report", {})
        risk_level = rr.get("risk_level", "unknown")
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🟠", "extreme": "🔴"}
        lines.extend([
            f"",
            f"---",
            f"",
            f"## 三、风险提醒",
            f"",
            f"- 组合风险: {risk_icon.get(risk_level, '⚪')} **{risk_level.upper()}** ({rr.get('market_risk', 0):.0f}/100)",
        ])
        for w in rr.get("warnings", []):
            lines.append(f"- ⚠ {w}")

        for risk_item in rr.get("top_risks", []):
            if risk_item.get("risk", 0) >= 50:
                lines.append(f"")
                lines.append(f"  **{risk_item['name']}** ({risk_item['symbol']}): 风险 {risk_item['risk']:.0f}")
                for reason in risk_item.get("reasons", []):
                    lines.append(f"  - {reason}")

        ss = report.get("strategy_signal")
        if ss:
            lines.extend([
                f"",
                f"---",
                f"",
                f"## 四、策略信号",
                f"",
                f"- 模型: {ss.get('model', 'N/A')}",
                f"- 选中标的: {ss.get('selected_count', 0)} 只",
                f"- 现金比例: {ss.get('cash_ratio', 0):.1%}",
            ])
            lines.append("")
            lines.append("### 目标权重")
            for sym, wgt in ss.get("total_weights", {}).items():
                if sym != "CASH":
                    lines.append(f"- {sym}: {wgt:.1%}")
            if "CASH" in ss.get("total_weights", {}):
                lines.append(f"- 现金: {ss['total_weights']['CASH']:.1%}")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 五、明日观察",
            f"",
            f"- 待生成",
            f"",
            f"---",
            f"_此报告由 FoundF 自动生成，仅供参考，不构成投资建议。_",
        ])
        return "\n".join(lines)

    def save(self, report: dict[str, Any]) -> Path:
        """保存 Markdown 报告。"""
        md = self.to_markdown(report)
        date = report["date"]
        path = self.report_dir / f"{date}.md"
        path.write_text(md, encoding="utf-8")
        return path

    def generate_and_save(self) -> Path:
        """生成并保存今日复盘报告。"""
        report = self.generate()
        return self.save(report)
