"""Strict, read-only adapter for versioned Walk-Forward evidence.

The adapter validates provenance and measurement completeness, then emits the
small ``windows`` + ``summary`` contract consumed by ``evolution.py``.  It
never estimates missing values, changes strategy weights, or makes a
governance decision.
"""

from __future__ import annotations

from datetime import date
import math
import statistics
from typing import Any, Mapping


WALK_FORWARD_EVIDENCE_SCHEMA = "foundf.walk_forward_evidence.v1"
EVOLUTION_WALK_FORWARD_SCHEMA = "foundf.strategy_evolution_walk_forward.v1"

_SUMMARY_METRICS = (
    "avg_annual_return",
    "positive_window_ratio",
    "avg_sharpe",
    "max_drawdown",
    "turnover",
    "avg_excess_return",
)
_WINDOW_METRICS = (
    "gross_return",
    "cost_return",
    "net_return",
    "benchmark_return",
    "excess_return",
    "turnover",
    "sharpe",
    "max_drawdown",
)


class EvidenceContractError(ValueError):
    """Raised when evidence cannot safely enter strategy governance."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceContractError(code)
    return value


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceContractError(code)
    return value.strip()


def _number(value: Any, code: str) -> float:
    if isinstance(value, bool):
        raise EvidenceContractError(code)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceContractError(code) from exc
    if not math.isfinite(result):
        raise EvidenceContractError(code)
    return result


def _date(value: Any, code: str) -> date:
    raw = _text(value, code)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise EvidenceContractError(code, raw) from exc


def _require_close(left: float, right: float, code: str) -> None:
    if not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9):
        raise EvidenceContractError(code)


def _daily_returns(value: Any, code: str) -> list[float]:
    if not isinstance(value, list) or len(value) < 2:
        raise EvidenceContractError(code)
    output = [_number(item, code) for item in value]
    if any(item <= -1 for item in output):
        raise EvidenceContractError(code)
    return output


def _compound(values: list[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1.0 + value
    return result - 1.0


def _sharpe(values: list[float]) -> float:
    deviation = statistics.stdev(values)
    return (
        statistics.mean(values) / deviation * math.sqrt(252.0)
        if deviation > 0
        else 0.0
    )


def _max_drawdown(values: list[float]) -> float:
    nav = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        nav *= 1.0 + value
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1.0)
    return drawdown


def adapt_walk_forward_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and adapt one explicitly versioned Walk-Forward evidence set.

    Structural validation is intentionally separate from policy evaluation.
    For example, a well-formed report with too few windows is returned here
    and subsequently rejected by ``StrategyEvolutionGovernor``.
    """

    raw = _mapping(evidence, "EVIDENCE_MUST_BE_MAPPING")
    schema = raw.get("schema_version")
    if schema != WALK_FORWARD_EVIDENCE_SCHEMA:
        raise EvidenceContractError("UNKNOWN_EVIDENCE_SCHEMA", str(schema))

    generated_at = _text(raw.get("generated_at"), "GENERATED_AT_MISSING")
    strategy_id = _text(raw.get("strategy_id"), "STRATEGY_ID_MISSING")
    strategy_version = _text(
        raw.get("strategy_version"), "STRATEGY_VERSION_MISSING"
    )

    cost_model = _mapping(raw.get("cost_model"), "COST_MODEL_MISSING")
    if cost_model.get("included") is not True:
        raise EvidenceContractError("COST_MODEL_NOT_INCLUDED")
    cost_model_id = _text(cost_model.get("id"), "COST_MODEL_ID_MISSING")
    commission_bps = _number(
        cost_model.get("commission_bps"), "COMMISSION_BPS_MISSING"
    )
    slippage_bps = _number(
        cost_model.get("slippage_bps"), "SLIPPAGE_BPS_MISSING"
    )
    total_cost_bps = commission_bps + slippage_bps
    if commission_bps < 0 or slippage_bps < 0 or not 0 < total_cost_bps < 10_000:
        raise EvidenceContractError("COST_MODEL_BPS_INVALID")

    benchmark = _mapping(raw.get("benchmark"), "BENCHMARK_MISSING")
    benchmark_id = _text(benchmark.get("id"), "BENCHMARK_ID_MISSING")

    protocol = _mapping(raw.get("oos_protocol"), "OOS_PROTOCOL_MISSING")
    if protocol.get("point_in_time") is not True:
        raise EvidenceContractError("POINT_IN_TIME_NOT_PROVEN")
    lag = _number(
        protocol.get("execution_lag_sessions"), "EXECUTION_LAG_MISSING"
    )
    if lag < 1 or not lag.is_integer():
        raise EvidenceContractError("EXECUTION_LAG_INVALID")

    windows_raw = raw.get("windows")
    if not isinstance(windows_raw, list):
        raise EvidenceContractError("WINDOWS_MUST_BE_LIST")
    if not windows_raw:
        raise EvidenceContractError("WINDOWS_EMPTY")

    windows: list[dict[str, Any]] = []
    observed_values: list[float] = []
    excess_returns: list[float] = []
    all_daily_returns: list[float] = []
    oos_days = 0
    for index, item in enumerate(windows_raw):
        window = _mapping(item, f"WINDOW_{index}_INVALID")
        train_start = _date(
            window.get("train_start"), f"WINDOW_{index}_TRAIN_START_MISSING"
        )
        train_end = _date(
            window.get("train_end"), f"WINDOW_{index}_TRAIN_END_MISSING"
        )
        test_start = _date(
            window.get("test_start"), f"WINDOW_{index}_TEST_START_MISSING"
        )
        test_end = _date(
            window.get("test_end"), f"WINDOW_{index}_TEST_END_MISSING"
        )
        if train_start > train_end or train_end >= test_start or test_start > test_end:
            raise EvidenceContractError(f"WINDOW_{index}_OOS_DATES_INVALID")

        metrics = {
            name: _number(
                window.get(name), f"WINDOW_{index}_{name.upper()}_MISSING"
            )
            for name in _WINDOW_METRICS
        }
        if metrics["cost_return"] < 0:
            raise EvidenceContractError(f"WINDOW_{index}_COST_NEGATIVE")
        if metrics["turnover"] < 0:
            raise EvidenceContractError(f"WINDOW_{index}_TURNOVER_NEGATIVE")
        daily_returns = _daily_returns(
            window.get("daily_net_returns"),
            f"WINDOW_{index}_DAILY_RETURNS_INVALID",
        )
        sessions = _number(
            window.get("sessions"), f"WINDOW_{index}_SESSIONS_MISSING"
        )
        if not sessions.is_integer() or int(sessions) != len(daily_returns):
            raise EvidenceContractError(f"WINDOW_{index}_SESSIONS_INCONSISTENT")
        _require_close(
            metrics["net_return"],
            metrics["gross_return"] - metrics["cost_return"],
            f"WINDOW_{index}_NET_COST_INCONSISTENT",
        )
        _require_close(
            metrics["excess_return"],
            metrics["net_return"] - metrics["benchmark_return"],
            f"WINDOW_{index}_BENCHMARK_INCONSISTENT",
        )
        _require_close(
            metrics["cost_return"],
            metrics["turnover"] * total_cost_bps / 10_000,
            f"WINDOW_{index}_COST_MODEL_INCONSISTENT",
        )
        _require_close(
            metrics["net_return"],
            _compound(daily_returns),
            f"WINDOW_{index}_DAILY_RETURN_INCONSISTENT",
        )
        _require_close(
            metrics["sharpe"],
            _sharpe(daily_returns),
            f"WINDOW_{index}_SHARPE_INCONSISTENT",
        )
        _require_close(
            metrics["max_drawdown"],
            _max_drawdown(daily_returns),
            f"WINDOW_{index}_DRAWDOWN_INCONSISTENT",
        )
        observed_values.extend(metrics.values())
        observed_values.extend(daily_returns)
        excess_returns.append(metrics["excess_return"])
        all_daily_returns.extend(daily_returns)
        oos_days += (test_end - test_start).days + 1
        windows.append(
            {
                "train_start": train_start.isoformat(),
                "train_end": train_end.isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "test": f"{test_start.isoformat()}~{test_end.isoformat()}",
                **metrics,
                "sessions": int(sessions),
                "daily_net_returns": daily_returns,
            }
        )

    summary_raw = _mapping(raw.get("summary"), "SUMMARY_MISSING")
    summary = {
        name: _number(summary_raw.get(name), f"SUMMARY_{name.upper()}_MISSING")
        for name in _SUMMARY_METRICS
    }
    if not 0 <= summary["positive_window_ratio"] <= 1:
        raise EvidenceContractError("SUMMARY_POSITIVE_WINDOW_RATIO_INVALID")
    if summary["turnover"] < 0:
        raise EvidenceContractError("SUMMARY_TURNOVER_NEGATIVE")
    observed_values.extend(summary.values())
    if all(math.isclose(value, 0.0, abs_tol=1e-12) for value in observed_values):
        raise EvidenceContractError("PLACEHOLDER_ALL_ZERO_EVIDENCE")

    definition = _text(
        summary_raw.get("positive_window_definition"),
        "POSITIVE_WINDOW_DEFINITION_MISSING",
    )
    if definition != "excess_return_after_cost":
        raise EvidenceContractError("POSITIVE_WINDOW_DEFINITION_UNSUPPORTED")
    measured_positive_ratio = sum(value > 0 for value in excess_returns) / len(
        excess_returns
    )
    _require_close(
        summary["positive_window_ratio"],
        measured_positive_ratio,
        "POSITIVE_WINDOW_RATIO_INCONSISTENT",
    )
    _require_close(
        summary["avg_excess_return"],
        sum(excess_returns) / len(excess_returns),
        "AVG_EXCESS_RETURN_INCONSISTENT",
    )
    oos_years = oos_days / 365.25
    compounded_return = _compound(
        [window["net_return"] for window in windows]
    )
    annual_return = (
        (1.0 + compounded_return) ** (1.0 / oos_years) - 1.0
        if oos_years > 0 and compounded_return > -1
        else -1.0
    )
    _require_close(
        summary["avg_annual_return"],
        annual_return,
        "AVG_ANNUAL_RETURN_INCONSISTENT",
    )
    _require_close(
        summary["avg_sharpe"],
        sum(window["sharpe"] for window in windows) / len(windows),
        "AVG_SHARPE_INCONSISTENT",
    )
    _require_close(
        summary["max_drawdown"],
        _max_drawdown(all_daily_returns),
        "MAX_DRAWDOWN_INCONSISTENT",
    )
    _require_close(
        summary["turnover"],
        sum(window["turnover"] for window in windows) / oos_years,
        "SUMMARY_TURNOVER_INCONSISTENT",
    )

    return {
        "schema_version": EVOLUTION_WALK_FORWARD_SCHEMA,
        "source_schema_version": WALK_FORWARD_EVIDENCE_SCHEMA,
        "generated_at": generated_at,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "windows": windows,
        "summary": summary,
        "evidence_metadata": {
            "cost_model_id": cost_model_id,
            "commission_bps": commission_bps,
            "slippage_bps": slippage_bps,
            "benchmark_id": benchmark_id,
            "point_in_time": True,
            "execution_lag_sessions": int(lag),
            "read_only_adapter": True,
            "automatic_weight_change": False,
        },
    }
