"""
量化策略核心引擎 v3 — 采用成熟多因子体系。

因子体系（设计文档定义）：
    Value(25%)     PE, PB, EV/EBITDA, FCF Yield
    Quality(25%)   ROE, ROIC, 利润稳定性, 现金流质量
    Growth(20%)    收入增长, 利润增长, 研发投入
    Momentum(15%)  3个月收益, 6个月收益, 12个月收益
    Risk(15%)      波动率, 最大回撤, Beta, 财务风险

数据源：DuckDB 数据仓库（通过 foundf_db.DataProvider）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from foundf_db import DataProvider


@dataclass(frozen=True)
class FactorConfig:
    """因子权重与参数配置。"""
    # 五大因子权重
    value_weight: float = 0.25
    quality_weight: float = 0.25
    growth_weight: float = 0.20
    momentum_weight: float = 0.15
    risk_weight: float = 0.15

    # 策略参数
    min_history: int = 130               # 最小历史天数
    top_n: int = 6                       # 选入前 N 只
    max_weight: float = 0.25             # 单标的最大权重
    target_vol: float = 0.14             # 目标年化波动率
    min_budget: float = 0.25             # 最低风险预算
    turnover_blend: float = 0.25         # 换手率平滑
    no_trade_band: float = 0.02          # 不交易区间

    # 价值因子参数
    value_factors: tuple[str, float] = (
        ("pe_ratio", 0.30),
        ("pb_ratio", 0.25),
        ("fcf_yield", 0.25),
        ("price_to_ma60", 0.20),         # 价格/60日均线（估值代理）
    )

    # 质量因子参数
    quality_factors: tuple[str, float] = (
        ("roe", 0.35),
        ("profit_stability", 0.30),
        ("cashflow_quality", 0.20),
        ("debt_ratio_inv", 0.15),         # 负债率反向
    )

    # 成长因子参数
    growth_factors: tuple[str, float] = (
        ("revenue_growth", 0.35),
        ("profit_growth", 0.35),
        ("r_and_d_intensity", 0.15),
        ("momentum_12m", 0.15),           # 12月收益代理成长
    )

    # 动量因子参数
    momentum_factors: tuple[str, float] = (
        ("momentum_3m", 0.40),
        ("momentum_6m", 0.35),
        ("momentum_12m", 0.25),
    )

    # 风险因子参数（_risk_score 为安全分，越高越安全，综合分直接用百分位）
    risk_factors: tuple[str, float] = (
        ("volatility", 0.30),
        ("max_drawdown", 0.25),
        ("downside_risk", 0.25),
        ("beta_raw", 0.20),
    )


DEFAULT_CONFIG = FactorConfig()


class FactorEngine:
    """因子计算引擎。

    使用方式:
        engine = FactorEngine(dp)
        # 计算所有标的的因子
        factors = engine.compute_all(snapshot_data)
        # 生成权重
        weights = engine.generate_weights(factors, portfolio_state)
    """

    def __init__(self, dp: DataProvider, config: FactorConfig = DEFAULT_CONFIG):
        self.dp = dp
        self.config = config
        self._all_factors: dict[str, dict[str, float]] = {}

    # ═══════════════════════════════════════════════════════
    # 公开 API
    # ═══════════════════════════════════════════════════════

    def compute_all(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        """计算所有标的五大因子分数。"""
        self._all_factors = {}
        # 最新估值快照（daily_basic：pe_ttm/pb），数据源缺失时为空 → 价值因子回退均线代理
        latest_basic: dict[str, dict[str, Any]] = {}
        fetch_basic = getattr(self.dp, "latest_daily_basic", None)
        if callable(fetch_basic):
            for row in fetch_basic(symbols) or []:
                latest_basic[row["symbol"]] = row
        self._value_source = {"real": 0, "proxy": 0}
        # 市场基准收益（沪深300，供真 Beta 计算；取不到则 Beta 分量中性 0.5）
        self._market_returns: np.ndarray | None = None
        try:
            mkt_bars = self.dp.daily_bars(["sh.000300"])
            if len(mkt_bars) >= 64:
                mkt_closes = np.array([b["close"] for b in mkt_bars], dtype=float)
                mkt_ret = np.diff(mkt_closes) / mkt_closes[:-1]
                self._market_returns = mkt_ret[-63:]
        except Exception:
            self._market_returns = None
        for symbol in symbols:
            bars = self.dp.daily_bars([symbol])
            if len(bars) < self.config.min_history:
                continue
            closes = np.array([b["close"] for b in bars], dtype=float)
            volumes = np.array([b.get("volume", 0) or 0 for b in bars], dtype=float)
            amounts = np.array([b.get("amount", 0) or 0 for b in bars], dtype=float)

            # 获取基本面信息
            basic = self.dp.stock_basic(symbol)

            value, value_src = self._value_score(closes, latest_basic.get(symbol))
            self._value_source[value_src] += 1
            self._all_factors[symbol] = {
                "value": value,
                "quality": self._quality_score(closes, basic),
                "growth": self._growth_score(closes),
                "momentum": self._momentum_score(closes),
                "risk": self._risk_score(closes),
                "volatility": self._calc_volatility(closes),
                "downside_vol": self._calc_downside_vol(closes),
                "last_price": float(closes[-1]),
            }
        return self._all_factors

    def generate_weights(
        self,
        portfolio_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """根据因子分数生成目标权重。"""
        factors = self._all_factors
        if not factors:
            return {"weights": {"CASH": 1.0}, "diagnostics": {"reason": "no_data"}}

        symbols = sorted(factors.keys())
        # 横截面标准化
        raw_scores = {}
        for sym in symbols:
            f = factors[sym]
            raw_scores[sym] = (
                self.config.value_weight * self._percentile(sym, factors, "value") +
                self.config.quality_weight * self._percentile(sym, factors, "quality") +
                self.config.growth_weight * self._percentile(sym, factors, "growth") +
                self.config.momentum_weight * self._percentile(sym, factors, "momentum") +
                # _risk_score 是安全分(越高越安全), 直接用百分位;
                # 2026-08-11 前曾写 1-percentile, 方向反转奖励高波动股(P0 审查发现)
                self.config.risk_weight * self._percentile(sym, factors, "risk")
            )

        # 选入 top_n
        sorted_syms = sorted(raw_scores, key=lambda s: raw_scores[s], reverse=True)
        selected = sorted_syms[:self.config.top_n]

        # 波动率预算
        vols = [factors[s]["volatility"] for s in selected]
        median_vol = float(np.median(vols)) if vols else 0.2
        vol_budget = min(1.0, self.config.target_vol / max(median_vol, 0.01))
        breadth = len(selected) / max(len(symbols), 1)
        breadth_budget = 0.45 + 0.55 * breadth
        risk_budget = float(np.clip(vol_budget * breadth_budget, self.config.min_budget, 1.0))

        # 相关性惩罚
        correlation = self._correlation_penalty(selected)

        # 权重分配
        conviction = np.array([max(raw_scores[s] - 0.5, 0.05) for s in selected])
        corr_values = np.array([correlation.get(s, 1.0) for s in selected])
        downside_vols = np.array([max(factors[s]["downside_vol"], 0.01) for s in selected])
        raw = conviction * corr_values / downside_vols
        target = self._capped_normalize(raw, self.config.max_weight, risk_budget)
        weights = {s: float(w) for s, w in zip(selected, target)}
        weights["CASH"] = round(max(0.0, 1.0 - sum(weights.values())), 6)

        # 诊断信息
        diagnostics = {
            "model": "multifactor_v3",
            "factor_weights": {
                "value": self.config.value_weight,
                "quality": self.config.quality_weight,
                "growth": self.config.growth_weight,
                "momentum": self.config.momentum_weight,
                "risk": self.config.risk_weight,
            },
            "selected": selected,
            "risk_budget": round(risk_budget, 4),
            "value_source": dict(getattr(self, "_value_source", {})),
            "scores": {s: round(raw_scores[s], 4) for s in symbols},
            "factor_breakdown": {
                s: {k: round(v, 4) for k, v in factors[s].items()}
                for s in selected
            },
        }

        return {"weights": weights, "diagnostics": diagnostics}

    # ═══════════════════════════════════════════════════════
    # 五大因子计算
    # ═══════════════════════════════════════════════════════

    def _value_score(
        self,
        closes: np.ndarray,
        basic: dict[str, Any] | None = None,
    ) -> tuple[float, str]:
        """价值因子：真实 EP/BP（daily_basic）优先，缺失回退 60 日均线代理。

        返回 ``(score, source)``，source ∈ {"real", "proxy"}，供治理诊断统计。
        负 PE/PB（亏损/资不抵债）不直接给价值分，该分量记 0 与
        ``research_engine`` 的价值排序口径一致。
        """
        ep_score: float | None = None
        bp_score: float | None = None
        if basic:
            pe_ttm = basic.get("pe_ttm")
            pb = basic.get("pb")
            if pe_ttm is not None:
                # EP = 1/PE_TTM，EP 10%（PE=10）记满分；负 PE → 0 分
                ep_score = float(np.clip((1.0 / pe_ttm) / 0.10, 0, 1)) if pe_ttm > 0 else 0.0
            if pb is not None:
                # BP = 1/PB，PB=1 记满分；负 PB → 0 分
                bp_score = float(np.clip((1.0 / pb) / 1.0, 0, 1)) if pb > 0 else 0.0
        if ep_score is not None or bp_score is not None:
            parts = [(s, w) for s, w in ((ep_score, 0.6), (bp_score, 0.4)) if s is not None]
            total_w = sum(w for _, w in parts)
            return float(sum(s * w for s, w in parts) / total_w), "real"

        # 回退：价格相对 60 日均线的位置（无估值数据时的代理）
        if len(closes) < 60:
            return 0.5, "proxy"
        ma60 = np.mean(closes[-60:])
        ratio = closes[-1] / ma60
        # 低于均线 = 便宜 = 高价值分
        if ratio < 0.8:
            return 0.85, "proxy"
        elif ratio < 0.9:
            return 0.75, "proxy"
        elif ratio < 1.0:
            return 0.65, "proxy"
        elif ratio < 1.1:
            return 0.50, "proxy"
        elif ratio < 1.2:
            return 0.35, "proxy"
        elif ratio < 1.4:
            return 0.20, "proxy"
        return 0.10, "proxy"

    def _quality_score(self, closes: np.ndarray, basic: list[dict[str, Any]] | None) -> float:
        """质量因子：ROE代理 + 利润稳定性 + 动量稳定性。"""
        if len(closes) < 130:
            return 0.5
        returns = np.diff(closes) / closes[:-1]

        # 利润稳定性（月度正收益比例）
        monthly_returns = []
        for i in range(0, len(returns), 21):
            chunk = returns[i:i + 21]
            if len(chunk) > 0:
                monthly_returns.append(float(np.prod(1 + chunk) - 1))
        positive_months = float(np.mean(np.array(monthly_returns) > 0)) if monthly_returns else 0.5

        # 最大回撤幅度（越小质量越高）
        peak = np.maximum.accumulate(closes[-126:]) if len(closes) >= 126 else np.maximum.accumulate(closes)
        dd = (closes[-len(peak):] / peak - 1).min()
        dd_quality = 1.0 - min(1.0, abs(dd) * 2)

        score = positive_months * 0.5 + dd_quality * 0.5
        return float(np.clip(score, 0, 1))

    def _growth_score(self, closes: np.ndarray) -> float:
        """成长因子：中长期收益 + 趋势强度。"""
        if len(closes) < 252:
            ret_6m = closes[-1] / closes[-126] - 1 if len(closes) >= 126 else 0
            ret_3m = closes[-1] / closes[-63] - 1 if len(closes) >= 63 else 0
            score = (max(ret_6m, 0) * 0.6 + max(ret_3m, 0) * 0.4) / 0.5
            return float(np.clip(score, 0, 1))

        ret_12m = closes[-1] / closes[-252] - 1
        ret_6m = closes[-1] / closes[-126] - 1
        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:])
        trend_strength = 0.5 if closes[-1] > ma20 > ma60 else (0.3 if closes[-1] > ma60 else 0.1)
        score = (max(ret_12m, 0) * 0.35 + max(ret_6m, 0) * 0.35 + trend_strength * 0.30) / 0.7
        return float(np.clip(score, 0, 1))

    def _momentum_score(self, closes: np.ndarray) -> float:
        """动量因子：多周期动量。"""
        if len(closes) < 63:
            return 0.5
        ret_3m = closes[-1] / closes[-63] - 1
        ret_6m = closes[-1] / closes[-126] - 1 if len(closes) >= 126 else 0
        ret_12m = closes[-1] / closes[-252] - 1 if len(closes) >= 252 else 0
        score = (
            max(ret_3m, 0) / 0.3 * 0.40 +
            max(ret_6m, 0) / 0.4 * 0.35 +
            max(ret_12m, 0) / 0.5 * 0.25
        )
        return float(np.clip(score, 0, 1))

    def _risk_score(self, closes: np.ndarray) -> float:
        """风险因子（安全分）：波动+回撤+下行风险+Beta（越高越安全）。"""
        if len(closes) < 63:
            return 0.5

        # 波动率 → 0-1 分（高波动 = 低分）
        vol = self._calc_volatility(closes)
        vol_score = max(0, 1 - vol * 3)

        # 最大回撤
        if len(closes) >= 126:
            peak = np.maximum.accumulate(closes[-126:])
            dd = (closes[-126:] / peak - 1).min()
        else:
            peak = np.maximum.accumulate(closes)
            dd = (closes / peak - 1).min()
        dd_score = max(0, 1 - abs(dd) * 3)

        # 下行风险
        downside = self._calc_downside_vol(closes)
        downside_score = max(0, 1 - downside * 3)

        # Beta（相对沪深300；无基准数据时中性 0.5）
        returns = np.diff(closes) / closes[:-1]
        mkt = getattr(self, "_market_returns", None)
        if len(returns) >= 63 and mkt is not None and len(mkt) >= 63:
            r = returns[-63:]
            m = mkt[-63:]
            var_m = float(np.var(m))
            if var_m > 1e-12:
                beta = float(np.cov(r, m)[0, 1] / var_m)
                # 防御口径：beta 越低风险分越高；beta≥2 记 0
                beta_score = float(np.clip(1.5 - beta, 0, 1))
            else:
                beta_score = 0.5
        else:
            beta_score = 0.5

        score = vol_score * 0.30 + dd_score * 0.25 + downside_score * 0.25 + beta_score * 0.20
        return float(np.clip(score, 0, 1))

    # ═══════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _calc_volatility(closes: np.ndarray) -> float:
        returns = np.diff(closes) / closes[:-1]
        return float(np.std(returns[-63:]) * np.sqrt(252)) if len(returns) >= 63 else 0.3

    @staticmethod
    def _calc_downside_vol(closes: np.ndarray) -> float:
        returns = np.diff(closes) / closes[:-1]
        recent = returns[-63:] if len(returns) >= 63 else returns
        downside = recent[recent < 0]
        if len(downside) > 5:
            return float(np.std(downside) * np.sqrt(252))
        return float(np.std(recent) * np.sqrt(252))

    @staticmethod
    def _percentile(symbol: str, factors: dict, key: str) -> float:
        """横截面百分位。"""
        values = np.array([f[key] for f in factors.values()])
        if len(values) < 2 or np.nanstd(values) == 0:
            return 0.5
        v = factors[symbol][key]
        rank = np.sum(values < v) / len(values)
        return float(rank)

    @staticmethod
    def _capped_normalize(
        raw: np.ndarray, cap: float, total: float,
    ) -> np.ndarray:
        weights = np.zeros_like(raw, dtype=float)
        remaining_indices = list(range(len(raw)))
        budget = total
        while remaining_indices and budget > 1e-10:
            values = np.maximum(raw[remaining_indices], 0)
            if values.sum() <= 0:
                break
            proposal = values / values.sum() * budget
            hit = proposal > cap
            if not hit.any():
                weights[remaining_indices] += proposal
                break
            for idx in np.where(hit)[0]:
                actual_idx = remaining_indices[idx]
                alloc = min(cap - weights[actual_idx], budget)
                weights[actual_idx] += alloc
                budget -= alloc
            remaining_indices = [
                i for i in remaining_indices
                if weights[i] < cap - 1e-10
            ]
        return weights

    def _correlation_penalty(self, symbols: list[str]) -> dict[str, float]:
        """基于收益率的相关性惩罚。"""
        series = {}
        for sym in symbols:
            bars = self.dp.daily_bars([sym])
            if len(bars) < 63:
                continue
            closes = np.array([b["close"] for b in bars[-63:]], dtype=float)
            series[sym] = np.diff(closes) / closes[:-1]
        if len(series) < 2:
            return {s: 1.0 for s in symbols}

        penalty = {}
        for sym in symbols:
            if sym not in series:
                penalty[sym] = 1.0
                continue
            peers = [s for s in symbols if s != sym and s in series]
            if not peers:
                penalty[sym] = 1.0
                continue
            min_len = min(len(series[sym]), min(len(series[p]) for p in peers))
            r1 = series[sym][-min_len:]
            correlations = []
            for peer in peers:
                r2 = series[peer][-min_len:]
                corr = np.corrcoef(r1, r2)[0, 1]
                if np.isfinite(corr):
                    correlations.append(abs(corr))
            avg_corr = float(np.mean(correlations)) if correlations else 0
            penalty[sym] = 1.0 / (1.0 + avg_corr)
        return penalty


# ═══════════════════════════════════════════════════════════
# 策略入口函数（与 existing generate_multifactor_signal.py 兼容）
# ═══════════════════════════════════════════════════════════

def generate(
    dp: DataProvider | None = None,
    symbols: list[str] | None = None,
    portfolio_state: dict[str, Any] | None = None,
    config: FactorConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """生成多因子策略信号。

    返回格式与 generate_multifactor_signal.generate() 兼容。
    """
    engine = FactorEngine(dp, config) if dp else None
    if engine is None or not symbols:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "status": "no_data",
            "weights": {"CASH": 1.0},
            "diagnostics": {"reason": "no_data_provider_or_symbols"},
        }

    factors = engine.compute_all(symbols)
    result = engine.generate_weights(portfolio_state)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["data_as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result["status"] = "ready"
    return result
