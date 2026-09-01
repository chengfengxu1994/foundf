"""
portfolio_ai — 投资组合智能分析模块。

每天读取：
    我的持仓 + 市场数据 + 新闻 + 财务数据 + 因子评分
输出：
    单股票分析（趋势/估值/成长/风险星级 + 综合评分）
    整体组合分析（风险集中度/相关性/调仓建议）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from foundf_db import DataProvider


@dataclass
class StockAnalysis:
    """单股票分析结果。"""
    symbol: str
    name: str
    market: str
    trend_score: float          # 0-100
    trend_stars: str            # ★★★★★
    valuation_score: float      # 0-100
    valuation_stars: str
    growth_score: float         # 0-100
    growth_stars: str
    risk_score: float           # 0-100 (越低越安全)
    risk_stars: str             # ★ 表示低风险
    total_score: float          # 0-100 综合
    suggestion: str             # 建议
    risks: list[str]            # 风险列表
    data_as_of: str


@dataclass
class PortfolioAnalysis:
    """投资组合整体分析。"""
    total_positions: int
    invested_ratio: float
    top_holdings: list[dict[str, Any]]
    sector_exposure: dict[str, float]
    market_exposure: dict[str, float]
    weighted_trend: float
    weighted_valuation: float
    weighted_growth: float
    weighted_risk: float
    concentration_risk: str          # 'high', 'medium', 'low'
    correlation_risk: str
    suggestions: list[str]


class PortfolioAnalyzer:
    """投资组合分析器。

    使用方式:
        dp = DataProvider.dual()
        analyzer = PortfolioAnalyzer(dp)
        result = analyzer.analyze_all()
    """

    # 评分权重配置
    TREND_WEIGHTS = {
        "ma20_vs_ma60": 0.35,      # 短期均线与长期均线位置
        "momentum_3m": 0.30,        # 3个月收益
        "momentum_6m": 0.20,        # 6个月收益
        "recent_5d": 0.15,          # 近5日走势
    }
    VALUATION_METHODS = {
        "PE": 0.35,
        "PB": 0.25,
        "FCF_YIELD": 0.25,
        "PRICE_VS_MA60": 0.15,     # 价格相对60日均线位置（估值参考）
    }
    GROWTH_WEIGHTS = {
        "revenue_growth": 0.35,     # 收入增长
        "profit_growth": 0.35,      # 利润增长
        "momentum_12m": 0.30,       # 12个月收益（成长代理）
    }
    RISK_WEIGHTS = {
        "volatility_3m": 0.30,
        "max_drawdown_6m": 0.25,
        "downside_vol": 0.25,
        "price_vs_ma60": 0.20,      # 跌破60日均线 = 高风险信号
    }

    def __init__(self, dp: DataProvider):
        self.dp = dp
        self._data_as_of: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── 公开API ──────────────────────────────────────────

    def analyze_all(self) -> PortfolioAnalysis:
        """分析所有持仓，返回整体分析报告。"""
        positions = self.dp.portfolio_positions()
        if not positions:
            return PortfolioAnalysis(
                total_positions=0, invested_ratio=0.0,
                top_holdings=[], sector_exposure={}, market_exposure={},
                weighted_trend=0, weighted_valuation=0, weighted_growth=0, weighted_risk=0,
                concentration_risk="low", correlation_risk="low", suggestions=["暂无持仓"],
            )

        # 逐股票分析
        individual: list[StockAnalysis] = []
        for pos in positions:
            try:
                analysis = self.analyze_stock(pos)
                individual.append(analysis)
            except Exception:
                continue

        # 聚合分析
        return self._aggregate(positions, individual)

    def analyze_stock(self, position: dict[str, Any]) -> StockAnalysis:
        """分析单股票。"""
        symbol = position["symbol"]
        name = position.get("name", symbol)
        market = position.get("market", "")

        # 获取日线数据
        bars = self.dp.daily_bars([symbol], end_date=self._data_as_of)
        if not bars:
            return StockAnalysis(
                symbol=symbol, name=name, market=market,
                trend_score=50, trend_stars="★★★",
                valuation_score=50, valuation_stars="★★★",
                growth_score=50, growth_stars="★★★",
                risk_score=50, risk_stars="★★★",
                total_score=50, suggestion="数据不足",
                risks=["历史数据不足，无法完整分析"],
                data_as_of=self._data_as_of,
            )

        closes = np.array([b["close"] for b in bars], dtype=float)
        dates = [b["date"] for b in bars]
        volumes = np.array([b.get("volume", 0) or 0 for b in bars], dtype=float)

        # 计算各维度分数
        trend_score = self._score_trend(closes)
        valuation_score = self._score_valuation(closes)
        growth_score = self._score_growth(closes)
        risk_score = self._score_risk(closes)

        # 综合评分（风险分越低越好，所以用 100-risk）
        total_score = (
            trend_score * 0.25 +
            valuation_score * 0.25 +
            growth_score * 0.20 +
            (100 - risk_score) * 0.30
        )
        total_score = min(100, max(0, total_score))

        # 建议逻辑
        suggestion, risks = self._generate_advice(
            trend_score, valuation_score, growth_score, risk_score, total_score,
            closes, volumes,
        )

        # Phase R2：买入类建议（加仓）必须前置 failure_check 守门；
        # 守门拦截或故障时 fail-closed 降级为中性措辞（与 place_sim_order 口径一致）
        if "加仓" in suggestion:
            try:
                from investment_agent.failure_feedback import failure_check
                gate = failure_check(symbol, "BUY", total_score / 100)
                if not gate.get("allowed", False):
                    suggestion = "持有观察"
                    risks.append("加仓建议被失败检查守门拦截，已降级")
                if gate.get("risk_warning"):
                    risks.append(gate["risk_warning"])
            except Exception:
                suggestion = "持有观察"
                risks.append("失败检查守门不可用，加仓建议已 fail-closed 降级")

        return StockAnalysis(
            symbol=symbol, name=name, market=market,
            trend_score=round(trend_score, 1),
            trend_stars=self._to_stars(trend_score),
            valuation_score=round(valuation_score, 1),
            valuation_stars=self._to_stars(valuation_score),
            growth_score=round(growth_score, 1),
            growth_stars=self._to_stars(growth_score),
            risk_score=round(risk_score, 1),
            risk_stars=self._risk_stars(risk_score),
            total_score=round(total_score, 1),
            suggestion=suggestion,
            risks=risks,
            data_as_of=self._data_as_of,
        )

    # ── 趋势评分 ──────────────────────────────────────

    def _score_trend(self, closes: np.ndarray) -> float:
        if len(closes) < 60:
            return 50.0
        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:])
        last = closes[-1]

        # 均线位置
        ma_score = 0
        if last > ma20 > ma60:
            ma_score = 90  # 多头排列
        elif last > ma20 and ma20 < ma60:
            ma_score = 65  # 短期反弹
        elif last < ma20 and ma20 > ma60:
            ma_score = 40  # 短期调整
        elif last < ma20 < ma60:
            ma_score = 20  # 空头排列
        else:
            ma_score = 50

        # 动量
        ret_3m = closes[-1] / closes[-63] - 1 if len(closes) >= 63 else 0
        ret_6m = closes[-1] / closes[-126] - 1 if len(closes) >= 126 else 0
        momentum_3m_score = min(100, max(0, (ret_3m + 0.3) / 0.6 * 100))
        momentum_6m_score = min(100, max(0, (ret_6m + 0.3) / 0.6 * 100))

        # 近期走势
        recent_5d = closes[-1] / closes[-5] - 1 if len(closes) >= 5 else 0
        recent_score = min(100, max(0, (recent_5d + 0.1) / 0.2 * 100))

        return (
            ma_score * 0.35 +
            momentum_3m_score * 0.30 +
            momentum_6m_score * 0.20 +
            recent_score * 0.15
        )

    # ── 估值评分 ──────────────────────────────────────

    def _score_valuation(self, closes: np.ndarray) -> float:
        if len(closes) < 60:
            return 50.0
        ma60 = np.mean(closes[-60:])
        last = closes[-1]

        # 价格相对60日均线的位置（便宜 = 价格低于均线 = 高估值分）
        ratio = last / ma60
        if ratio < 0.8:
            return 85   # 极度低估
        elif ratio < 0.9:
            return 75   # 低估
        elif ratio < 1.0:
            return 65   # 略低估
        elif ratio < 1.1:
            return 50   # 合理
        elif ratio < 1.2:
            return 35   # 略高估
        elif ratio < 1.4:
            return 20   # 高估
        else:
            return 10   # 极度高估

    # ── 成长评分 ──────────────────────────────────────

    def _score_growth(self, closes: np.ndarray) -> float:
        if len(closes) < 252:
            # 用中短期动量代理成长
            ret_3m = closes[-1] / closes[-63] - 1 if len(closes) >= 63 else 0
            score = min(100, max(0, (ret_3m + 0.2) / 0.5 * 100))
            return score

        ret_12m = closes[-1] / closes[-252] - 1
        ret_6m = closes[-1] / closes[-126] - 1 if len(closes) >= 126 else 0
        score = min(100, max(0, (ret_12m * 0.6 + ret_6m * 0.4 + 0.3) / 0.7 * 100))
        return score

    # ── 风险评分 ──────────────────────────────────────

    def _score_risk(self, closes: np.ndarray) -> float:
        if len(closes) < 20:
            return 50.0
        returns = np.diff(closes) / closes[:-1]
        recent_returns = returns[-63:] if len(returns) >= 63 else returns

        # 波动率
        vol = float(np.std(recent_returns) * np.sqrt(252))
        vol_score = min(100, vol * 200)  # 年化波动20% = 40分

        # 最大回撤
        peak = np.maximum.accumulate(closes[-126:]) if len(closes) >= 126 else np.maximum.accumulate(closes)
        dd = (closes[-len(peak):] / peak - 1).min()
        dd_score = min(100, max(0, abs(dd) * 200))

        # 下行波动
        downside = recent_returns[recent_returns < 0]
        downside_vol = float(np.std(downside) * np.sqrt(252)) if len(downside) > 5 else vol
        downside_score = min(100, downside_vol * 200)

        # 价格跌破60均线
        if len(closes) >= 60:
            ma60 = np.mean(closes[-60:])
            below_ma60 = 30 if closes[-1] < ma60 else 0
        else:
            below_ma60 = 0

        risk = (
            vol_score * 0.30 +
            dd_score * 0.25 +
            downside_score * 0.25 +
            below_ma60 * 0.20
        )
        return min(100, max(0, risk))

    # ── 建议生成 ──────────────────────────────────────

    def _generate_advice(
        self, trend: float, valuation: float, growth: float,
        risk: float, total: float, closes: np.ndarray,
        volumes: np.ndarray,
    ) -> tuple[str, list[str]]:
        risks: list[str] = []

        # 风险检测
        if risk > 70:
            risks.append("高波动风险")
        if len(closes) >= 60 and closes[-1] < np.mean(closes[-60:]):
            risks.append("价格位于60日均线下方，短期承压")
        if len(volumes) >= 20:
            vol_ratio = np.mean(volumes[-5:]) / (np.mean(volumes[-20:]) or 1)
            if vol_ratio > 2:
                risks.append("近期成交量异常放大")
        if trend < 35:
            risks.append("趋势走弱")

        # 建议
        if total >= 80:
            advice = "坚定持有"
        elif total >= 65:
            advice = "继续持有"
        elif total >= 50:
            advice = "持有观察"
        elif total >= 35:
            advice = "减仓观察"
        else:
            advice = "建议减仓"

        # 特殊信号
        if risk < 25 and trend > 70:
            advice = "优质标的，可考虑加仓"
        if risk > 80 and trend < 30:
            advice = "风险较高，建议减仓"

        return advice, risks

    # ── 聚合分析 ──────────────────────────────────────

    def _aggregate(
        self, positions: list[dict[str, Any]],
        analyses: list[StockAnalysis],
    ) -> PortfolioAnalysis:
        if not analyses:
            return PortfolioAnalysis(
                total_positions=0, invested_ratio=0.0,
                top_holdings=[], sector_exposure={}, market_exposure={},
                weighted_trend=0, weighted_valuation=0, weighted_growth=0, weighted_risk=0,
                concentration_risk="low", correlation_risk="low",
                suggestions=["暂无持仓"],
            )

        # 标的权重（按金额）
        total_value = sum(
            p.get("shares", 0) * (p.get("current_price") or 0)
            for p in positions
        ) or 1
        weights = {}
        for p in positions:
            val = p.get("shares", 0) * (p.get("current_price") or 0)
            weights[p["symbol"]] = val / total_value

        # Top holdings
        sorted_pos = sorted(
            positions, key=lambda p: p.get("shares", 0) * (p.get("current_price") or 0),
            reverse=True,
        )
        top = []
        for p in sorted_pos[:5]:
            val = p.get("shares", 0) * (p.get("current_price") or 0)
            top.append({
                "symbol": p["symbol"], "name": p["name"],
                "value": round(val, 2),
                "weight": round(val / total_value * 100, 1),
            })

        # Market & sector exposure
        market_exp: dict[str, float] = {}
        sector_exp: dict[str, float] = {}
        for p in positions:
            mkt = p.get("market", "其他")
            market_exp[mkt] = market_exp.get(mkt, 0) + weights.get(p["symbol"], 0)
            # 从 stock_basic 获取行业信息
            basic = self.dp.stock_basic(p["symbol"])
            if basic and basic[0].get("industry"):
                sector = basic[0]["industry"]
                sector_exp[sector] = sector_exp.get(sector, 0) + weights.get(p["symbol"], 0)

        # Weighted scores
        analysis_map = {a.symbol: a for a in analyses}
        w_trend = w_valuation = w_growth = w_risk = 0.0
        for p in positions:
            a = analysis_map.get(p["symbol"])
            if a:
                w = weights.get(p["symbol"], 0)
                w_trend += a.trend_score * w
                w_valuation += a.valuation_score * w
                w_growth += a.growth_score * w
                w_risk += a.risk_score * w

        # 集中度风险
        top1_weight = top[0]["weight"] / 100 if top else 0
        top3_weight = sum(t["weight"] / 100 for t in top[:3]) if len(top) >= 3 else 1
        if top1_weight > 0.4 or top3_weight > 0.7:
            concentration = "high"
        elif top1_weight > 0.25 or top3_weight > 0.5:
            concentration = "medium"
        else:
            concentration = "low"

        # 相关性风险（基于日收益率的简单估算）
        correlation_risk = self._estimate_correlation_risk(positions)

        # Suggestions
        suggestions = []
        if concentration in ("high", "medium"):
            suggestions.append(f"持仓集中度{concentration}，建议分散化")
        if w_risk > 60:
            suggestions.append("组合整体风险偏高，考虑增加防御性资产")
        if w_trend < 45:
            suggestions.append("组合趋势偏弱，关注是否有逻辑变化")
        if len(sector_exp) <= 2 and len(positions) >= 4:
            suggestions.append("行业集中在少数领域，建议跨行业分散")
        if market_exp.get("ETF_CN", 0) > 0.6:
            suggestions.append("A股ETF占比过高，建议增加跨境配置")
        if correlation_risk == "high":
            suggestions.append("持仓间相关性偏高，同涨同跌风险较大")
        if w_valuation > 65:
            suggestions.append("组合估值偏高，注意回调风险")
        if w_risk < 25 and w_trend > 65:
            suggestions.append("组合质量优秀，可考虑适度加仓优质标的")

        return PortfolioAnalysis(
            total_positions=len(positions),
            invested_ratio=min(1.0, total_value / (total_value + 1)),
            top_holdings=top,
            sector_exposure={k: round(v * 100, 1) for k, v in sorted(sector_exp.items(), key=lambda x: -x[1])},
            market_exposure={k: round(v * 100, 1) for k, v in sorted(market_exp.items(), key=lambda x: -x[1])},
            weighted_trend=round(w_trend, 1),
            weighted_valuation=round(w_valuation, 1),
            weighted_growth=round(w_growth, 1),
            weighted_risk=round(w_risk, 1),
            concentration_risk=concentration,
            correlation_risk=correlation_risk,
            suggestions=suggestions,
        )

    def _estimate_correlation_risk(self, positions: list[dict[str, Any]]) -> str:
        """基于价格回报率的简单相关性估算。"""
        if len(positions) < 2:
            return "low"
        # 获取各标的近期价格序列
        price_series = {}
        for p in positions:
            bars = self.dp.daily_bars([p["symbol"]], end_date=self._data_as_of)
            if len(bars) >= 20:
                price_series[p["symbol"]] = np.array([b["close"] for b in bars[-20:]], dtype=float)
        if len(price_series) < 2:
            return "low"
        # 计算收益率相关性
        symbols_list = list(price_series.keys())
        returns = []
        for sym in symbols_list:
            r = np.diff(price_series[sym]) / price_series[sym][:-1]
            returns.append(r)
        # 计算平均相关系数
        corr_sum = 0.0
        count = 0
        for i in range(len(returns)):
            for j in range(i + 1, len(returns)):
                if len(returns[i]) >= 5 and len(returns[j]) >= 5:
                    corr = np.corrcoef(returns[i][-min(20, len(returns[i])):],
                                       returns[j][-min(20, len(returns[j])):])[0, 1]
                    if np.isfinite(corr):
                        corr_sum += abs(corr)
                        count += 1
        avg_corr = corr_sum / count if count > 0 else 0
        if avg_corr > 0.7:
            return "high"
        elif avg_corr > 0.4:
            return "medium"
        return "low"

    # ── 工具方法 ──────────────────────────────────────

    @staticmethod
    def _to_stars(score: float) -> str:
        if score >= 85:
            return "★★★★★"
        elif score >= 70:
            return "★★★★"
        elif score >= 55:
            return "★★★"
        elif score >= 40:
            return "★★"
        else:
            return "★"

    @staticmethod
    def _risk_stars(risk_score: float) -> str:
        """风险星级：分数越低（越安全）星星越多。"""
        if risk_score <= 20:
            return "★★★★★"
        elif risk_score <= 35:
            return "★★★★"
        elif risk_score <= 50:
            return "★★★"
        elif risk_score <= 70:
            return "★★"
        else:
            return "★"

    def format_stock_report(self, analysis: StockAnalysis) -> str:
        """生成可读的单股票分析报告（Markdown）。"""
        lines = [
            f"## {analysis.name}（{analysis.symbol}）",
            f"",
            f"**趋势:** {analysis.trend_stars}  ({analysis.trend_score:.0f}分)",
            f"**估值:** {analysis.valuation_stars}  ({analysis.valuation_score:.0f}分)",
            f"**成长:** {analysis.growth_stars}  ({analysis.growth_score:.0f}分)",
            f"**风险:** {analysis.risk_stars}  ({analysis.risk_score:.0f}分 — 分越低越安全)",
            f"",
            f"**综合评分: {analysis.total_score:.0f}分**",
            f"",
            f"**建议:** {analysis.suggestion}",
        ]
        if analysis.risks:
            lines.append("")
            lines.append("**风险提示:**")
            for r in analysis.risks:
                lines.append(f"- {r}")
        return "\n".join(lines)

    def format_portfolio_report(self, analysis: PortfolioAnalysis) -> str:
        """生成可读的投资组合分析报告（Markdown）。"""
        lines = [
            f"# 投资组合分析报告",
            f"",
            f"## 概览",
            f"- 持仓数量: {analysis.total_positions} 只",
            f"- 仓位比例: {analysis.invested_ratio:.1%}",
            f"",
            f"## 前5大持仓",
        ]
        for h in analysis.top_holdings:
            lines.append(f"- {h['name']}（{h['symbol']}）: {h['weight']:.1f}%")
        lines.extend([
            f"",
            f"## 市场分布",
        ])
        for mkt, pct in analysis.market_exposure.items():
            lines.append(f"- {mkt}: {pct:.1f}%")
        lines.extend([
            f"",
            f"## 加权评分",
            f"- 趋势: {analysis.weighted_trend:.0f}/100",
            f"- 估值: {analysis.weighted_valuation:.0f}/100",
            f"- 成长: {analysis.weighted_growth:.0f}/100",
            f"- 风险: {analysis.weighted_risk:.0f}/100（越低越好）",
            f"",
            f"## 风险诊断",
            f"- 集中度风险: {analysis.concentration_risk}",
            f"- 相关性风险: {analysis.correlation_risk}",
        ])
        if analysis.suggestions:
            lines.extend([
                f"",
                f"## 优化建议",
            ])
            for s in analysis.suggestions:
                lines.append(f"- {s}")
        return "\n".join(lines)
