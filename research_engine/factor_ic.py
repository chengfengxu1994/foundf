"""
factor_ic.py — 因子 IC / RankIC 分析。

IC (Information Coefficient)：时点 t 的因子横截面值与 t→t+h 远期收益的相关性。
    - IC:      Pearson 相关
    - RankIC:  Spearman 秩相关（对异常值稳健，业界主用）
    - ICIR:    mean(IC) / std(IC)，衡量 IC 稳定性

判定标准（成熟量化实践）：
    |mean RankIC| ≥ 0.03 且 ICIR ≥ 0.2  → 有效
    |mean RankIC| < 0.01 或 ICIR < 0.05 → 无效（候选剔除）
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

MIN_CROSS_SECTION = 5  # 横截面最少标的数


def _rank(values: np.ndarray) -> np.ndarray:
    """平均秩（处理并列）。"""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sorted_vals = values[order]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    rx, ry = _rank(x), _rank(y)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def compute_ic_series(data: dict[str, Any], factor_name: str,
                      builder, horizon: int = 21) -> list[dict[str, Any]]:
    """计算某因子在所有采样日的 IC 序列。

    Returns: [{date, ic, rank_ic, n}, ...]
    """
    series = []
    fv = data["factor_values"].get(factor_name, {})
    for d in data["dates"]:
        cross = fv.get(d, {})
        if len(cross) < MIN_CROSS_SECTION:
            continue
        xs, ys = [], []
        for sym, fval in cross.items():
            fwd = builder.forward_return(data, sym, d, horizon)
            if fwd is not None and np.isfinite(fwd):
                xs.append(fval)
                ys.append(fwd)
        if len(xs) < MIN_CROSS_SECTION:
            continue
        x, y = np.array(xs), np.array(ys)
        series.append({
            "date": d,
            "ic": pearson(x, y),
            "rank_ic": spearman(x, y),
            "n": len(xs),
        })
    return series


def summarize_ic(ic_series: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总 IC 序列 → mean / std / ICIR / 正 IC 比例。"""
    ics = np.array([e["rank_ic"] for e in ic_series
                    if not np.isnan(e["rank_ic"])], dtype=float)
    raw = np.array([e["ic"] for e in ic_series
                    if not np.isnan(e["ic"])], dtype=float)
    if len(ics) == 0:
        return {"periods": 0}
    mean_ic = float(np.mean(ics))
    std_ic = float(np.std(ics))
    return {
        "periods": len(ics),
        "mean_ic": round(float(np.mean(raw)), 4) if len(raw) else None,
        "mean_rank_ic": round(mean_ic, 4),
        "std_rank_ic": round(std_ic, 4),
        "icir": round(mean_ic / std_ic, 3) if std_ic > 0 else None,
        "positive_ratio": round(float(np.mean(ics > 0)), 3),
    }
