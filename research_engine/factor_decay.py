"""
factor_decay.py — 因子衰減分析。

有效因子的 IC 随持有期变化的形态揭示其性质：
    - 快速衰减（5d 强、63d 弱）→ 交易型因子，不适合长期持有
    - 缓慢衰减/平台型        → 配置型因子，适合长期策略

与长期投资目标的匹配度：优先保留 21d/63d 仍有正 IC 的因子。
"""

from __future__ import annotations

from typing import Any

from .factor_ic import compute_ic_series, summarize_ic

DECAY_HORIZONS = [5, 10, 21, 63, 126]


def analyze_decay(data: dict[str, Any], factor_name: str,
                  builder) -> dict[str, Any]:
    """多 horizon IC 衰减曲线。"""
    by_horizon: dict[str, Any] = {}
    for h in DECAY_HORIZONS:
        series = compute_ic_series(data, factor_name, builder, horizon=h)
        by_horizon[f"{h}d"] = summarize_ic(series)

    # 衰减形态
    ic5 = by_horizon.get("5d", {}).get("mean_rank_ic")
    ic63 = by_horizon.get("63d", {}).get("mean_rank_ic")
    if ic5 is None or ic63 is None:
        shape = "INSUFFICIENT_DATA"
    elif ic5 > 0.02 and ic63 > 0.02:
        shape = "PERSISTENT"      # 平台型：适合长期
    elif ic5 > 0.02 and ic63 <= 0.01:
        shape = "FAST_DECAY"      # 交易型：与长期目标不匹配
    elif ic5 <= 0.01 and ic63 > 0.02:
        shape = "SLOW_BUILD"      # 慢热型：长期有效
    else:
        shape = "WEAK"

    return {
        "by_horizon": by_horizon,
        "decay_shape": shape,
        "long_horizon_effective": bool(ic63 is not None and ic63 > 0.02),
    }
