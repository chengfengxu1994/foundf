"""基于可信单位净值的组合绩效、回撤与多资产基准计算。"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date
from typing import Any, Mapping, Sequence


def rebuild_unit_nav(
    records: Sequence[Mapping[str, Any]],
    *,
    min_valuation_coverage: float = 0.95,
) -> dict[str, Any]:
    """从历史总资产和外部现金流幂等重建单位净值。

    每条记录的 ``external_flow`` 表示上一估值日之后至本估值日发生的净入金；
    外部现金流只改变份额，不改变单位净值。
    """

    canonical = sorted(records, key=lambda row: str(row["date"]))
    snapshots = []
    units = 0.0
    previous_nav = 1.0
    previous_date: date | None = None
    for row in canonical:
        as_of = date.fromisoformat(str(row["date"])[:10])
        if previous_date is not None and as_of <= previous_date:
            raise ValueError("record dates must be unique and increasing")
        total_asset = float(row["total_asset"])
        coverage = float(row.get("valuation_coverage", 0))
        flow = float(row.get("external_flow", 0) or 0)
        if total_asset <= 0 or coverage < min_valuation_coverage:
            raise ValueError(f"untrusted valuation on {as_of.isoformat()}")
        if not snapshots:
            units = total_asset
            unit_nav = 1.0
            flow = 0.0
        else:
            units += flow / previous_nav
            if units <= 0:
                raise ValueError("external flow makes units non-positive")
            unit_nav = total_asset / units
        snapshot = {
            "date": as_of.isoformat(),
            "total_asset": round(total_asset, 2),
            "external_flow": round(flow, 2),
            "units": round(units, 8),
            "unit_nav": round(unit_nav, 8),
            "valuation_coverage": round(coverage, 6),
            "calculation": (
                "initial_units=total_asset"
                if not snapshots
                else "units=previous_units+external_flow/previous_nav;"
                "unit_nav=total_asset/units"
            ),
        }
        snapshots.append(snapshot)
        previous_nav = unit_nav
        previous_date = as_of
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "schema_version": "foundf.unit_nav.v1",
        "method": "CASH_FLOW_ADJUSTED_UNIT_NAV_TWR",
        "input_hash": digest,
        "snapshots": snapshots,
    }


def modified_dietz_return(
    beginning_value: float,
    ending_value: float,
    cash_flows: Sequence[Mapping[str, Any]],
) -> float | None:
    """计算单期 Modified Dietz；weight 为现金流在期内剩余时间比例。"""

    beginning = float(beginning_value)
    ending = float(ending_value)
    weighted_flows = sum(
        float(row["amount"]) * float(row["weight"]) for row in cash_flows
    )
    total_flows = sum(float(row["amount"]) for row in cash_flows)
    denominator = beginning + weighted_flows
    if denominator <= 0:
        return None
    return (ending - beginning - total_flows) / denominator


def _returns(values: Sequence[float]) -> list[float]:
    return [
        current / previous - 1
        for previous, current in zip(values, values[1:])
        if previous > 0
    ]


def _max_drawdown(values: Sequence[float]) -> dict[str, Any]:
    peak = -math.inf
    maximum = 0.0
    peak_index = trough_index = 0
    active_peak_index = 0
    for index, value in enumerate(values):
        if value > peak:
            peak = value
            active_peak_index = index
        drawdown = value / peak - 1 if peak > 0 else 0.0
        if drawdown < maximum:
            maximum = drawdown
            peak_index = active_peak_index
            trough_index = index
    return {
        "max_drawdown": round(maximum, 6),
        "peak_index": peak_index,
        "trough_index": trough_index,
    }


def calculate_performance(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    annual_risk_free_rate: float = 0.02,
    benchmark_levels: Mapping[str, Sequence[float]] | None = None,
    benchmark_weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """仅使用单位净值计算绩效；不足两个可信快照时明确降级。"""

    values = [float(row["unit_nav"]) for row in snapshots]
    if len(values) < 2 or any(value <= 0 for value in values):
        return {"status": "DATA_BUILDING", "observations": len(values)}
    returns = _returns(values)
    annual_vol = (
        statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None
    )
    total_return = values[-1] / values[0] - 1
    annual_return = (1 + total_return) ** (252 / len(returns)) - 1
    sharpe = (
        (annual_return - annual_risk_free_rate) / annual_vol
        if annual_vol and annual_vol > 0
        else None
    )
    drawdown = _max_drawdown(values)
    result: dict[str, Any] = {
        "status": "READY",
        "method": "UNIT_NAV_TWR",
        "observations": len(values),
        "total_return": round(total_return, 6),
        "annualized_return": round(annual_return, 6),
        "annualized_volatility": (
            round(annual_vol, 6) if annual_vol is not None else None
        ),
        "sharpe_ratio": round(sharpe, 6) if sharpe is not None else None,
        **drawdown,
    }

    if benchmark_levels and benchmark_weights:
        weights = {key: float(value) for key, value in benchmark_weights.items()}
        if abs(sum(weights.values()) - 1) > 1e-6:
            raise ValueError("benchmark weights must sum to 1")
        required_length = len(values)
        if any(
            key not in benchmark_levels
            or len(benchmark_levels[key]) != required_length
            or float(benchmark_levels[key][0]) <= 0
            for key in weights
        ):
            result["benchmark"] = {"status": "DATA_INCOMPLETE"}
        else:
            blended = []
            for index in range(required_length):
                blended.append(
                    sum(
                        weights[key]
                        * float(benchmark_levels[key][index])
                        / float(benchmark_levels[key][0])
                        for key in weights
                    )
                )
            benchmark_returns = _returns(blended)
            active = [
                portfolio - benchmark
                for portfolio, benchmark in zip(returns, benchmark_returns)
            ]
            tracking_error = (
                statistics.stdev(active) * math.sqrt(252)
                if len(active) >= 2
                else None
            )
            result["benchmark"] = {
                "status": "READY",
                "weights": weights,
                "total_return": round(blended[-1] / blended[0] - 1, 6),
                "excess_return": round(
                    total_return - (blended[-1] / blended[0] - 1), 6
                ),
                "tracking_error": (
                    round(tracking_error, 6)
                    if tracking_error is not None
                    else None
                ),
            }
    return result

