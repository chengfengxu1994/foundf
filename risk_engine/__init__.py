"""
risk_engine — AI 风险控制引擎。

重点不是预测涨，而是避免大亏。

输入：
    新闻事件 + 公告 + 市场数据 + 估值数据
输出：
    每只持仓股票的 Risk Level (0-100)
    整体市场风险等级
    特定风险原因列表

风险维度：
    1. 估值风险 — 价格处于历史区间高位
    2. 波动风险 — 近期波动率异常
    3. 趋势风险 — 价格跌破关键均线
    4. 新闻风险 — 负面新闻/公告
    5. 集中度风险 — 组合过度集中于少数标的
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from foundf_db import DataProvider


@dataclass
class RiskReport:
    """单个标的的风险报告。"""
    symbol: str
    name: str
    total_risk: float       # 0-100
    valuation_risk: float   # 0-100
    volatility_risk: float
    trend_risk: float
    news_risk: float
    concentration_boost: float  # 集中度加乘
    reasons: list[str]
    data_as_of: str


@dataclass
class MarketRiskReport:
    """整体市场风险报告。"""
    market_risk: float          # 0-100
    risk_level: str             # 'low', 'medium', 'high', 'extreme'
    top_risks: list[RiskReport]
    warnings: list[str]


# 估值分位数阈值（基于价格/60日均线）
_VALUATION_BANDS = [
    (2.0, 100, "估值远超历史均值"),
    (1.5, 85, "估值偏高"),
    (1.3, 70, "估值高于均值"),
    (1.1, 50, "估值合理偏高"),
    (0.9, 30, "估值合理"),
    (0.8, 20, "估值偏低"),
    (0.0, 10, "估值显著偏低"),
]

# 负面关键词列表
_NEGATIVE_KEYWORDS = [
    "减持", "亏损", "违约", "处罚", "立案", "监管", "风险警示",
    "退市", "st", "*st", "停牌", "暂停上市", "破产", "清算",
]


class RiskEngine:
    """风险控制引擎。

    使用方式:
        engine = RiskEngine(dp)
        report = engine.assess_stock("600519")
        portfolio_risk = engine.assess_portfolio()
    """

    def __init__(self, dp: DataProvider):
        self.dp = dp

    def assess_stock(
        self, symbol: str, name: str = "", market: str = "",
    ) -> RiskReport:
        """评估单只股票的风险。"""
        bars = self.dp.daily_bars([symbol])
        if not bars:
            return RiskReport(
                symbol=symbol, name=name or symbol, total_risk=50,
                valuation_risk=50, volatility_risk=50, trend_risk=50,
                news_risk=0, concentration_boost=0,
                reasons=["数据不足"],
                data_as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )

        closes = np.array([b["close"] for b in bars], dtype=float)

        # 各维度风险
        valuation_risk = self._valuation_risk(closes)
        volatility_risk = self._volatility_risk(closes)
        trend_risk = self._trend_risk(closes)
        news_risk = self._news_risk(symbol)

        reasons: list[str] = []

        # 估值风险原因
        if valuation_risk >= 85:
            reasons.append("估值显著偏高")
        elif valuation_risk >= 70:
            reasons.append("估值偏高，价格处于历史高位")
        elif valuation_risk >= 50:
            pass  # 正常范围不提示

        # 波动风险原因
        if volatility_risk >= 70:
            reasons.append("近期波动率异常偏高")
        elif volatility_risk >= 50:
            reasons.append("波动率偏高")

        # 趋势风险原因
        if trend_risk >= 70:
            reasons.append("价格跌破60日均线，趋势偏弱")
        elif trend_risk >= 50:
            reasons.append("价格接近60日均线，方向不明")

        # 新闻风险
        if news_risk >= 30:
            reasons.append("近期存在负面新闻事件")

        # 综合风险（加权）
        total_risk = (
            valuation_risk * 0.25 +
            volatility_risk * 0.20 +
            trend_risk * 0.25 +
            news_risk * 0.30
        )
        total_risk = min(100, max(0, total_risk))

        return RiskReport(
            symbol=symbol, name=name or symbol,
            total_risk=round(total_risk, 1),
            valuation_risk=round(valuation_risk, 1),
            volatility_risk=round(volatility_risk, 1),
            trend_risk=round(trend_risk, 1),
            news_risk=round(news_risk, 1),
            concentration_boost=0,
            reasons=reasons,
            data_as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    def assess_portfolio(
        self, positions: list[dict[str, Any]] | None = None,
    ) -> MarketRiskReport:
        """评估投资组合的整体风险。"""
        if positions is None:
            positions = self.dp.portfolio_positions()

        if not positions:
            return MarketRiskReport(
                market_risk=0, risk_level="low",
                top_risks=[], warnings=["暂无持仓"],
            )

        # 评估每只持仓
        reports = []
        for p in positions:
            report = self.assess_stock(
                p["symbol"], p.get("name", ""), p.get("market", ""),
            )
            # 计算集中度加乘
            total_value = sum(
                pos.get("shares", 0) * (pos.get("current_price") or 0)
                for pos in positions
            ) or 1
            weight = (
                p.get("shares", 0) * (p.get("current_price") or 0) / total_value
            )
            if weight > 0.3:
                report.concentration_boost = min(20, (weight - 0.3) * 50)
                report.total_risk = min(100, report.total_risk + report.concentration_boost)
            reports.append(report)

        # 组合风险 = 加权平均 + 集中度惩罚
        total_value = sum(
            pos.get("shares", 0) * (pos.get("current_price") or 0)
            for pos in positions
        ) or 1
        weighted_risk = 0.0
        top1_weight = 0.0
        for i, p in enumerate(positions):
            weight = p.get("shares", 0) * (p.get("current_price") or 0) / total_value
            weighted_risk += reports[i].total_risk * weight
            top1_weight = max(top1_weight, weight)

        # 集中度惩罚
        concentration_penalty = max(0, (top1_weight - 0.3) * 30)
        market_risk = min(100, weighted_risk + concentration_penalty)

        # 风险等级
        if market_risk >= 70:
            risk_level = "extreme"
        elif market_risk >= 50:
            risk_level = "high"
        elif market_risk >= 30:
            risk_level = "medium"
        else:
            risk_level = "low"

        # Top risks
        top_risks = sorted(reports, key=lambda r: r.total_risk, reverse=True)[:5]

        # Warnings
        warnings = []
        if risk_level in ("high", "extreme"):
            warnings.append(f"组合风险等级: {risk_level}，建议减仓")
        if top1_weight > 0.3:
            warnings.append(f"单标集中度{top1_weight:.1%}，超过30%警戒线")
        high_risk_stocks = [r for r in reports if r.total_risk >= 60]
        if high_risk_stocks:
            names = ", ".join(f"{r.name}({r.total_risk:.0f})" for r in high_risk_stocks[:3])
            warnings.append(f"高风险标的: {names}")

        return MarketRiskReport(
            market_risk=round(market_risk, 1),
            risk_level=risk_level,
            top_risks=top_risks,
            warnings=warnings,
        )

    def _valuation_risk(self, closes: np.ndarray) -> float:
        """估值风险：价格相对60日均线的位置。"""
        if len(closes) < 60:
            return 50.0
        ma60 = np.mean(closes[-60:])
        ratio = closes[-1] / ma60
        for band_ratio, risk_score, _ in _VALUATION_BANDS:
            if ratio >= band_ratio:
                return float(risk_score)
        return 10.0

    def _volatility_risk(self, closes: np.ndarray) -> float:
        """波动风险：近期波动率 vs 长期波动率。"""
        if len(closes) < 63:
            return 50.0
        returns = np.diff(closes) / closes[:-1]
        short_vol = float(np.std(returns[-20:]) * np.sqrt(252))
        long_vol = float(np.std(returns[-63:]) * np.sqrt(252))
        if long_vol < 0.01:
            return 20.0
        ratio = short_vol / long_vol
        if ratio > 2.0:
            return 85.0
        elif ratio > 1.5:
            return 65.0
        elif ratio > 1.2:
            return 50.0
        else:
            return 20.0 + ratio * 15

    def _trend_risk(self, closes: np.ndarray) -> float:
        """趋势风险：价格相对均线位置 + 短期走势。"""
        if len(closes) < 60:
            return 50.0
        last = closes[-1]
        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:])

        if last < ma20 and last < ma60:
            # 空头排列
            ret_5d = closes[-1] / closes[-5] - 1 if len(closes) >= 5 else 0
            base = 70
            if ret_5d < -0.03:  # 加速下跌
                base = 90
            return float(base)
        elif last > ma20 and last < ma60:
            return 55.0  # 反弹中
        elif last > ma20 and last > ma60:
            return 20.0  # 多头排列
        else:
            return 40.0

    def _news_risk(self, symbol: str) -> float:
        """新闻风险：检查近期是否有负面事件。"""
        # 从 news_event 表查询
        events = self.dp.news_events(limit=50)
        if not events:
            return 0.0

        # 检查关联标的的负面新闻
        negative_count = 0
        total_relevant = 0
        for event in events:
            event_symbol = event.get("symbol", "")
            if event_symbol and event_symbol != symbol:
                continue
            total_relevant += 1
            title = (event.get("title") or "").lower()
            content = (event.get("content") or "").lower()
            combined = title + " " + content
            for kw in _NEGATIVE_KEYWORDS:
                if kw.lower() in combined:
                    negative_count += 1
                    break

        if total_relevant == 0:
            return 0.0
        ratio = negative_count / total_relevant
        # 完全负面 = 80分, 无负面 = 0分
        return float(min(80, ratio * 100))

    @staticmethod
    def format_report(report: MarketRiskReport) -> str:
        """生成可读的风险报告。"""
        lines = [
            "## 风险控制报告",
            f"",
            f"**组合风险等级: {report.risk_level.upper()}** (风险评估: {report.market_risk:.0f}/100)",
            f"",
        ]
        if report.warnings:
            lines.append("### ⚠ 预警")
            for w in report.warnings:
                lines.append(f"- {w}")
            lines.append("")

        lines.append("### 高风险标的")
        for r in report.top_risks[:3]:
            if r.total_risk >= 50:
                lines.append(f"- **{r.name}** ({r.symbol}): 风险 {r.total_risk:.0f}")
                for reason in r.reasons:
                    lines.append(f"  - {reason}")

        return "\n".join(lines)
