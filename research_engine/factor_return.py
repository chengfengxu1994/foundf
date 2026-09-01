"""
factor_return.py — 因子分组收益分析。

每个采样日按因子值排序分组（小宇宙用 top/bottom 30%），
持有一个 horizon，统计多空价差收益序列：
    - 年化收益
    - 最大回撤
    - 年度收益分布

这是"因子能不能赚钱"的直接证据，与 IC（"因子有没有预测力"）互补。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

GROUP_PCT = 0.30          # 头尾各 30%
MIN_GROUP = 3             # 每组最少标的


def compute_spread_series(data: dict[str, Any], factor_name: str,
                          builder, horizon: int = 21) -> list[dict[str, Any]]:
    """逐期多空价差收益序列（多头=因子最高组，空头=最低组）。"""
    series = []
    fv = data["factor_values"].get(factor_name, {})
    for d in data["dates"]:
        cross = fv.get(d, {})
        if len(cross) < MIN_GROUP * 2:
            continue
        rets = {}
        for sym in cross:
            fwd = builder.forward_return(data, sym, d, horizon)
            if fwd is not None and np.isfinite(fwd):
                rets[sym] = fwd
        ranked = sorted(rets.items(), key=lambda kv: cross[kv[0]], reverse=True)
        n = max(MIN_GROUP, int(len(ranked) * GROUP_PCT))
        top, bottom = ranked[:n], ranked[-n:]
        if not top or not bottom:
            continue
        top_ret = float(np.mean([r for _, r in top]))
        bottom_ret = float(np.mean([r for _, r in bottom]))
        series.append({
            "date": d,
            "spread": top_ret - bottom_ret,
            "top_ret": top_ret,
            "bottom_ret": bottom_ret,
            "n": len(ranked),
        })
    return series


def summarize_returns(spread_series: list[dict[str, Any]],
                      horizon: int = 21) -> dict[str, Any]:
    """汇总价差序列 → 年化 / 最大回撤 / 年度收益 / 胜率。"""
    if not spread_series:
        return {"periods": 0}

    spreads = np.array([e["spread"] for e in spread_series], dtype=float)
    equity = np.cumprod(1 + spreads)
    peak = np.maximum.accumulate(equity)
    max_dd = float((equity / peak - 1).min())

    years = max((spread_series[-1]["date"] - spread_series[0]["date"]).days / 365.25, 0.1)
    ann_return = float(equity[-1] ** (1 / years) - 1)

    # 年度收益
    by_year: dict[int, float] = {}
    for e in spread_series:
        by_year.setdefault(e["date"].year, 1.0)
        by_year[e["date"].year] *= (1 + e["spread"])
    yearly = {str(y): round(v - 1, 4) for y, v in sorted(by_year.items())}

    return {
        "periods": len(spreads),
        "ann_return": round(ann_return, 4),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(float(np.mean(spreads > 0)), 3),
        "avg_spread_per_period": round(float(np.mean(spreads)), 4),
        "yearly_returns": yearly,
    }
