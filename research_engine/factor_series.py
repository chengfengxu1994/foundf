"""
factor_series.py — 历史时点因子序列计算。

IC 研究需要"时点因子值"：在每个采样日 t，只用 t 之前的数据计算因子，
避免未来函数。与 factor_engine 的区别：factor_engine 算"今天"的快照，
本模块算历史每一个采样日的横截面序列。

重要：Quality/Growth 保留价格代理（PROXY）作对照；Value 已于
2026-08-06 接入真实估值源：tushare daily_basic → EP（1/PE_TTM）与
BP（1/PB），经 _load_fundamental_inputs 与价格日期对齐（缺失日记
NaN，负 PE/PB 的亏损股不参与价值排序）。2026-08-07 新增三个研究口径
因子：ep_ts_pct/bp_ts_pct（个股 EP/BP 自身 5 年时序分位锚，
识别绝对贵贱）与 ep_stability（EP 变异系数取负，过滤周期股
利润顶部的假便宜）。2026-08-28 财报回填（baostock 季度接口）后新增
真基本面因子：quality_roe_real（ROE）、accrual_cfo_np（经营现金流/
净利润）、growth_profit_yoy（净利润同比），经 _load_statement_inputs
按 filed_at（真实披露日）做 PIT 对齐——每个交易日只可见 filed_at
早于当日的最新一期财报。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

# 因子定义：每个函数接收 (prices, i)，prices 为升序收盘价格数组，
# i 为当前时点索引，只可使用 prices[:i+1]。返回原始因子值（IC 基于排序，
# 单调变换不影响 RankIC）。
LOOKBACK = 252  # 最长回看窗口


def _ret(px: np.ndarray, i: int, days: int) -> float | None:
    if i < days:
        return None
    base = px[i - days]
    return float(px[i] / base - 1) if base > 0 else None


def _momentum_3m(px: np.ndarray, i: int) -> float | None:
    return _ret(px, i, 63)


def _momentum_6m(px: np.ndarray, i: int) -> float | None:
    return _ret(px, i, 126)


def _momentum_12m(px: np.ndarray, i: int) -> float | None:
    return _ret(px, i, 252)


def _low_volatility(px: np.ndarray, i: int) -> float | None:
    """低波动因子：63 日日收益波动率取负（越高 = 波动越低）。"""
    if i < 63:
        return None
    window = px[i - 63:i + 1]
    rets = np.diff(window) / window[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) < 30:
        return None
    return float(-np.std(rets) * np.sqrt(252))


def _value_proxy(px: np.ndarray, i: int) -> float | None:
    """价值代理：年线偏离度 MA252/Price - 1（越高 = 相对年线越便宜）。

    PROXY — 真正的价值因子（PE/PB/EV-EBITDA）依赖财务数据。
    """
    if i < 252:
        return None
    ma = float(np.mean(px[i - 251:i + 1]))
    return float(ma / px[i] - 1) if px[i] > 0 else None


def _quality_proxy(px: np.ndarray, i: int) -> float | None:
    """质量代理：近 12 个月月度正收益比例（越高 = 走势质量越稳）。

    PROXY — 真正的质量因子（ROE/ROIC/毛利率）依赖财务数据。
    """
    if i < 252:
        return None
    rets = np.diff(px[i - 251:i + 1]) / px[i - 251:i]
    monthly = [float(np.prod(1 + rets[j:j + 21]) - 1)
               for j in range(0, len(rets), 21) if len(rets[j:j + 21]) > 0]
    if not monthly:
        return None
    return float(np.mean(np.array(monthly) > 0))


def _growth_proxy(px: np.ndarray, i: int) -> float | None:
    """成长代理：12M 收益剔除最近 1 个月（长期趋势，降低短期反转污染）。

    PROXY — 真正的成长因子（营收/利润增速）依赖财务数据。
    """
    if i < 252:
        return None
    base = px[i - 252]
    end = px[i - 21] if i >= 273 else px[i]
    return float(end / base - 1) if base > 0 else None


def _level(arr: np.ndarray, i: int) -> float | None:
    """基本面水平因子：直接取对齐序列当前值（EP/BP 等，IC 基于排序，
    水平值即因子值）。缺失（NaN）返回 None。"""
    v = arr[i]
    return float(v) if np.isfinite(v) else None


def _neg_level(arr: np.ndarray, i: int) -> float | None:
    """取负的水平因子（低换手溢价：越高 = 换手越低）。"""
    v = _level(arr, i)
    return -v if v is not None else None


TS_PCT_WINDOW = 1250        # 时序分位滚动窗口（约 5 年交易日）
TS_PCT_MIN_OBS = 250        # 窗口内最少有效观测数
STABILITY_WINDOW = 250      # 盈利稳定性回看窗口（约 1 年交易日）
STABILITY_MAX_NAN_RATIO = 0.30  # 窗口内缺失占比上限，超过记 NULL


def _ts_pct(arr: np.ndarray, i: int) -> float | None:
    """时序分位锚：当前值在自身过去 5 年（1250 交易日）窗口有效观测中
    的分位（0~1，越高 = 相对自身历史越便宜）。

    与纯截面分位互补：截面分位永远能选出"相对最便宜"，
    时序分位回答"相对自身历史贵不贵"。窗口内有效观测不足
    TS_PCT_MIN_OBS 或当前值缺失（如 pe_ttm<=0 亏损日）时返回 None。
    """
    cur = arr[i]
    if not np.isfinite(cur):
        return None
    lo = max(0, i - TS_PCT_WINDOW + 1)
    window = arr[lo:i + 1]
    window = window[np.isfinite(window)]
    if len(window) < TS_PCT_MIN_OBS:
        return None
    return float(np.mean(window <= cur))


def _ep_ts_pct(arr: np.ndarray, i: int) -> float | None:
    return _ts_pct(arr, i)


def _bp_ts_pct(arr: np.ndarray, i: int) -> float | None:
    return _ts_pct(arr, i)


def _ep_stability(arr: np.ndarray, i: int) -> float | None:
    """盈利稳定性：过去 250 交易日 EP 变异系数取负（越高 = 盈利越稳）。

    周期股陷阱过滤：航运类"利润顶部 EP 最高的假便宜"对应 EP 剧烈波动、
    CV 高，取负后得分低被排到价值排序末尾。均值 <=0 或窗口内缺失
    占比超 30% 时返回 None（不参与截面）。
    """
    if i < STABILITY_WINDOW - 1:
        return None
    window = arr[i - STABILITY_WINDOW + 1:i + 1]
    # 直接统计缺失占比: 1-mean(isfinite) 有浮点误差(0.3 边界误判)
    if float(np.mean(~np.isfinite(window))) > STABILITY_MAX_NAN_RATIO:
        return None
    valid = window[np.isfinite(window)]
    mean = float(np.mean(valid))
    if mean <= 0:
        return None
    return float(-np.std(valid) / mean)


# name → (函数, 类别, 是否价格代理, 输入序列键)
# 输入序列键: "close"=收盘价序列; "ep"=盈利收益率 1/PE_TTM;
#             "bp"=账面市值比 1/PB; "turnover"=换手率%（均与价格日期对齐）;
#             "roe"/"cfo_to_np"/"profit_growth"=财报序列（按 filed_at PIT 对齐）
FACTOR_DEFS: dict[str, tuple[Any, str, bool, str]] = {
    "momentum_3m": (_momentum_3m, "Momentum", False, "close"),
    "momentum_6m": (_momentum_6m, "Momentum", False, "close"),
    "momentum_12m": (_momentum_12m, "Momentum", False, "close"),
    "low_volatility": (_low_volatility, "LowVol", False, "close"),
    "value_proxy": (_value_proxy, "Value", True, "close"),
    "quality_proxy": (_quality_proxy, "Quality", True, "close"),
    "growth_proxy": (_growth_proxy, "Growth", True, "close"),
    "value_ep": (_level, "Value", False, "ep"),
    "value_bp": (_level, "Value", False, "bp"),
    "low_turnover": (_neg_level, "Liquidity", False, "turnover"),
    "ep_ts_pct": (_ep_ts_pct, "Value", False, "ep"),
    "bp_ts_pct": (_bp_ts_pct, "Value", False, "bp"),
    "ep_stability": (_ep_stability, "Quality", False, "ep"),
    "quality_roe_real": (_level, "Quality", False, "roe"),
    "accrual_cfo_np": (_level, "Quality", False, "cfo_to_np"),
    "growth_profit_yoy": (_level, "Growth", False, "profit_growth"),
}


class FactorSeriesBuilder:
    """从 daily_price 构建历史时点因子序列。

    返回结构:
        {
            "dates": [date, ...],                      # 采样日（每月首个交易日）
            "symbols": [str, ...],
            "prices": {symbol: np.ndarray},            # 全历史收盘价（升序）
            "price_index": {symbol: {date: idx}},
            "factor_values": {factor: {date: {symbol: value}}},
        }
    """

    def __init__(self, warehouse):
        self.warehouse = warehouse

    def build(self, start_date: date | None = None,
              end_date: date | None = None) -> dict[str, Any]:
        rows = self.warehouse.query(
            "SELECT symbol, date, close FROM daily_price ORDER BY symbol, date")
        if not rows:
            return {"dates": [], "symbols": [], "prices": {},
                    "price_index": {}, "factor_values": {}}

        # 组织价格序列
        by_symbol: dict[str, list[tuple[date, float]]] = {}
        for r in rows:
            d = r["date"]
            if isinstance(d, str):
                d = date.fromisoformat(d)
            by_symbol.setdefault(r["symbol"], []).append((d, float(r["close"])))

        prices: dict[str, np.ndarray] = {}
        price_dates: dict[str, list[date]] = {}
        price_index: dict[str, dict[date, int]] = {}
        all_dates: set[date] = set()
        for sym, series in by_symbol.items():
            series.sort(key=lambda x: x[0])
            price_dates[sym] = [d for d, _ in series]
            prices[sym] = np.array([c for _, c in series], dtype=float)
            price_index[sym] = {d: i for i, d in enumerate(price_dates[sym])}
            all_dates.update(price_dates[sym])

        # 采样日：每月首个交易日（全市场日历的并集）
        sorted_dates = sorted(all_dates)
        sample_dates = []
        seen_months: set[tuple[int, int]] = set()
        for d in sorted_dates:
            key = (d.year, d.month)
            if key not in seen_months:
                seen_months.add(key)
                sample_dates.append(d)

        if start_date:
            sample_dates = [d for d in sample_dates if d >= start_date]
        if end_date:
            sample_dates = [d for d in sample_dates if d <= end_date]

        # 计算因子值
        fund_inputs = self._load_fundamental_inputs(price_dates)
        stmt_inputs = self._load_statement_inputs(price_dates)
        for sym, series in stmt_inputs.items():
            fund_inputs.setdefault(sym, {}).update(series)
        factor_values: dict[str, dict[date, dict[str, float]]] = {
            name: {} for name in FACTOR_DEFS
        }
        for d in sample_dates:
            for sym in prices:
                idx = price_index[sym].get(d)
                if idx is None:
                    # 该标的当日无交易，找最近的前一个交易日
                    prior = [pd for pd in price_dates[sym] if pd <= d]
                    if not prior:
                        continue
                    idx = price_index[sym][prior[-1]]
                for name, (fn, _, _, input_key) in FACTOR_DEFS.items():
                    if input_key == "close":
                        arr = prices[sym]
                    else:
                        arr = fund_inputs.get(sym, {}).get(input_key)
                        if arr is None:
                            continue
                    v = fn(arr, idx)
                    if v is not None and np.isfinite(v):
                        factor_values[name].setdefault(d, {})[sym] = v

        return {
            "dates": sample_dates,
            "symbols": sorted(prices.keys()),
            "prices": prices,
            "price_dates": price_dates,
            "price_index": price_index,
            "factor_values": factor_values,
        }

    def _load_fundamental_inputs(
        self, price_dates: dict[str, list[date]]
    ) -> dict[str, dict[str, np.ndarray]]:
        """加载 daily_basic 并生成与价格日期对齐的因子输入序列。

        返回 {symbol: {"ep": np.ndarray, "bp": np.ndarray,
        "turnover": np.ndarray}}，数组下标与 price_dates[symbol] 一致，
        缺失日记 NaN。daily_basic 表不存在或为空时返回 {}（基本面因子
        自动跳过，价格因子不受影响）。
        """
        try:
            rows = self.warehouse.query(
                "SELECT symbol, date, pe_ttm, pb, turnover_rate "
                "FROM daily_basic"
            )
        except Exception:
            return {}
        if not rows:
            return {}

        by_symbol: dict[str, dict[date, dict[str, Any]]] = {}
        for r in rows:
            d = r["date"]
            if isinstance(d, str):
                d = date.fromisoformat(d)
            by_symbol.setdefault(str(r["symbol"]), {})[d] = r

        inputs: dict[str, dict[str, np.ndarray]] = {}
        for sym, fmap in by_symbol.items():
            dates = price_dates.get(sym)
            if not dates:
                continue
            n = len(dates)
            ep = np.full(n, np.nan)
            bp = np.full(n, np.nan)
            turnover = np.full(n, np.nan)
            for idx, d in enumerate(dates):
                rec = fmap.get(d)
                if not rec:
                    continue
                pe = rec.get("pe_ttm")
                if pe is not None and float(pe) > 0:
                    ep[idx] = 1.0 / float(pe)
                pb = rec.get("pb")
                if pb is not None and float(pb) > 0:
                    bp[idx] = 1.0 / float(pb)
                tv = rec.get("turnover_rate")
                if tv is not None:
                    turnover[idx] = float(tv)
            inputs[sym] = {"ep": ep, "bp": bp, "turnover": turnover}
        return inputs

    STATEMENT_KEYS = ("roe", "cfo_to_np", "profit_growth")

    def _load_statement_inputs(
        self, price_dates: dict[str, list[date]]
    ) -> dict[str, dict[str, np.ndarray]]:
        """加载 financial_statement 并按 filed_at 做 PIT 对齐。

        每个交易日只可见 filed_at <= 当日的最新一期财报（披露日前用旧期），
        返回 {symbol: {"roe"/"cfo_to_np"/"profit_growth": np.ndarray}}，
        数组下标与 price_dates[symbol] 一致，尚无可披露财报的日记 NaN。
        filed_at 为 NULL 的行跳过（无真实披露日，PIT 不安全）。
        表不存在或为空时返回 {}（真基本面因子自动跳过）。
        """
        import bisect

        try:
            rows = self.warehouse.query(
                "SELECT symbol, filed_at, roe, cfo_to_np, profit_growth "
                "FROM financial_statement WHERE filed_at IS NOT NULL "
                "ORDER BY symbol, filed_at"
            )
        except Exception:
            return {}
        if not rows:
            return {}

        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            d = r["filed_at"]
            if isinstance(d, str):
                d = date.fromisoformat(d)
            by_symbol.setdefault(str(r["symbol"]), []).append(
                {"filed_at": d, **{k: r.get(k) for k in self.STATEMENT_KEYS}})

        inputs: dict[str, dict[str, np.ndarray]] = {}
        for sym, reports in by_symbol.items():
            dates = price_dates.get(sym)
            if not dates:
                continue
            n = len(dates)
            series = {k: np.full(n, np.nan) for k in self.STATEMENT_KEYS}
            for j, rep in enumerate(reports):
                start = bisect.bisect_left(dates, rep["filed_at"])
                end = (bisect.bisect_left(dates, reports[j + 1]["filed_at"])
                       if j + 1 < len(reports) else n)
                if start >= end:
                    continue
                for k in self.STATEMENT_KEYS:
                    v = rep.get(k)
                    if v is not None:
                        series[k][start:end] = float(v)
            inputs[sym] = series
        return inputs

    def forward_return(self, data: dict[str, Any], symbol: str,
                       d: date, horizon: int) -> float | None:
        """计算 symbol 在采样日 d 之后 horizon 个交易日的收益。"""
        idx = data["price_index"].get(symbol, {}).get(d)
        if idx is None:
            prior = [pd for pd in data["price_dates"][symbol] if pd <= d]
            if not prior:
                return None
            idx = data["price_index"][symbol][prior[-1]]
        px = data["prices"][symbol]
        if idx + horizon >= len(px):
            return None
        base = px[idx]
        return float(px[idx + horizon] / base - 1) if base > 0 else None
