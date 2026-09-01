"""
factor_stability.py — 因子稳定性分析。

一个因子"偶尔有效"不等于"长期有效"。本模块回答：
    - 逐年 mean RankIC 的符号一致性（稳定年份比例）
    - 滚动 12 期 IC 趋势（上升 / 稳定 / 衰减）
    - 最差年份表现

长期投资策略只应使用跨年份稳定有效的因子。
"""

from __future__ import annotations

from typing import Any

import numpy as np


def analyze_stability(ic_series: list[dict[str, Any]]) -> dict[str, Any]:
    """基于 IC 序列的稳定性分析。"""
    valid = [e for e in ic_series if not np.isnan(e["rank_ic"])]
    if not valid:
        return {"periods": 0}

    # 逐年统计
    by_year: dict[int, list[float]] = {}
    for e in valid:
        by_year.setdefault(e["date"].year, []).append(e["rank_ic"])

    yearly_ic = {}
    for y, ics in sorted(by_year.items()):
        yearly_ic[str(y)] = {
            "mean_rank_ic": round(float(np.mean(ics)), 4),
            "periods": len(ics),
        }

    year_means = [v["mean_rank_ic"] for v in yearly_ic.values()]
    overall_mean = float(np.mean([e["rank_ic"] for e in valid]))
    sign = 1 if overall_mean >= 0 else -1
    stable_years = sum(1 for m in year_means if m * sign > 0)
    stable_ratio = stable_years / len(year_means) if year_means else 0.0

    # 滚动 12 期 IC 趋势
    rank_ics = [e["rank_ic"] for e in valid]
    rolling = []
    for i in range(11, len(rank_ics)):
        window = rank_ics[i - 11:i + 1]
        rolling.append(float(np.mean(window)))
    if len(rolling) >= 4:
        recent = float(np.mean(rolling[-4:]))
        early = float(np.mean(rolling[:4]))
        if recent > early + 0.01:
            trend = "RISING"
        elif recent < early - 0.01:
            trend = "DECLINING"
        else:
            trend = "STABLE"
    else:
        trend = "INSUFFICIENT_DATA"

    worst_year = min(yearly_ic.items(), key=lambda kv: kv[1]["mean_rank_ic"])[0] \
        if yearly_ic else None

    return {
        "periods": len(valid),
        "yearly_ic": yearly_ic,
        "stable_year_ratio": round(stable_ratio, 3),
        "stable_years": stable_years,
        "total_years": len(year_means),
        "rolling_12p_trend": trend,
        "worst_year": worst_year,
        "worst_year_ic": yearly_ic.get(worst_year, {}).get("mean_rank_ic")
        if worst_year else None,
    }
