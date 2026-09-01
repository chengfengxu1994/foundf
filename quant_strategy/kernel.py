"""统一策略内核（阶段 0）：multifactor_v3_sim.5 生产语义的纯函数复刻。

本模块是 ``FactorEngine``（``quant_strategy/__init__.py``）数值路径的
**逐行同序同精度** 纯函数版：零 IO、零全局状态，输入全部是内存中的
numpy 序列 / dict，输出与 ``compute_all`` + ``generate_weights`` 完全一致。

Parity 锁定：
    数值等价由 ``tests/strategy/test_kernel_parity.py`` 锁定——合成数据
    下五桶分数 abs diff < 1e-12、综合分一致、weights（含 CASH round-6）
    与 diagnostics 完全相等。任何对内核的修改必须先确认 parity 测试全绿。

已知缺陷（原样复刻，**不要在本模块"顺手修复"**）：
    - Beta 计算不按日期对齐：股票与基准各取末尾 63 日收益直接
      ``np.cov(r, m)``，序列日期错位时照算。这是已记录的待 supersede
      缺陷，修复走 multifactor_v3_sim.6 治理流程，不走本模块。
    - quality / growth 为纯价格代理（``financial_statement`` 表为空），
      diagnostics 中以 ``QUALITY_GROWTH_PROXY = True`` 显式常量标注
      （附加键，不改变现有 diagnostics 键集合）。
    - ``FactorConfig`` 的 ``value_factors`` 等子因子权重 tuple 是死配置，
      compute 路径从未读取，真实公式全部硬编码——本模块以硬编码为准。

接口契约（阶段 1 runner 适配用）：
    - ``score_symbol``：单标的五桶打分（等价 compute_all 循环体）
    - ``cross_sectional_scores``：横截面百分位 + 五桶加权（等价
      generate_weights 的 raw_scores 段）
    - ``weights_from_scores``：选股/风险预算/相关性惩罚/水位归一/CASH/
      diagnostics（等价 generate_weights 余下全部；相关性惩罚吃内存
      returns 视图，不再二次向 dp 取数）
    - ``apply_no_trade_band``：自 ``daily_candidates.py`` 原样搬入
      （函数体一字未动），daily_candidates 改为 re-export。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import FactorConfig

#: 质量/成长因子为纯价格代理的显式标注（附加 diagnostics 键，不改现有键）
QUALITY_GROWTH_PROXY = True


# ═══════════════════════════════════════════════════════
# 单标的打分（等价 FactorEngine.compute_all 的循环体）
# ═══════════════════════════════════════════════════════

def score_symbol(
    closes: np.ndarray,
    valuation: tuple[float | None, float | None] | None,
    market_returns63: np.ndarray | None,
    config: FactorConfig,
) -> tuple[dict[str, float], str]:
    """单标的五桶打分，等价 FactorEngine.compute_all 的循环体。

    参数:
        closes: 全历史收盘价（np.ndarray，按日期升序）
        valuation: ``(pe_ttm, pb)`` 元组或 None（等价 latest_basic.get(symbol)
            取出的 ``{"pe_ttm":…, "pb":…}``；仅这两个键参与计算，
            turnover_rate 在生产路径中未被读取）
        market_returns63: 基准（沪深300）末尾 63 日收益或 None
        config: FactorConfig（本函数体不读字段，保留参数以对齐调用形状）

    返回 ``(factors, value_source)``：
        factors 键与 compute_all 逐标的 dict 完全一致
        （value/quality/growth/momentum/risk/volatility/downside_vol/last_price），
        value_source ∈ {"real", "proxy"}。
    """
    value, value_src = _value_score(closes, valuation)
    factors = {
        "value": value,
        "quality": _quality_score(closes),
        "growth": _growth_score(closes),
        "momentum": _momentum_score(closes),
        "risk": _risk_score(closes, market_returns63),
        "volatility": _calc_volatility(closes),
        "downside_vol": _calc_downside_vol(closes),
        "last_price": float(closes[-1]),
    }
    return factors, value_src


def _value_score(
    closes: np.ndarray,
    valuation: tuple[float | None, float | None] | None,
) -> tuple[float, str]:
    """价值因子（复刻 FactorEngine._value_score）：EP/BP 实值优先，缺则均线代理。"""
    ep_score: float | None = None
    bp_score: float | None = None
    if valuation:
        pe_ttm, pb = valuation
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


def _quality_score(closes: np.ndarray) -> float:
    """质量因子（复刻 FactorEngine._quality_score）：纯价格代理。

    basic（stock_basic 空壳）在生产路径中从未被读取，此处不保留该参数。
    """
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


def _growth_score(closes: np.ndarray) -> float:
    """成长因子（复刻 FactorEngine._growth_score）：中长期收益 + 趋势强度。"""
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


def _momentum_score(closes: np.ndarray) -> float:
    """动量因子（复刻 FactorEngine._momentum_score）：多周期动量。"""
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


def _risk_score(closes: np.ndarray, market_returns63: np.ndarray | None) -> float:
    """风险因子（复刻 FactorEngine._risk_score）：安全分，越高越安全。

    Beta 不按日期对齐（股票与基准各取末尾 63 日收益直接 cov）——
    原样复刻的已知缺陷，待 sim.6 supersede 修复，勿在本模块"修正"。
    """
    if len(closes) < 63:
        return 0.5

    # 波动率 → 0-1 分（高波动 = 低分）
    vol = _calc_volatility(closes)
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
    downside = _calc_downside_vol(closes)
    downside_score = max(0, 1 - downside * 3)

    # Beta（相对沪深300；无基准数据时中性 0.5）
    returns = np.diff(closes) / closes[:-1]
    mkt = market_returns63
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


def _calc_volatility(closes: np.ndarray) -> float:
    """复刻 FactorEngine._calc_volatility：63 日日收益 std × √252。"""
    returns = np.diff(closes) / closes[:-1]
    return float(np.std(returns[-63:]) * np.sqrt(252)) if len(returns) >= 63 else 0.3


def _calc_downside_vol(closes: np.ndarray) -> float:
    """复刻 FactorEngine._calc_downside_vol：63 日负收益 std × √252。"""
    returns = np.diff(closes) / closes[:-1]
    recent = returns[-63:] if len(returns) >= 63 else returns
    downside = recent[recent < 0]
    if len(downside) > 5:
        return float(np.std(downside) * np.sqrt(252))
    return float(np.std(recent) * np.sqrt(252))


# ═══════════════════════════════════════════════════════
# 横截面综合分（等价 generate_weights 的 raw_scores 段）
# ═══════════════════════════════════════════════════════

def cross_sectional_scores(
    factors: dict[str, dict[str, float]],
    config: FactorConfig,
) -> dict[str, float]:
    """横截面百分位 + 五桶加权，等价 generate_weights 的 raw_scores 段。

    输出 ``{symbol: raw_score}``，键序与 ``sorted(factors.keys())`` 一致
    （生产路径 dict 插入序即 sorted 顺序）。
    """
    raw_scores = {}
    for sym in sorted(factors.keys()):
        raw_scores[sym] = (
            config.value_weight * _percentile(sym, factors, "value") +
            config.quality_weight * _percentile(sym, factors, "quality") +
            config.growth_weight * _percentile(sym, factors, "growth") +
            config.momentum_weight * _percentile(sym, factors, "momentum") +
            # _risk_score 是安全分(越高越安全), 直接用百分位
            config.risk_weight * _percentile(sym, factors, "risk")
        )
    return raw_scores


def _percentile(symbol: str, factors: dict, key: str) -> float:
    """横截面百分位（复刻 FactorEngine._percentile）。

    注意: 输入必须是**完整**的 factors dict（含全部逐标的键），与生产
    一致；子集会改变百分位分母。
    """
    values = np.array([f[key] for f in factors.values()])
    if len(values) < 2 or np.nanstd(values) == 0:
        return 0.5
    v = factors[symbol][key]
    rank = np.sum(values < v) / len(values)
    return float(rank)


# ═══════════════════════════════════════════════════════
# 权重生成（等价 generate_weights 余下全部）
# ═══════════════════════════════════════════════════════

def weights_from_scores(
    raw_scores: dict[str, float],
    factors: dict[str, dict[str, float]],
    returns63_by_symbol: dict[str, np.ndarray],
    config: FactorConfig,
    value_source: dict[str, int] | None = None,
) -> dict[str, Any]:
    """由横截面综合分生成目标权重，等价 FactorEngine.generate_weights。

    参数:
        raw_scores: ``cross_sectional_scores`` 的输出（{symbol: 综合分}）
        factors: **完整**的逐标的因子 dict（百分位诊断/选股需全宇宙）
        returns63_by_symbol: 相关性惩罚的内存 returns 视图——
            ``{symbol: 最后 63 根收盘的日收益序列(长度62)}``。
            生产 FactorEngine._correlation_penalty 二次向 dp 取数，
            本函数改为吃内存视图（数值完全同源）；
            缺键或序列不足 62 等价生产 ``len(bars) < 63`` 的 continue 分支。
        config: FactorConfig
        value_source: ``build_value_source_counter`` 的输出（real/proxy
            计数）。生产挂在 engine 实例状态上，内核无全局状态，故显式
            传入；缺省写 ``{}``。

    返回 ``{"weights": …, "diagnostics": …}``，diagnostics 键名与现网
    完全一致（model/factor_weights/selected/risk_budget/value_source/
    scores/factor_breakdown），另加 QUALITY_GROWTH_PROXY 常量标注。
    """
    if not factors:
        return {"weights": {"CASH": 1.0}, "diagnostics": {"reason": "no_data"}}

    symbols = sorted(factors.keys())

    # 选入 top_n（stable sort 与生产 sorted(reverse=True) 同行为）
    sorted_syms = sorted(raw_scores, key=lambda s: raw_scores[s], reverse=True)
    selected = sorted_syms[:config.top_n]

    # 波动率预算
    vols = [factors[s]["volatility"] for s in selected]
    median_vol = float(np.median(vols)) if vols else 0.2
    vol_budget = min(1.0, config.target_vol / max(median_vol, 0.01))
    breadth = len(selected) / max(len(symbols), 1)
    breadth_budget = 0.45 + 0.55 * breadth
    risk_budget = float(np.clip(vol_budget * breadth_budget, config.min_budget, 1.0))

    # 相关性惩罚（内存 returns 视图，不再二次取数）
    correlation = _correlation_penalty(selected, returns63_by_symbol)

    # 权重分配
    conviction = np.array([max(raw_scores[s] - 0.5, 0.05) for s in selected])
    corr_values = np.array([correlation.get(s, 1.0) for s in selected])
    downside_vols = np.array([max(factors[s]["downside_vol"], 0.01) for s in selected])
    raw = conviction * corr_values / downside_vols
    target = _capped_normalize(raw, config.max_weight, risk_budget)
    weights = {s: float(w) for s, w in zip(selected, target)}
    weights["CASH"] = round(max(0.0, 1.0 - sum(weights.values())), 6)

    # 诊断信息（键名与现网完全一致；value_source 由调用方合入）
    diagnostics = {
        "model": "multifactor_v3",
        "factor_weights": {
            "value": config.value_weight,
            "quality": config.quality_weight,
            "growth": config.growth_weight,
            "momentum": config.momentum_weight,
            "risk": config.risk_weight,
        },
        "selected": selected,
        "risk_budget": round(risk_budget, 4),
        "value_source": dict(value_source or {}),
        "scores": {s: round(raw_scores[s], 4) for s in symbols},
        "factor_breakdown": {
            s: {k: round(v, 4) for k, v in factors[s].items()}
            for s in selected
        },
        # 附加常量标注：quality/growth 为纯价格代理（不改变现有键）
        "QUALITY_GROWTH_PROXY": QUALITY_GROWTH_PROXY,
    }

    return {"weights": weights, "diagnostics": diagnostics}


def build_value_source_counter(
    scored: dict[str, tuple[dict[str, float], str]],
) -> dict[str, int]:
    """由 score_symbol 批量输出统计 value_source 计数。

    等价 FactorEngine.compute_all 中 ``self._value_source[src] += 1``；
    内核无全局状态，故单独提供。``scored`` 为 ``{symbol: (factors, src)}``。
    """
    counter = {"real": 0, "proxy": 0}
    for _, src in scored.values():
        counter[src] += 1
    return counter


def _capped_normalize(
    raw: np.ndarray, cap: float, total: float,
) -> np.ndarray:
    """迭代水位法归一（复刻 FactorEngine._capped_normalize，一字未动）。"""
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


def _correlation_penalty(
    symbols: list[str],
    returns63_by_symbol: dict[str, np.ndarray],
) -> dict[str, float]:
    """相关性惩罚（复刻 FactorEngine._correlation_penalty 的数值语义）。

    生产版二次向 dp 取数（bars[-63:] → 62 长度收益）；本版吃内存
    returns 视图，调用方按同一口径构造。键缺失或长度 < 62 等价生产
    ``len(bars) < 63`` 的 continue 分支（penalty=1.0）。
    """
    series = {}
    for sym in symbols:
        rets = returns63_by_symbol.get(sym)
        if rets is None or len(rets) < 62:
            continue
        series[sym] = np.asarray(rets, dtype=float)
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


# ═══════════════════════════════════════════════════════
# no_trade_band 缓冲带（自 daily_candidates.py 原样搬入，函数体一字未动）
# ═══════════════════════════════════════════════════════

def apply_no_trade_band(
    weights: dict[str, float],
    prev_weights: dict[str, float] | None,
    band: float,
) -> tuple[dict[str, float], list[str]]:
    """纯函数：no_trade_band 缓冲带（v3_sim.5，抑制日度换手磨损）。

    与上一批 targets 对比，权重变化绝对值 < ``band`` 的标的沿用上一批
    权重（不发出调仓信号），变化超带才用新权重；新入选标的无上一批
    权重，直接按新权重进入。返回 ``(调整后权重, 带内沿用的标的列表)``，
    CASH 按调整后非现金权重重新计算。``prev_weights`` 为 None（首批）
    时原样返回。
    """
    if not prev_weights:
        return dict(weights), []
    adjusted: dict[str, float] = {}
    kept: list[str] = []
    for symbol, weight in weights.items():
        if symbol == "CASH":
            continue
        prev = prev_weights.get(symbol)
        if prev is not None and abs(weight - prev) < band:
            adjusted[symbol] = prev
            kept.append(symbol)
        else:
            adjusted[symbol] = weight
    adjusted["CASH"] = round(max(0.0, 1.0 - sum(adjusted.values())), 6)
    return adjusted, kept


# ═══════════════════════════════════════════════════════
# P3 市场状态阀门（multifactor_v5_valve.1，预注册冻结
# sf_674cc7bd2cf24f73443d，2026-08-29）——信号驱动自由仓位
# ═══════════════════════════════════════════════════════

VALVE_FLOOR = 0.3        # 股票预算下限（市场最贵时）
VALVE_WINDOW = 1250      # 时序分位窗口（约 5 年交易日）
VALVE_MIN_OBS = 250      # 窗口内最少有效观测
VALVE_MIN_POOL = 20      # 池级中位数最少有效标的不低于此才出数


def pool_ep_ts_budget(
    pool_ep: "np.ndarray | list[float]",
    floor: float = VALVE_FLOOR,
    window: int = VALVE_WINDOW,
    min_obs: int = VALVE_MIN_OBS,
) -> tuple[float | None, float | None]:
    """纯函数：池级 EP 中位水平的自身时序分位 → 股票预算。

    ``pool_ep``: 升序日频池级 EP（1/PE）中位值序列，末尾为当前日，
    NaN 为无效日。分位高 = EP 处于自身历史高位 = 便宜 → 加仓：
    ``budget = floor + (1-floor) * pct``（方向勿反：EP 高=便宜；
    v1 曾写反成贵→加仓，2026-08-29 修正）。

    观测不足（当前值无效或窗口有效观测 < min_obs）返回
    ``(None, None)``，调用方跳过阀门、沿用内核原始权重。
    """
    arr = np.asarray(pool_ep, dtype=float)
    if arr.size == 0:
        return None, None
    cur = arr[-1]
    if not np.isfinite(cur):
        return None, None
    window_vals = arr[max(0, arr.size - window):]
    window_vals = window_vals[np.isfinite(window_vals)]
    if window_vals.size < min_obs:
        return None, None
    pct = float(np.mean(window_vals <= cur))
    budget = float(floor + (1.0 - floor) * pct)
    return budget, pct


def apply_regime_valve(
    weights: dict[str, float],
    budget: float,
) -> dict[str, float]:
    """纯函数：把内核权重（含 CASH）重归一到指定股票预算——
    股票腿等比缩放，CASH = 1 - budget。股票腿为 0 时原样返回。
    与 deploy/adhoc_year_backtest.py 的 _apply_regime_valve 同源。
    """
    eq = {s: v for s, v in weights.items() if s != "CASH"}
    eq_sum = sum(eq.values())
    if eq_sum <= 0:
        return dict(weights)
    scale = budget / eq_sum
    out = {s: float(v * scale) for s, v in eq.items()}
    out["CASH"] = round(max(0.0, 1.0 - budget), 6)
    return out
