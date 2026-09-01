"""一次性对照回测: 近似 multifactor 选股逻辑 2022-2024 逐年收益 vs 标普500/沪深300。

背景: 用户 2026-08-10 要求「回测成绩对比标普500, 2022/2023/2024 同端时间」。
生产 Walk-Forward 链路当前 NOT_READY(universe 78<100、reanchor 后哈希失配、
basis 未人工批准), 故用本脚本做**独立一次性**对照, 不走治理证据链。

口径(全部保守披露):
- 选股逻辑近似 multifactor_v3_sim: 价值(EP=1/pe_ttm, BP=1/pb, 来自 daily_basic
  point-in-time 快照) + 动量(63/126/252 交易日收益 0.40/0.35/0.25) + 低波
  (63 日波动/126 日最大回撤/下行风险反向)。质量/成长两桶因无 point-in-time
  财报数据**剔除**, 三桶按生产 25:15:15 归一化为 0.4545/0.2727/0.2727。
- 宇宙: daily_price 中 6 位 A 股、全历史 source 单一(与 bundle 选择规则一致),
  剔除上市不满 130 交易日者。**幸存者偏差**: 不含已退市股票。
- 执行: 每月末交易日 T 打分, T+1 **开盘价**调仓, top 6 等权(单票上限 25%),
  满仓版(不含生产 75% 现金约束——现金约束是验证期保守设计, 非策略本身)。
- 成本: 双边各 17.5bps(佣金 2.5 + 滑点 15, 同 cn-a-share-sim-cost-v1)。
- 估值: 前复权 close 作 total-return 代理(不含股息); 标普500 用 FRED SP500
  价格指数(不含股息); 沪深300 用 daily_price sh.000300 价格指数(不含股息)。
  三者口径一致(均为价格指数), 但 A 股前复权含分红再投资近似, 实际略占优。

模式(--mode):
- legacy_v3(默认): 上述三桶近似口径, v3 回归基线 0.0481/0.2671/0.4822 冻结。
- kernel: 统一策略内核阶段 2 —— 打分走 quant_strategy/kernel.py 纯函数
  内核(生产 multifactor_v3_sim.5 五桶语义: value 真 EP/BP 缺省回退 MA60
  代理 / quality / growth / momentum / risk 含 Beta), 选股与权重走内核
  weights_from_scores(非等权、risk_budget 隐含现金腿, 与 legacy 满仓等权
  是口径升级而非回归)。可选 --no-trade-band 0.02 作换手敏感度工具。

用法: python3 deploy/adhoc_year_backtest.py [--db data/finance.duckdb]
      [--mode legacy_v3|kernel] [--no-trade-band 0.02]
产物: legacy_v3 → reports/adhoc_backtest/year_compare_2022_2024.json
      kernel    → reports/adhoc_backtest/year_compare_kernel.json(不覆盖基线)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np

# 脚本直跑(python3 deploy/adhoc_year_backtest.py)时 sys.path[0] 是 deploy/,
# 需把仓库根插进来才能 import quant_strategy 内核
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_strategy import FactorConfig  # noqa: E402
from quant_strategy import kernel  # noqa: E402

COST_BPS = 17.5 / 1e4          # 单边成本(佣金2.5+滑点15)
STAMP_BPS = 5 / 1e4            # A 股印花税(仅卖出侧)
TOP_N = 6
MIN_HISTORY = 130
W_VALUE, W_MOM, W_RISK = 25 / 55, 15 / 55, 15 / 55
BENCH_LOCAL = "sh.000300"
FRED_URL = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv"
    "?id=SP500&cosd=2021-11-01&coed=2025-01-10"
)
# kernel 模式生产配置: 打分口径 = multifactor_v3_sim.5(top_n=6 /
# max_weight=0.25 / min_history=130 / min_budget=0.25), 与 runner 同源
FACTOR_CONFIG = FactorConfig()


def load_universe(con: duckdb.DuckDBPyConnection) -> list[str]:
    """与 build_walk_forward_bundle 同规则: 6 位 A 股 + 全历史 source 单一。"""
    rows = con.execute(
        "SELECT symbol, COUNT(DISTINCT source) AS sc, MIN(date) AS d0 "
        "FROM daily_price WHERE symbol ~ '^[0-9]{6}$' "
        "GROUP BY symbol HAVING sc = 1 ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def load_prices(con, symbols: list[str], start: date, end: date):
    """返回 {symbol: (dates ndarray, open, close)} 按日期升序。"""
    out = {}
    for sym in symbols:
        rows = con.execute(
            "SELECT date, open, close FROM daily_price "
            "WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
            [sym, start, end],
        ).fetchall()
        if len(rows) >= MIN_HISTORY:
            d = np.array([r[0].toordinal() for r in rows])
            o = np.array([float(r[1]) for r in rows])
            c = np.array([float(r[2]) for r in rows])
            out[sym] = (d, o, c)
    return out


def load_hl(con, symbols: list[str], start: date, end: date):
    """返回 {symbol: (dates ndarray, high, low)}——exec_gate 盘中回补判定用。"""
    out = {}
    for sym in symbols:
        rows = con.execute(
            "SELECT date, high, low FROM daily_price "
            "WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
            [sym, start, end],
        ).fetchall()
        if len(rows) >= MIN_HISTORY:
            d = np.array([r[0].toordinal() for r in rows])
            h = np.array([float(r[1]) if r[1] else np.nan for r in rows])
            lo = np.array([float(r[2]) if r[2] else np.nan for r in rows])
            out[sym] = (d, h, lo)
    return out


def load_basic(con, symbols: list[str], start: date, end: date):
    """daily_basic point-in-time: {symbol: (dates, ep, bp)}。"""
    out = {}
    for sym in symbols:
        rows = con.execute(
            "SELECT date, pe_ttm, pb FROM daily_basic "
            "WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
            [sym, start, end],
        ).fetchall()
        if not rows:
            continue
        d = np.array([r[0].toordinal() for r in rows])
        pe = np.array([float(r[1]) if r[1] else np.nan for r in rows])
        pb = np.array([float(r[2]) if r[2] else np.nan for r in rows])
        with np.errstate(divide="ignore", invalid="ignore"):
            ep = np.where(pe > 0, 1.0 / pe, np.nan)
            bp = np.where(pb > 0, 1.0 / pb, np.nan)
        out[sym] = (d, ep, bp)
    return out


def load_basic_raw(con, symbols: list[str], start: date, end: date):
    """kernel 模式专用: daily_basic **原始** (pe_ttm, pb) point-in-time 快照。

    legacy 路径的 load_basic 已把 pe/pb 反转成 EP/BP, 而内核 _value_score
    吃原始 (pe_ttm, pb) 元组(与生产 latest_daily_basic 行一致), 故单独
    加载避免 1/(1/x) 浮点回环误差。返回 {symbol: (dates, pe, pb)},
    缺失值为 nan。
    """
    out = {}
    for sym in symbols:
        rows = con.execute(
            "SELECT date, pe_ttm, pb FROM daily_basic "
            "WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
            [sym, start, end],
        ).fetchall()
        if not rows:
            continue
        d = np.array([r[0].toordinal() for r in rows])
        pe = np.array([float(r[1]) if r[1] is not None else np.nan for r in rows])
        pb = np.array([float(r[2]) if r[2] is not None else np.nan for r in rows])
        out[sym] = (d, pe, pb)
    return out


def benchmark_returns63(bench, t_ord: int):
    """按内核样板提取基准末尾 63 日收益(closes ≤ t_ord, ≥64 根 → diff/前序
    取 [-63:], 不足返回 None → 内核 Beta 分量退化中性 0.5, 与生产一致)。

    bench: (dates ordinal ndarray, closes ndarray) 或 None。
    保留生产 Beta 不按日期对齐的已知缺陷语义, 原样复刻不"顺手修复"。
    """
    if bench is None:
        return None
    bd, bc = bench
    closes = bc[: np.searchsorted(bd, t_ord, side="right")]
    if closes.size < 64:
        return None
    return (np.diff(closes) / closes[:-1])[-63:]


def pct_rank(values: dict[str, float], higher_better: bool = True) -> dict[str, float]:
    keys = list(values)
    arr = np.array([values[k] for k in keys])
    order = arr.argsort()
    ranks = np.empty(len(arr))
    ranks[order] = np.arange(len(arr))
    r = ranks / max(len(arr) - 1, 1)
    if not higher_better:
        r = 1.0 - r
    return {k: float(v) for k, v in zip(keys, r)}


TS_PCT_WINDOW = 1250   # EP 时序分位窗口（约 5 年交易日，与 research_engine 同口径）
TS_PCT_MIN_OBS = 250   # 窗口内最少有效观测


def _median_ep_ts_pct(basic, symbols, t_ord: int) -> float | None:
    """候选池中位 EP 自身时序分位（PIT：只用 ≤ t_ord 的数据）。

    basic: load_basic 的 {symbol: (dates ordinal, ep, bp)}；symbols: 当期
     eligible 候选（pack["factors"] 键）。返回 None = 有效标的不足，
    调用方跳过阀门（沿用内核原始权重，fail-open 到满仓语义由上层档位决定）。
    """
    pcts = []
    for sym in symbols:
        entry = basic.get(sym)
        if not entry:
            continue
        d, ep, _bp = entry
        i = int(np.searchsorted(d, t_ord, side="right"))
        if i == 0:
            continue
        cur = ep[i - 1]
        if not np.isfinite(cur):
            continue
        window = ep[max(0, i - TS_PCT_WINDOW):i]
        window = window[np.isfinite(window)]
        if len(window) < TS_PCT_MIN_OBS:
            continue
        pcts.append(float(np.mean(window <= cur)))
    return float(np.median(pcts)) if pcts else None


def _pool_ep_series(basic, cal):
    """池级 EP 中位水平日频序列（v2 状态口径，治 v1 自引用钝化）。

    对每个交易日取全池有效 EP 的中位数（一个标量水平值），返回
    (ords, values) 与 cal 对齐，有效标的 <20 的日记 NaN。
    v1 用「逐股 EP 分位再取中位」，池子本身是价值因子选出的便宜股，
    分位永远中枢化；v2 直接用池 EP 中位**水平**对自身历史打分。
    """
    ords = np.array(cal, dtype=int)
    vals = np.full(len(cal), np.nan)
    lookups = {}
    for sym, (d, ep, _bp) in basic.items():
        lookups[sym] = (d, ep)
    for j, t in enumerate(ords):
        eps = []
        for d, ep in lookups.values():
            i = int(np.searchsorted(d, t, side="right")) - 1
            if i >= 0 and np.isfinite(ep[i]):
                eps.append(float(ep[i]))
        if len(eps) >= 20:
            vals[j] = float(np.median(eps))
    return ords, vals


def _series_ts_pct(vals: np.ndarray, i: int) -> float | None:
    """标量序列在 i 点的自身时序分位（窗口 TS_PCT_WINDOW，最少
    TS_PCT_MIN_OBS 有效观测）。分位高 = 当前 EP 水平处于自身历史高位
    = 便宜（EP 高=便宜，方向勿再反）。"""
    cur = vals[i]
    if not np.isfinite(cur):
        return None
    window = vals[max(0, i - TS_PCT_WINDOW + 1):i + 1]
    window = window[np.isfinite(window)]
    if len(window) < TS_PCT_MIN_OBS:
        return None
    return float(np.mean(window <= cur))


def _apply_regime_valve(w: dict, budget: float) -> dict:
    """把内核权重（含 CASH）重归一到指定股票预算：等比缩放股票腿，
    CASH = 1 - budget。研究侧 overlay，不动内核语义。"""
    eq = {s: v for s, v in w.items() if s != "CASH"}
    eq_sum = sum(eq.values())
    if eq_sum <= 0:
        return w
    scale = budget / eq_sum
    out = {s: v * scale for s, v in eq.items()}
    out["CASH"] = round(max(0.0, 1.0 - budget), 6)
    return out


def _kernel_scores(t_ord: int, prices, basic_raw,
                   market_returns63) -> dict:
    """kernel 模式打分: quant_strategy/kernel.py 纯函数内核的薄适配器(阶段 2)。

    编排顺序与 tests/strategy/test_kernel_parity.py 的 run_kernel() 样板
    一致(也与 backtest_engine/v2/runner.py 适配器同源, 交叉锁定见
    tests/strategy/test_adhoc_kernel_source_lock.py):
    min_history 剔除 → score_symbol×N(point-in-time 截取 closes 尾段,
    估值取 ≤ t_ord 最新 (pe,pb) 快照或 None) → cross_sectional_scores。

    返回 {"scores", "factors", "returns63", "value_source"}: scores 即横截面
    综合分; factors/returns63/value_source 供 run_backtest 走内核
    weights_from_scores 合成权重。
    """
    config = FACTOR_CONFIG
    scored: dict[str, tuple[dict[str, float], str]] = {}
    factors: dict[str, dict[str, float]] = {}
    returns63: dict[str, np.ndarray] = {}
    for sym, (d, _o, c) in prices.items():
        idx = int(np.searchsorted(d, t_ord, side="right")) - 1
        # min_history 剔除(生产在 compute_all 循环里做, 内核由调用方负责)
        if idx + 1 < config.min_history:
            continue
        closes = c[: idx + 1]
        # 估值: ≤ t_ord 最新 daily_basic 快照 → (pe_ttm, pb) 元组或 None
        valuation = None
        if sym in basic_raw:
            bd, pe, pb = basic_raw[sym]
            bi = int(np.searchsorted(bd, t_ord, side="right")) - 1
            if bi >= 0:
                valuation = (
                    None if np.isnan(pe[bi]) else float(pe[bi]),
                    None if np.isnan(pb[bi]) else float(pb[bi]),
                )
        scored[sym] = kernel.score_symbol(
            closes, valuation, market_returns63, config
        )
        factors[sym] = scored[sym][0]
        # 相关性惩罚内存 returns 视图: 最后 63 根收盘 → 62 长度日收益
        closes63 = closes[-63:]
        returns63[sym] = np.diff(closes63) / closes63[:-1]
    if not factors:
        return {"scores": {}, "factors": {}, "returns63": {},
                "value_source": {}}
    return {
        "scores": kernel.cross_sectional_scores(factors, config),
        "factors": factors,
        "returns63": returns63,
        "value_source": kernel.build_value_source_counter(scored),
    }


def composite_scores(t_ord: int, prices, basic,
                     weights: tuple[float, float, float] = (W_VALUE, W_MOM, W_RISK),
                     panel: list | None = None,
                     mode: str = "legacy_v3",
                     basic_raw=None,
                     market_returns63=None):
    """在 ordinal 日 t 对所有满足历史要求的标的打综合分。

    mode="legacy_v3"(默认): 三桶近似口径, 返回 {symbol: 综合分}。
    mode="kernel": 走 _kernel_scores(内核五桶生产语义), 返回带 factors
    明细的结构 dict, basic_raw 为 load_basic_raw 的原始估值快照,
    market_returns63 为 benchmark_returns63 的输出(调用方注入基准)。

    2026-08-11 审查修正: 各子因子先**分别**横截面百分位再按权重合成——
    此前 EP(~0.058)与 BP(~0.491) 原始值直接平均, BP 量级 9 倍于 EP,
    价值桶实际由 BP 独断; 风险桶同理(vol/mdd/downside 量纲不同直接加总,
    mdd 主导)。

    panel: 可选输出收集器(默认 None 零开销零行为变化, 仅 legacy 模式)。
    传入列表时对每个被评分标的(common 集合)追加一条截面记录, 供可复现包
    导出逐月因子明细; 缺失子因子记 None(JSON null)。
    """
    if mode == "kernel":
        return _kernel_scores(t_ord, prices, basic_raw or {}, market_returns63)
    # frozen: v3 口径回归基线 0.0481/0.2671/0.4822，勿改
    # (以下 legacy_v3 函数体原样保留)
    ep_raw, bp_raw, mom_raw = {}, {}, {}
    vol_raw, mdd_raw, dwn_raw = {}, {}, {}
    for sym, (d, _o, c) in prices.items():
        idx = np.searchsorted(d, t_ord, side="right") - 1
        if idx < MIN_HISTORY - 1:
            continue
        hist = c[: idx + 1]
        # 动量(同为收益率量纲, 允许先合成再排名)
        r3 = hist[-1] / hist[-63] - 1 if len(hist) >= 63 else 0.0
        r6 = hist[-1] / hist[-126] - 1 if len(hist) >= 126 else 0.0
        r12 = hist[-1] / hist[-252] - 1 if len(hist) >= 252 else 0.0
        mom_raw[sym] = 0.40 * r3 + 0.35 * r6 + 0.25 * r12
        # 风险子因子(分别排名, 越低越好)
        win = hist[-63:]
        vol_raw[sym] = float(np.std(np.diff(win) / win[:-1])) if len(win) > 2 else 1.0
        w126 = hist[-126:]
        peak = np.maximum.accumulate(w126)
        mdd_raw[sym] = float(np.max((peak - w126) / peak)) if len(w126) > 1 else 1.0
        neg = np.diff(w126) / w126[:-1]
        dwn_raw[sym] = float(np.std(neg[neg < 0])) if np.any(neg < 0) else 0.0
        # 价值子因子(daily_basic 最近快照, 分别排名)
        if sym in basic:
            bd, ep, bp = basic[sym]
            bi = np.searchsorted(bd, t_ord, side="right") - 1
            if bi >= 0:
                if not np.isnan(ep[bi]):
                    ep_raw[sym] = float(ep[bi])
                if not np.isnan(bp[bi]):
                    bp_raw[sym] = float(bp[bi])
    value_any = set(ep_raw) | set(bp_raw)
    common = set(mom_raw) & value_any
    if len(common) < TOP_N + 2:
        return {}
    ep_rank = pct_rank({k: ep_raw[k] for k in common if k in ep_raw})
    bp_rank = pct_rank({k: bp_raw[k] for k in common if k in bp_raw})
    mp = pct_rank({k: mom_raw[k] for k in common})
    vol_r = pct_rank({k: vol_raw[k] for k in common}, higher_better=False)
    mdd_r = pct_rank({k: mdd_raw[k] for k in common}, higher_better=False)
    dwn_r = pct_rank({k: dwn_raw[k] for k in common}, higher_better=False)
    wv, wm, wr = weights
    out = {}
    for k in common:
        # 价值: EP/BP 可用分量的均值(缺一用一)
        comps = [r for r in (ep_rank.get(k), bp_rank.get(k)) if r is not None]
        v = sum(comps) / len(comps)
        r_ = 0.50 * vol_r[k] + 0.25 * mdd_r[k] + 0.25 * dwn_r[k]
        out[k] = wv * v + wm * mp[k] + wr * r_
        if panel is not None:
            # 只读快照记录, 不参与上方任何数值计算
            panel.append({
                "date": date.fromordinal(t_ord).isoformat(), "symbol": k,
                "ep_raw": ep_raw.get(k), "bp_raw": bp_raw.get(k),
                "mom_raw": mom_raw.get(k), "vol_raw": vol_raw.get(k),
                "mdd_raw": mdd_raw.get(k), "dwn_raw": dwn_raw.get(k),
                "ep_rank": ep_rank.get(k), "bp_rank": bp_rank.get(k),
                "mom_rank": mp.get(k), "vol_rank": vol_r.get(k),
                "mdd_rank": mdd_r.get(k), "dwn_rank": dwn_r.get(k),
                "value_score": v, "risk_score": r_, "composite": out[k],
            })
    return out


def _sealed_limit(sym: str, open_px: float,
                  prev_close: float | None,
                  day_close: float | None) -> tuple[bool, bool]:
    """开盘一字板(收盘未开板)判定: 返回 (涨停不可买, 跌停不可卖)。

    阈值近似: 主板 ±10%(9.5% 触发), 创业板/科创板 ±20%(19.5%);
    ST ±5% 未细分(候选池现无 ST)。盘中开板(收盘≠开盘)视为可成交——
    涨停买入以开盘价成交属保守口径(全天最高价买入)。
    """
    if not prev_close or not day_close or not open_px:
        return False, False
    if abs(day_close - open_px) / open_px >= 0.001:
        return False, False
    th = 0.195 if sym.startswith(("300", "688")) else 0.095
    return (open_px >= prev_close * (1 + th),
            open_px <= prev_close * (1 - th))


def run_backtest(prices, basic, start: date, end: date,
                 top_n: int = TOP_N, freq: str = "M",
                 cost: float = COST_BPS,
                 weights: tuple[float, float, float] = (W_VALUE, W_MOM, W_RISK),
                 ledger: list | None = None,
                 panel: list | None = None,
                 mode: str = "legacy_v3",
                 no_trade_band: float | None = None,
                 basic_raw=None,
                 bench=None,
                 regime_valve: float | None = None,
                 valve_log: list | None = None,
                 valve_state: str = "stock",
                 pool_series=None,
                 limit_guard: bool = False,
                 limit_log: list | None = None,
                 exec_gate: float | None = None,
                 hl=None,
                 gate_log: list | None = None):
    """月末(或季末)打分 T+1 开盘调仓, 返回 [(date, nav)]。

    mode="legacy_v3"(默认): 满仓 top_n 等权(执行逻辑一字不动)。
    mode="kernel": 打分走内核五桶语义, 选股与权重走内核
    weights_from_scores——非等权(conviction×相关性惩罚/下行波动加权,
    单票上限 max_weight)、risk_budget∈[min_budget,1.0] 隐含 CASH 现金腿
    (非满仓); 目标市值=权重×总资产, 差额换手成本与停牌结转口径同 legacy。
    no_trade_band 非 None 时相邻两期目标权重先过内核 apply_no_trade_band
    (默认 None 不接, 仅作换手敏感度工具)。
    basic_raw: load_basic_raw 的原始估值快照; bench: (dates, closes) 基准
    数组, kernel 模式必需(legacy 模式忽略这两个参数)。
    regime_valve: 仅 kernel 模式, 非 None 时启用 P3 市场状态阀门——
    每期按 EP 便宜度分位 pct 计算股票预算 budget = floor+(1-floor)*pct
    (EP 时序分位高=便宜→加仓; 2026-08-29 修正: v1 曾把方向做反成
    贵→加仓, 见 year_compare_kernel_valve_2025.json 负结果);
    valve_state="stock"(默认)=候选池中位逐股 EP 分位, "pool"=池级 EP
    中位水平的自身时序分位(需 pool_series);
    valve_log 非 None 时逐期追加 {date, ep_pct, budget}。
    limit_guard(2026-08-29, 默认 False 零行为变化): 涨跌停一字板执行
    约束——开盘即封板(收盘未开板)时涨停不可买(该 sleeve 资金留现金)、
    跌停不可卖(按停牌同口径结转); 盘中开板视为可成交(涨停买入按开盘价
    成交已是保守口径)。limit_log 非 None 时追加 {exec_date, symbol,
    blocked} 事件。
    exec_gate(2026-08-29, 仅 kernel 模式, 默认 None 不接): 模拟盘真实
    执行链的 ±2% 偏离闸门建模——信号日收盘为决策价, T+1 开盘价相对
    决策价偏离超 gate 的委托被拒; 当日盘中价重回闸门带内([low,high]
    与 [决策价×(1-gate), 决策价×(1+gate)] 有交集)按闸门边界价回补成交
    (保守口径), 全天未回带 → 漏单(买入留现金/卖出结转下月)。
    需配合 hl=load_hl(...) 传入日内高低价; gate_log 非 None 时追加
    {exec_date, symbol, side, dev_open, outcome} 事件(outcome=
    RECOVERY/MISSED)。

    ledger: 可选逐笔账本收集器(默认 None 零行为变化, 仅 legacy 模式)。
    传入列表时在每次 T+1 调仓执行处按**实际成交股数差额**追加
    {exec_date, symbol, side, shares, price, notional, fee, price_source}
    ——BUY/SELL 按新旧持仓股数差额, 停牌结转记 side=CARRY(股数不变、
    fee=0、估值价=最近收盘)。
    panel: 可选因子截面收集器(仅 legacy 模式), 原样透传给 composite_scores。
    两者均为纯记录, 不改变任何净值数值路径。
    """
    cal = sorted({int(d[0][i]) for d in prices.values() for i in range(len(d[0]))})
    cal = [t for t in cal if start.toordinal() <= t <= end.toordinal()]
    # 月末/季末交易日
    rebal_days = []
    for i, t in enumerate(cal):
        dt = date.fromordinal(t)
        nxt = date.fromordinal(cal[i + 1]) if i + 1 < len(cal) else None
        if nxt is None:
            break
        if nxt.month != dt.month and (freq == "M" or dt.month % 3 == 0):
            rebal_days.append(t)
    holdings: dict[str, float] = {}   # symbol -> 股数
    cash = 1.0
    nav_series = []
    pending_rebal = None
    pending_decision = None           # exec_gate: 信号日各标的收盘(决策价)
    prev_target_weights = None        # kernel 模式 no_trade_band 的上一批目标权重
    for t in cal:
        dt = date.fromordinal(t)
        # T+1 开盘执行
        if pending_rebal is not None:
            target = pending_rebal
            pending_rebal = None
            decision_px = pending_decision
            pending_decision = None
            price_map = {}
            last_close = {}
            day_close: dict[str, float] = {}
            prev_close: dict[str, float] = {}
            # kernel 模式 target 为含 CASH 键的权重 dict, legacy 为标的 list
            target_syms = ([s for s in target if s != "CASH"]
                           if mode == "kernel" else target)
            for sym in set(list(holdings) + target_syms):
                d, o, c = prices[sym]
                i = np.searchsorted(d, t)
                if i < len(d) and d[i] == t:
                    price_map[sym] = float(o[i])
                    day_close[sym] = float(c[i])
                    if i >= 1:
                        prev_close[sym] = float(c[i - 1])
                j = np.searchsorted(d, t, side="right") - 1
                if j >= 0:
                    last_close[sym] = float(c[j])
            # 涨跌停一字板执行约束(limit_guard): 开盘封板且收盘未开板
            # → 跌停持仓卖不出(移出 price_map 走停牌结转口径)、
            # 涨停目标买不进(blocked_buy, 该 sleeve 资金留现金)
            blocked_buy: set[str] = set()
            if limit_guard:
                for sym in list(price_map):
                    bb, bs_ = _sealed_limit(sym, price_map[sym],
                                            prev_close.get(sym),
                                            day_close.get(sym))
                    if bs_ and sym in holdings:
                        del price_map[sym]
                        if limit_log is not None:
                            limit_log.append({"exec_date": dt.isoformat(),
                                              "symbol": sym, "blocked": "SELL"})
                    elif bb:
                        blocked_buy.add(sym)
                        if limit_log is not None:
                            limit_log.append({"exec_date": dt.isoformat(),
                                              "symbol": sym, "blocked": "BUY"})
            # 偏离闸门建模(exec_gate, 仅 kernel): 开盘价相对信号日决策价
            # (月末收盘)偏离超 ±gate 的委托拒单; 当日盘中重回闸门带内按
            # 闸门边界价回补成交, 全天未回带 → 漏单(新进买入留现金,
            # 卖出/调减按停牌口径结转持仓)
            if exec_gate is not None and mode == "kernel" and decision_px:
                total_est = cash + sum(
                    q * price_map.get(s, last_close.get(s, 0.0))
                    for s, q in holdings.items())
                for sym in list(price_map):
                    dp = decision_px.get(sym)
                    if not dp or dp <= 0:
                        continue
                    open_px = price_map[sym]
                    dev = open_px / dp - 1.0
                    if abs(dev) <= exec_gate:
                        continue
                    if sym in holdings and sym not in target_syms:
                        side = "SELL"
                    elif sym in target_syms and sym not in holdings:
                        side = "BUY"
                    else:
                        cur = holdings.get(sym, 0.0) * open_px
                        tgt = float(target.get(sym, 0.0)) * total_est
                        side = "BUY" if tgt > cur else "SELL"
                    hi = lo = None
                    if hl is not None and sym in hl:
                        hd, hh, ll = hl[sym]
                        k = int(np.searchsorted(hd, t))
                        if k < len(hd) and hd[k] == t:
                            hi, lo = float(hh[k]), float(ll[k])
                    band_lo = dp * (1 - exec_gate)
                    band_hi = dp * (1 + exec_gate)
                    fill = None
                    if hi is not None and lo is not None:
                        if dev > exec_gate and lo <= band_hi:
                            fill = band_hi
                        elif dev < -exec_gate and hi >= band_lo:
                            fill = band_lo
                    outcome = "RECOVERY" if fill is not None else "MISSED"
                    if fill is not None:
                        price_map[sym] = fill
                    elif side == "BUY" and sym not in holdings:
                        blocked_buy.add(sym)
                    else:
                        del price_map[sym]  # 卖出/调减漏单 → 停牌口径结转
                    if gate_log is not None:
                        gate_log.append({
                            "exec_date": dt.isoformat(), "symbol": sym,
                            "side": side, "dev_open": round(dev, 4),
                            "outcome": outcome,
                        })
            # 停牌持仓: 无当日开盘, 无法卖出, 原样结转(按最近收盘估值)
            carried = {s: q for s, q in holdings.items() if s not in price_map}
            carried_val = sum(q * last_close.get(s, 0.0) for s, q in carried.items())
            total = cash + carried_val + sum(
                q * price_map[s] for s, q in holdings.items() if s in price_map
            )
            if mode == "kernel":
                # kernel 执行: 目标市值 = 权重 × 总资产(含 CASH 现金腿,
                # Σw = risk_budget ≤ 1, 非满仓非等权——与 legacy 满仓等权
                # 是口径差异, 属生产语义升级)。差额换手成本/印花税/停牌
                # 结转口径与 legacy 一致。ledger/panel 仅 legacy 模式支持。
                buyable = [s for s in target_syms if s in price_map
                           and s not in blocked_buy]
                tgt_val = {s: float(target[s]) * total for s in buyable}
                buy_notional = sell_notional = 0.0
                for s, q in holdings.items():
                    if s not in price_map:
                        continue
                    cur = q * price_map[s]
                    delta = tgt_val.get(s, 0.0) - cur
                    if delta >= 0:
                        buy_notional += delta
                    else:
                        sell_notional += -delta
                for s in buyable:
                    if s not in holdings:
                        buy_notional += tgt_val[s]
                fee = ((buy_notional + sell_notional) * cost
                       + sell_notional * STAMP_BPS)
                # 费用从现金侧计提: 等比收缩买入市值, 保证现金不为负
                invest = sum(tgt_val.values())
                scale = 1.0
                if invest > 0:
                    scale = min(1.0, (total - carried_val - fee) / invest)
                    scale = max(scale, 0.0)
                new_holdings = dict(carried)
                for s in buyable:
                    new_holdings[s] = tgt_val[s] * scale / price_map[s]
                leftover = (total - carried_val - fee
                            - sum(v * scale for v in tgt_val.values()))
                holdings = new_holdings
                cash = max(leftover, 0.0)
            else:
                # frozen: v3 口径回归基线 0.0481/0.2671/0.4822，勿改
                # (以下 legacy_v3 执行块语句原样保留, 仅 else 缩进)
                buyable = [s for s in target if s in price_map
                           and s not in blocked_buy]
                n = max(len(buyable), 1)
                per = (total - carried_val) / n
                # 差额换手(2026-08-11 审查修正): 只对 |目标市值-现值| 收取成本,
                # 未变动持仓不再虚构全额双边换手; 卖出侧加印花税 STAMP_BPS。
                buy_notional = sell_notional = 0.0
                for s, q in holdings.items():
                    if s not in price_map:
                        continue
                    cur = q * price_map[s]
                    tgt = per if s in buyable else 0.0
                    delta = tgt - cur
                    if delta >= 0:
                        buy_notional += delta
                    else:
                        sell_notional += -delta
                for s in buyable:
                    if s not in holdings:
                        buy_notional += per
                fee = (buy_notional + sell_notional) * cost + sell_notional * STAMP_BPS
                budget = (total - carried_val - fee) / n
                new_holdings = dict(carried)
                for s in buyable:
                    new_holdings[s] = budget / price_map[s]
                leftover = total - carried_val - fee - budget * len(buyable)
                if ledger is not None:
                    # 逐笔账本(仅追加记录, 不参与任何数值计算):
                    # 1) 停牌结转持仓
                    for s, q in carried.items():
                        px = last_close.get(s, 0.0)
                        ledger.append({
                            "exec_date": dt.isoformat(), "symbol": s, "side": "CARRY",
                            "shares": q, "price": px, "notional": q * px,
                            "fee": 0.0, "price_source": "last_close",
                        })
                    # 2) 可交易持仓的股数差额(新-旧, 正=BUY 负=SELL)
                    for s, q in holdings.items():
                        if s not in price_map:
                            continue
                        dq = new_holdings.get(s, 0.0) - q
                        if abs(dq) > 1e-12:
                            side = "BUY" if dq > 0 else "SELL"
                            notional = abs(dq) * price_map[s]
                            ledger.append({
                                "exec_date": dt.isoformat(), "symbol": s,
                                "side": side, "shares": abs(dq),
                                "price": price_map[s], "notional": notional,
                                "fee": notional * (cost if dq > 0
                                                   else cost + STAMP_BPS),
                                "price_source": "open",
                            })
                    # 3) 新进持仓(旧账本中没有的标的)
                    for s in buyable:
                        if s not in holdings:
                            q_new = new_holdings[s]
                            notional = q_new * price_map[s]
                            ledger.append({
                                "exec_date": dt.isoformat(), "symbol": s,
                                "side": "BUY", "shares": q_new,
                                "price": price_map[s], "notional": notional,
                                "fee": notional * cost, "price_source": "open",
                            })
                holdings = new_holdings
                cash = max(leftover, 0.0)
        # 收盘估值
        nav = cash
        for s, q in holdings.items():
            d, _o, c = prices[s]
            i = np.searchsorted(d, t, side="right") - 1
            if i >= 0:
                nav += q * float(c[i])
        nav_series.append((dt, nav))
        # 月末打分, 次日开盘调仓
        if t in rebal_days and t != cal[-1]:
            if mode == "kernel":
                pack = composite_scores(
                    t, prices, basic, mode="kernel", basic_raw=basic_raw,
                    market_returns63=benchmark_returns63(bench, t),
                )
                if pack["scores"]:
                    result = kernel.weights_from_scores(
                        pack["scores"], pack["factors"], pack["returns63"],
                        FACTOR_CONFIG, value_source=pack["value_source"],
                    )
                    w = result["weights"]
                    if no_trade_band is not None:
                        # 换手敏感度工具: 相邻两期目标权重过内核缓冲带
                        w, _kept = kernel.apply_no_trade_band(
                            w, prev_target_weights, no_trade_band
                        )
                    if regime_valve is not None:
                        if valve_state == "pool" and pool_series is not None:
                            _po, _pv = pool_series
                            _i = int(np.searchsorted(_po, t, side="right")) - 1
                            pct = (_series_ts_pct(_pv, _i) if _i >= 0 else None)
                        else:
                            # 逐股 EP 分位取池中位（分位高=便宜）
                            pct = _median_ep_ts_pct(
                                basic, list(pack["factors"]), t)
                        if pct is not None:
                            pct = float(np.clip(pct, 0.0, 1.0))
                            # 便宜→加仓：budget 随便宜度分位单调升
                            budget = regime_valve + (1.0 - regime_valve) * pct
                            w = _apply_regime_valve(w, budget)
                            if valve_log is not None:
                                valve_log.append({
                                    "date": date.fromordinal(t).isoformat(),
                                    "ep_pct": round(pct, 4),
                                    "budget": round(budget, 4),
                                })
                    prev_target_weights = w
                    pending_rebal = w
                    if exec_gate is not None:
                        # 决策价 = 信号日(月末)收盘, 与生产 sim_targets.prices
                        # 同口径(偏离闸门基准)
                        pending_decision = {}
                        for sym in set(list(holdings)
                                       + [s for s in w if s != "CASH"]):
                            d2, _o2, c2 = prices[sym]
                            ii = int(np.searchsorted(d2, t, side="right")) - 1
                            if ii >= 0:
                                pending_decision[sym] = float(c2[ii])
            else:
                # frozen: v3 口径回归基线 0.0481/0.2671/0.4822，勿改
                scores = composite_scores(t, prices, basic, weights, panel=panel)
                if scores:
                    top = sorted(scores, key=scores.get, reverse=True)[:top_n]
                    pending_rebal = top
    return nav_series


def annual_stats(nav_series, years=(2022, 2023, 2024)):
    out = {}
    for year in years:
        pts = [(d, n) for d, n in nav_series if d.year == year]
        if len(pts) < 20:
            continue
        # 上年末净值作基期
        prev = [n for d, n in nav_series if d < pts[0][0]]
        base = prev[-1] if prev else pts[0][1]
        ret = pts[-1][1] / base - 1
        peak, mdd = base, 0.0
        for _d, n in pts:
            peak = max(peak, n)
            mdd = min(mdd, n / peak - 1)
        # 日频年化夏普(rf=0 口径, 另报 rf=2% 修正)
        navs = np.array([base] + [n for _d, n in pts])
        dr = np.diff(navs) / navs[:-1]
        vol = float(np.std(dr) * np.sqrt(252))
        ann = float(np.mean(dr) * 252)
        sharpe0 = ann / vol if vol > 0 else 0.0
        sharpe2 = (ann - 0.02) / vol if vol > 0 else 0.0
        out[year] = {
            "return": round(ret, 4), "max_drawdown": round(mdd, 4),
            "ann_vol": round(vol, 4),
            "sharpe_rf0": round(sharpe0, 2), "sharpe_rf2": round(sharpe2, 2),
        }
    return out


def fetch_sp500(cache: Path = Path("data/external/sp500_fred.csv")) -> dict[date, float]:
    """读 FRED SP500 本地缓存(宿主机 curl 预下载, urllib/curl 在部分
    Python 进程环境下被代理干扰)。"""
    out = {}
    for line in cache.read_text().splitlines()[1:]:
        d, v = line.strip().split(",")
        if v:
            out[date.fromisoformat(d)] = float(v)
    return out


def bench_annual(series: dict[date, float], years=(2022, 2023, 2024)) -> dict[int, dict]:
    out = {}
    for year in years:
        pts = sorted((d, v) for d, v in series.items() if d.year == year)
        prev = sorted((d, v) for d, v in series.items() if d.year < year)
        if not pts or not prev:
            continue
        base = prev[-1][1]
        ret = pts[-1][1] / base - 1
        peak, mdd = base, 0.0
        for _d, v in pts:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        navs = np.array([base] + [v for _d, v in pts])
        dr = np.diff(navs) / navs[:-1]
        vol = float(np.std(dr) * np.sqrt(252))
        ann = float(np.mean(dr) * 252)
        sharpe0 = ann / vol if vol > 0 else 0.0
        sharpe2 = (ann - 0.02) / vol if vol > 0 else 0.0
        out[year] = {
            "return": round(ret, 4), "max_drawdown": round(mdd, 4),
            "ann_vol": round(vol, 4),
            "sharpe_rf0": round(sharpe0, 2), "sharpe_rf2": round(sharpe2, 2),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/finance.duckdb")
    ap.add_argument("--out", default=None,
                    help="缺省按 --mode 选择: legacy_v3 → year_compare_2022_2024.json;"
                         " kernel → year_compare_kernel.json(不覆盖 v3 基线)")
    ap.add_argument("--mode", choices=["legacy_v3", "kernel"],
                    default="legacy_v3",
                    help="打分/权重口径, 默认 legacy_v3(v3 回归基线)")
    ap.add_argument("--no-trade-band", type=float, default=None,
                    help="仅 kernel 模式: 相邻两期目标权重过内核 "
                         "apply_no_trade_band(如 0.02), 默认 None 不接")
    ap.add_argument("--end-year", type=int, default=2024,
                    help="评价区间末年(含), 默认 2024; 起始年固定 2021"
                         "(2022 起计收益, 2020 起 warm-up)")
    ap.add_argument("--kernel-min-budget", type=float, default=None,
                    help="仅 kernel 模式: 覆盖 FactorConfig.min_budget"
                         "(如 1.0 = 满仓内核变体, 隔离现金腿影响); "
                         "默认 None 用生产值 0.25")
    ap.add_argument("--regime-valve", type=float, default=None, metavar="FLOOR",
                    help="仅 kernel 模式: P3 市场状态阀门, EP 便宜度分位→"
                         "股票预算 floor+(1-floor)*pct(便宜→加仓); "
                         "FLOOR 为预算下限(如 0.3), 默认 None 不接")
    ap.add_argument("--valve-state", choices=["stock", "pool"], default="stock",
                    help="阀门状态口径: stock=候选池中位逐股EP分位(默认); "
                         "pool=池级EP中位水平的自身时序分位(治自引用钝化)")
    ap.add_argument("--limit-guard", action="store_true",
                    help="涨跌停一字板执行约束: 开盘封板且收盘未开板时"
                         "涨停不可买/跌停不可卖, 默认关(零行为变化)")
    ap.add_argument("--exec-gate", type=float, default=None, metavar="GATE",
                    help="仅 kernel 模式: 偏离闸门建模(如 0.02=±2%%), 开盘价"
                         "偏离信号日决策价超闸门→拒单, 盘中回带按边界价回补,"
                         "全天未回带→漏单; 需加载日内高低价, 默认 None 不接")
    args = ap.parse_args()

    if args.kernel_min_budget is not None:
        global FACTOR_CONFIG
        FACTOR_CONFIG = replace(FACTOR_CONFIG,
                                min_budget=args.kernel_min_budget)
        print(f"kernel min_budget 覆盖为 {args.kernel_min_budget}"
              f"(生产默认 0.25, 仅本进程生效)")

    start, end = date(2021, 1, 1), date(args.end_year, 12, 31)
    years = tuple(range(2022, args.end_year + 1))
    load_start = date(2020, 1, 1)  # ≥252 交易日 warm-up(12m 动量); 交易仍从 start 起
    con = duckdb.connect(args.db, read_only=True)
    universe = load_universe(con)
    print(f"宇宙: {len(universe)} 只(source 单一 6 位 A 股)")
    prices = load_prices(con, universe, load_start, end)
    print(f"价格满足最小历史: {len(prices)} 只")
    basic = load_basic(con, list(prices), load_start, end)
    print(f"有 daily_basic 估值: {len(basic)} 只")
    basic_raw = None
    if args.mode == "kernel":
        basic_raw = load_basic_raw(con, list(prices), load_start, end)
        print(f"kernel 模式原始估值快照: {len(basic_raw)} 只")
    # kernel 模式需要基准 63 日收益(Beta 分量), 查询区间扩到 load_start;
    # legacy 口径下 bench_annual 按年过滤, 数值不受影响
    bench_rows = con.execute(
        "SELECT date, close FROM daily_price WHERE symbol=? AND date BETWEEN ? AND ? "
        "ORDER BY date", [BENCH_LOCAL, load_start, end],
    ).fetchall()
    con.close()
    bench = (np.array([r[0].toordinal() for r in bench_rows]),
             np.array([float(r[1]) for r in bench_rows]))

    valve_log: list | None = [] if args.regime_valve is not None else None
    limit_log: list | None = [] if args.limit_guard else None
    gate_log: list | None = [] if args.exec_gate is not None else None
    hl = None
    if args.exec_gate is not None:
        con = duckdb.connect(args.db, read_only=True)
        hl = load_hl(con, list(prices), load_start, end)
        con.close()
        print(f"exec_gate ±{args.exec_gate}: 日内高低价已加载 {len(hl)} 只")
    pool_series = None
    if args.regime_valve is not None and args.valve_state == "pool":
        cal = sorted({int(d[0][i]) for d in prices.values()
                      for i in range(len(d[0]))})
        pool_series = _pool_ep_series(basic, cal)
        print(f"池级 EP 中位序列: {len(cal)} 日")
    nav = run_backtest(prices, basic, start, end, mode=args.mode,
                       no_trade_band=args.no_trade_band,
                       basic_raw=basic_raw, bench=bench,
                       regime_valve=args.regime_valve,
                       valve_log=valve_log,
                       valve_state=args.valve_state,
                       pool_series=pool_series,
                       limit_guard=args.limit_guard,
                       limit_log=limit_log,
                       exec_gate=args.exec_gate,
                       hl=hl,
                       gate_log=gate_log)
    strat = annual_stats(nav, years)
    csi300 = bench_annual({d: float(c) for d, c, in bench_rows}, years)
    sp500 = bench_annual(fetch_sp500(), years)

    if args.mode == "kernel":
        disclaimer = ("一次性对照回测(kernel 模式), 非治理证据; 幸存者偏差; "
                      "打分=生产 multifactor_v3_sim.5 五桶语义"
                      "(quality/growth 为纯价格代理); 非满仓(内核 risk_budget "
                      "隐含现金腿)+非等权(conviction/相关性/下行波动加权), "
                      "与 legacy_v3 满仓等权三桶是口径升级而非回归; "
                      "价格指数不含股息")
        if args.kernel_min_budget is not None:
            disclaimer += (f"; min_budget 覆盖为 {args.kernel_min_budget}"
                           f"(满仓内核变体, 隔离现金腿影响)")
    else:
        disclaimer = ("一次性对照回测, 非治理证据; 幸存者偏差; 质量/成长因子桶剔除; "
                      "满仓口径(不含生产75%现金约束); 价格指数不含股息")
    result = {
        "schema": "foundf.adhoc_year_compare.v1",
        "mode": args.mode,
        "disclaimer": disclaimer,
        "universe_size": len(prices),
        "cost_bps_per_side": 17.5,
        "rebalance": "月末T+1开盘",
        "strategy": strat,
        "csi300": csi300,
        "sp500": sp500,
        "nav_end": {y: round([n for d, n in nav if d.year == y][-1], 4)
                    for y in years},
    }
    if args.mode == "kernel" and args.no_trade_band is not None:
        result["no_trade_band"] = args.no_trade_band
    if args.mode == "kernel" and args.kernel_min_budget is not None:
        result["kernel_min_budget"] = args.kernel_min_budget
    if args.mode == "kernel" and args.regime_valve is not None:
        budgets = [v["budget"] for v in (valve_log or [])]
        result["regime_valve"] = {
            "floor": args.regime_valve,
            "state": args.valve_state,
            "rule": "budget = floor + (1-floor) * EP便宜度时序分位(便宜→加仓)",
            "periods": len(budgets),
            "avg_budget": round(sum(budgets) / len(budgets), 4) if budgets else None,
            "log": valve_log,
        }
        result["disclaimer"] += ("; 启用 P3 状态阀门(研究 overlay, "
                                 "EP 时序分位动态预算)")
    if args.limit_guard:
        result["limit_guard"] = {
            "rule": "开盘一字板(收盘未开板): 涨停不可买/跌停不可卖(结转)",
            "blocked_events": limit_log,
        }
        result["disclaimer"] += "; 启用涨跌停一字板执行约束"
    if args.exec_gate is not None:
        from collections import Counter
        outcomes = Counter(
            (e["side"], e["outcome"]) for e in (gate_log or []))
        result["exec_gate"] = {
            "gate": args.exec_gate,
            "rule": "开盘价偏离信号日决策价超闸门→拒单; 盘中回带按边界价"
                    "回补; 全天未回带→漏单(买留现金/卖结转)",
            "gated_events": len(gate_log or []),
            "by_outcome": {f"{s}_{o}": n for (s, o), n in sorted(outcomes.items())},
            "log": gate_log,
        }
        result["disclaimer"] += ("; 启用偏离闸门执行建模(模拟盘真实执行链 "
                                 "±2% 口径)")
    default_out = ("reports/adhoc_backtest/year_compare_kernel.json"
                   if args.mode == "kernel"
                   else "reports/adhoc_backtest/year_compare_2022_2024.json")
    out = Path(args.out or default_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
