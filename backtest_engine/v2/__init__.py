"""Point-in-time Walk-Forward evidence engine.

This engine is deliberately narrower than a strategy optimizer: it receives a
versioned strategy callback, historical universe membership, explicit total
return price-basis evidence, a benchmark and a non-zero cost model.  It exposes
only training rows to the callback, executes on the next common session, and
emits the strict ``foundf.walk_forward_evidence.v1`` contract.

Missing membership, adjustment, benchmark or execution data fails closed.  No
result from this module changes production weights or represents broker fills.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import numpy as np

from foundf_db import Warehouse


EVIDENCE_SCHEMA = "foundf.walk_forward_evidence.v1"
TOTAL_RETURN_BASIS = "TOTAL_RETURN_ADJUSTED"
ALLOWED_PRICE_FIELDS = {"close", "close_x_adj_factor"}


class WalkForwardDataError(ValueError):
    """A stable fail-closed reason for incomplete backtest evidence."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class UniverseMembership:
    symbol: str
    effective_from: str
    effective_to: str | None
    source_id: str

    def validate(self) -> None:
        start = _iso_date(self.effective_from, "MEMBERSHIP_START_INVALID")
        end = (
            _iso_date(self.effective_to, "MEMBERSHIP_END_INVALID")
            if self.effective_to
            else None
        )
        if end is not None and end < start:
            raise WalkForwardDataError("MEMBERSHIP_RANGE_INVALID", self.symbol)
        if not self.symbol.strip() or not self.source_id.strip():
            raise WalkForwardDataError("MEMBERSHIP_PROVENANCE_MISSING")


@dataclass(frozen=True)
class PriceSeriesEvidence:
    symbol: str
    basis: str
    price_field: str
    source_id: str

    def validate(self) -> None:
        if self.basis != TOTAL_RETURN_BASIS:
            raise WalkForwardDataError(
                "TOTAL_RETURN_ADJUSTMENT_NOT_PROVEN", self.symbol
            )
        if self.price_field not in ALLOWED_PRICE_FIELDS:
            raise WalkForwardDataError("PRICE_FIELD_UNSUPPORTED", self.symbol)
        if not self.symbol.strip() or not self.source_id.strip():
            raise WalkForwardDataError("PRICE_PROVENANCE_MISSING")


@dataclass(frozen=True)
class CostModel:
    model_id: str
    commission_bps: float
    slippage_bps: float

    @property
    def total_bps(self) -> float:
        return float(self.commission_bps) + float(self.slippage_bps)

    def validate(self) -> None:
        values = (self.commission_bps, self.slippage_bps)
        if (
            not self.model_id.strip()
            or any(not math.isfinite(float(value)) or float(value) < 0 for value in values)
            or self.total_bps <= 0
            or self.total_bps >= 10_000
        ):
            raise WalkForwardDataError("COST_MODEL_INVALID")


@dataclass(frozen=True)
class WindowContext:
    index: int
    train_start: str
    train_end: str
    planned_test_start: str
    planned_test_end: str
    universe: tuple[str, ...]


StrategyCallback = Callable[
    [Mapping[str, tuple[dict[str, Any], ...]], WindowContext],
    Mapping[str, float],
]


class PitStatusView(Protocol):
    """引擎查询日频交易状态的只读注入接口（stock_status_daily 口径）。

    返回 1=正常、0=停牌；None 表示该 (symbol, day) 无状态行。由 runner
    负责从 DuckDB 喂数据（预载或逐查均可），引擎本体保持可纯内存单测。
    """

    def trade_status(self, symbol: str, day: date) -> int | None:
        ...


def _iso_date(value: str | date | None, code: str) -> date:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise WalkForwardDataError(code) from exc


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _maximum_drawdown(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    nav = np.cumprod(1.0 + np.asarray(returns, dtype=float))
    nav = np.r_[1.0, nav]
    peaks = np.maximum.accumulate(nav)
    return float(np.min(nav / peaks - 1.0))


def _annualized_sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    values = np.asarray(returns, dtype=float)
    deviation = float(np.std(values, ddof=1))
    return (
        float(np.mean(values) / deviation * np.sqrt(252.0))
        if deviation > 0
        else 0.0
    )


def _weights(raw: Mapping[str, float], universe: set[str]) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise WalkForwardDataError("STRATEGY_WEIGHTS_MISSING")
    result: dict[str, float] = {}
    for symbol, value in raw.items():
        name = str(symbol).strip()
        if name not in universe:
            raise WalkForwardDataError("STRATEGY_SYMBOL_OUTSIDE_UNIVERSE", name)
        if isinstance(value, bool):
            raise WalkForwardDataError("STRATEGY_WEIGHT_INVALID", name)
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise WalkForwardDataError("STRATEGY_WEIGHT_INVALID", name) from exc
        if not math.isfinite(weight) or weight < 0:
            raise WalkForwardDataError("SHORT_OR_INVALID_WEIGHT", name)
        if weight > 0:
            result[name] = weight
    total = sum(result.values())
    if total <= 0 or total > 1.0 + 1e-9:
        raise WalkForwardDataError("STRATEGY_GROSS_EXPOSURE_INVALID")
    return result


def _turnover(
    prior: Mapping[str, float],
    target_assets: Mapping[str, float],
) -> float:
    target = dict(target_assets)
    target["CASH"] = max(0.0, 1.0 - sum(target_assets.values()))
    names = set(prior) | set(target)
    return 0.5 * sum(
        abs(float(target.get(name, 0.0)) - float(prior.get(name, 0.0)))
        for name in names
    )


class WalkForwardEngine:
    """Generate strict, costed, benchmarked out-of-sample evidence."""

    def __init__(
        self,
        warehouse: Warehouse,
        *,
        train_months: int = 36,
        test_months: int = 6,
        step_months: int = 6,
        minimum_universe_size: int = 100,
        minimum_train_sessions: int = 504,
        minimum_test_sessions: int = 60,
        pit_v2_mode: bool = False,
        pit_status_view: PitStatusView | None = None,
    ) -> None:
        if min(train_months, test_months, step_months) <= 0:
            raise ValueError("window months must be positive")
        self.warehouse = warehouse
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.minimum_universe_size = minimum_universe_size
        self.minimum_train_sessions = minimum_train_sessions
        self.minimum_test_sessions = minimum_test_sessions
        # 真 PIT 宇宙重建 Phase 3：三处新语义（成员级历史不足剔除 /
        # 退市强制退出 / 停牌结转）只在 pit_v2_mode=True 时启用，
        # 默认 False 保持 v1 路径行为逐位不变。
        self.pit_v2_mode = pit_v2_mode
        self.pit_status_view = pit_status_view

    def build_windows(
        self,
        start_date: str | date,
        end_date: str | date,
    ) -> list[dict[str, date]]:
        start = _iso_date(start_date, "START_DATE_INVALID")
        end = _iso_date(end_date, "END_DATE_INVALID")
        if start >= end:
            raise WalkForwardDataError("DATE_RANGE_INVALID")
        windows: list[dict[str, date]] = []
        train_start = start
        while True:
            train_end = _add_months(train_start, self.train_months) - timedelta(days=1)
            planned_test_start = train_end + timedelta(days=1)
            planned_test_end = (
                _add_months(planned_test_start, self.test_months)
                - timedelta(days=1)
            )
            if planned_test_end > end:
                break
            windows.append(
                {
                    "train_start": train_start,
                    "train_end": train_end,
                    "test_start": planned_test_start,
                    "test_end": planned_test_end,
                }
            )
            train_start = _add_months(train_start, self.step_months)
        if not windows:
            raise WalkForwardDataError("WINDOWS_EMPTY")
        return windows

    @staticmethod
    def _active_memberships(
        memberships: Sequence[UniverseMembership],
        as_of: date,
    ) -> list[UniverseMembership]:
        return sorted(
            (
                item
                for item in memberships
                if _iso_date(item.effective_from, "MEMBERSHIP_START_INVALID")
                <= as_of
                and (
                    item.effective_to is None
                    or _iso_date(item.effective_to, "MEMBERSHIP_END_INVALID")
                    >= as_of
                )
            ),
            key=lambda item: item.symbol,
        )

    @staticmethod
    def _adjusted_prices(
        rows: Sequence[dict[str, Any]],
        evidence: PriceSeriesEvidence,
    ) -> dict[date, dict[str, Any]]:
        output: dict[date, dict[str, Any]] = {}
        for row in rows:
            day = _iso_date(row["date"], "PRICE_DATE_INVALID")
            close = float(row["close"])
            open_price = (
                float(row["open"]) if row.get("open") is not None else None
            )
            factor = (
                float(row["adj_factor"])
                if evidence.price_field == "close_x_adj_factor"
                and row.get("adj_factor") is not None
                else 1.0
            )
            if (
                evidence.price_field == "close_x_adj_factor"
                and row.get("adj_factor") is None
            ):
                raise WalkForwardDataError(
                    "ADJUSTMENT_FACTOR_MISSING", evidence.symbol
                )
            adjusted_open = (
                open_price * factor if open_price is not None else None
            )
            adjusted_close = close * factor
            if (
                not math.isfinite(adjusted_close)
                or adjusted_close <= 0
                or (
                    adjusted_open is not None
                    and (
                        not math.isfinite(adjusted_open)
                        or adjusted_open <= 0
                    )
                )
            ):
                raise WalkForwardDataError(
                    "NONPOSITIVE_OR_INVALID_PRICE", evidence.symbol
                )
            output[day] = {
                "date": day.isoformat(),
                "open": adjusted_open,
                "close": adjusted_close,
                "source": str(row.get("source") or ""),
            }
        return output

    def _load_prices(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        price_evidence: Mapping[str, PriceSeriesEvidence],
    ) -> dict[str, dict[date, dict[str, Any]]]:
        placeholders = ", ".join("?" for _ in symbols)
        rows = self.warehouse.query(
            "SELECT date, symbol, open, close, adj_factor, source "
            f"FROM daily_price WHERE symbol IN ({placeholders}) "
            "AND date >= ? AND date <= ? ORDER BY symbol, date",
            [*symbols, start.isoformat(), end.isoformat()],
        )
        grouped: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        for row in rows:
            grouped[str(row["symbol"])].append(row)
        return {
            symbol: self._adjusted_prices(grouped[symbol], price_evidence[symbol])
            for symbol in symbols
        }

    def _v1_exec_plan(
        self,
        *,
        window: dict[str, date],
        index: int,
        selected: Sequence[str],
        benchmark_symbol: str,
        all_prices: Mapping[str, Mapping[date, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        """v1 执行计划：所选标的与基准日历全局交集（原语义，逐位不变）。"""

        calendars = [
            {
                day
                for day in all_prices[symbol]
                if window["test_start"] <= day <= window["test_end"]
            }
            for symbol in [*selected, benchmark_symbol]
        ]
        common_dates = sorted(set.intersection(*calendars)) if calendars else []
        if len(common_dates) < self.minimum_test_sessions:
            raise WalkForwardDataError(
                "TEST_EXECUTION_COVERAGE_INSUFFICIENT",
                f"window={index},sessions={len(common_dates)}",
            )
        entry_date, exit_date = common_dates[0], common_dates[-1]
        missing_entry_open = [
            symbol
            for symbol in [*selected, benchmark_symbol]
            if all_prices[symbol][entry_date]["open"] is None
        ]
        if missing_entry_open:
            raise WalkForwardDataError(
                "EXECUTION_OPEN_MISSING",
                ",".join(sorted(missing_entry_open)),
            )
        return {
            "entry_date": entry_date,
            "dates": common_dates,
            "entry_open": {
                symbol: float(all_prices[symbol][entry_date]["open"])
                for symbol in selected
            },
            "closes": {
                symbol: {
                    day: float(all_prices[symbol][day]["close"])
                    for day in common_dates
                }
                for symbol in selected
            },
            "benchmark_entry_open": float(
                all_prices[benchmark_symbol][entry_date]["open"]
            ),
            "benchmark_exit_close": float(
                all_prices[benchmark_symbol][exit_date]["close"]
            ),
            "delisting_exits": 0,
            "suspension_carry_days": 0,
        }

    def _pit_v2_exec_plan(
        self,
        *,
        window: dict[str, date],
        index: int,
        selected: Sequence[str],
        benchmark_symbol: str,
        all_prices: Mapping[str, Mapping[date, Mapping[str, Any]]],
        membership_end: Mapping[str, date | None],
    ) -> dict[str, Any]:
        """v2 执行计划：并集日历 + 逐标的可得性。

        口径（与 deploy/adhoc_year_backtest.py 的 CARRY 语义一致）：

        - 日历 = 所选标的与基准在 test 窗内的交易日**并集**；基准必须在每个
          并集日有真实行情行，否则交易日历证据不完整，fail-closed。
        - 入场日 = 并集中首个全体所选标的与基准都有真实行情行且 open 非空
          的交易日（停牌标的无法买入，T+1 开盘价执行口径不变）。
        - 退市强制退出：membership effective_to 落在窗口内（早于 exit_date）
          时，以 ≤ effective_to 的最后可得收盘价强制退出；退出后权重归零
          （净值贡献冻结为退出日价值，等价于转为现金），收益只计入至退出日。
        - 停牌结转：窗口内某日无行情行时查 stock_status_daily（经
          PitStatusView 注入），trade_status=0（停牌）以最后可得收盘价
          结转估值；无状态行、或状态为正常却无行情行，都视为数据缺口
          fail-closed（PRICE_DATA_GAP），绝不零填充或静默跳过。
        """

        delisting_exits = 0
        suspension_carry_days = 0
        spine = sorted(
            {
                day
                for symbol in [*selected, benchmark_symbol]
                for day in all_prices[symbol]
                if window["test_start"] <= day <= window["test_end"]
            }
        )
        if len(spine) < self.minimum_test_sessions:
            raise WalkForwardDataError(
                "TEST_EXECUTION_COVERAGE_INSUFFICIENT",
                f"window={index},sessions={len(spine)}",
            )
        benchmark_rows = all_prices[benchmark_symbol]
        missing_benchmark = [day for day in spine if day not in benchmark_rows]
        if missing_benchmark:
            raise WalkForwardDataError(
                "BENCHMARK_SESSION_MISSING",
                f"window={index},first={missing_benchmark[0].isoformat()}",
            )
        for symbol in selected:
            end = membership_end.get(symbol)
            if end is not None and end < window["test_start"]:
                raise WalkForwardDataError(
                    "MEMBERSHIP_ENDED_BEFORE_TEST_WINDOW",
                    f"{symbol}:{end.isoformat()}",
                )
        entry_date: date | None = None
        for day in spine:
            if all(
                day in all_prices[symbol]
                and all_prices[symbol][day]["open"] is not None
                for symbol in [*selected, benchmark_symbol]
            ):
                entry_date = day
                break
        if entry_date is None:
            raise WalkForwardDataError(
                "EXECUTION_OPEN_MISSING",
                f"window={index}",
            )
        exit_date = spine[-1]
        exec_dates = [day for day in spine if day >= entry_date]
        entry_open: dict[str, float] = {}
        closes: dict[str, dict[date, float]] = {}
        for symbol in selected:
            rows = all_prices[symbol]
            member_end = membership_end.get(symbol)
            forced_exit_day: date | None = None
            if member_end is not None and member_end < exit_date:
                available = [
                    day
                    for day in rows
                    if entry_date <= day <= member_end
                ]
                if not available:
                    raise WalkForwardDataError(
                        "DELISTING_EXIT_PRICE_MISSING", symbol
                    )
                # 以 ≤ effective_to 的最后可得收盘价强制退出
                forced_exit_day = available[-1]
                delisting_exits += 1
            series: dict[date, float] = {}
            last_close: float | None = None
            for day in exec_dates:
                if forced_exit_day is not None and day > forced_exit_day:
                    # 退出后权重归零：净值贡献冻结在退出日价值
                    series[day] = float(last_close)
                    continue
                row = rows.get(day)
                if row is not None:
                    last_close = float(row["close"])
                    series[day] = last_close
                    continue
                status = (
                    self.pit_status_view.trade_status(symbol, day)
                    if self.pit_status_view is not None
                    else None
                )
                if status == 0:
                    # 停牌结转：以最后可得收盘价估值（CARRY 语义）
                    series[day] = float(last_close)
                    suspension_carry_days += 1
                    continue
                raise WalkForwardDataError(
                    "PRICE_DATA_GAP",
                    f"{symbol}:{day.isoformat()},status={status}",
                )
            closes[symbol] = series
            entry_open[symbol] = float(rows[entry_date]["open"])
        return {
            "entry_date": entry_date,
            "dates": exec_dates,
            "entry_open": entry_open,
            "closes": closes,
            "benchmark_entry_open": float(benchmark_rows[entry_date]["open"]),
            "benchmark_exit_close": float(benchmark_rows[exit_date]["close"]),
            "delisting_exits": delisting_exits,
            "suspension_carry_days": suspension_carry_days,
        }

    def run(
        self,
        strategy_func: StrategyCallback,
        *,
        strategy_id: str,
        strategy_version: str,
        memberships: Sequence[UniverseMembership],
        price_evidence: Sequence[PriceSeriesEvidence],
        benchmark_symbol: str,
        benchmark_id: str,
        cost_model: CostModel,
        start_date: str | date,
        end_date: str | date,
    ) -> dict[str, Any]:
        if not strategy_id.strip() or not strategy_version.strip():
            raise WalkForwardDataError("STRATEGY_IDENTITY_MISSING")
        cost_model.validate()
        for item in memberships:
            item.validate()
        intervals: dict[str, list[tuple[date, date]]] = {}
        for item in memberships:
            start = _iso_date(item.effective_from, "MEMBERSHIP_START_INVALID")
            end = (
                _iso_date(item.effective_to, "MEMBERSHIP_END_INVALID")
                if item.effective_to
                else date.max
            )
            prior = intervals.setdefault(item.symbol, [])
            if any(
                start <= prior_end and prior_start <= end
                for prior_start, prior_end in prior
            ):
                raise WalkForwardDataError(
                    "MEMBERSHIP_INTERVAL_OVERLAP", item.symbol
                )
            prior.append((start, end))
        evidence_by_symbol = {item.symbol: item for item in price_evidence}
        if len(evidence_by_symbol) != len(price_evidence):
            raise WalkForwardDataError("DUPLICATE_PRICE_EVIDENCE")
        for item in price_evidence:
            item.validate()
        required_symbols = {item.symbol for item in memberships} | {
            benchmark_symbol
        }
        missing_evidence = required_symbols - set(evidence_by_symbol)
        if missing_evidence:
            raise WalkForwardDataError(
                "PRICE_EVIDENCE_MISSING", ",".join(sorted(missing_evidence))
            )
        if not benchmark_id.strip():
            raise WalkForwardDataError("BENCHMARK_ID_MISSING")

        windows = self.build_windows(start_date, end_date)
        first_start = windows[0]["train_start"]
        last_end = windows[-1]["test_end"]
        all_prices = self._load_prices(
            sorted(required_symbols),
            first_start,
            last_end,
            evidence_by_symbol,
        )
        prior_weights: dict[str, float] = {"CASH": 1.0}
        output_windows: list[dict[str, Any]] = []
        all_daily_returns: list[float] = []
        provenance_rows: list[dict[str, Any]] = []
        diagnostics_rows: list[dict[str, Any]] = []

        for index, window in enumerate(windows):
            active = self._active_memberships(
                memberships, window["train_end"]
            )
            if len(active) < self.minimum_universe_size:
                raise WalkForwardDataError(
                    "POINT_IN_TIME_UNIVERSE_TOO_SMALL",
                    f"window={index},count={len(active)}",
                )
            active_symbols = tuple(item.symbol for item in active)
            # 退市强制退出依据：train_end 时点活跃 membership 的 effective_to
            membership_end: dict[str, date | None] = {
                item.symbol: (
                    _iso_date(item.effective_to, "MEMBERSHIP_END_INVALID")
                    if item.effective_to
                    else None
                )
                for item in active
            }
            training: dict[str, tuple[dict[str, Any], ...]] = {}
            train_history_excluded = 0
            for symbol in active_symbols:
                rows = tuple(
                    row
                    for day, row in all_prices[symbol].items()
                    if window["train_start"] <= day <= window["train_end"]
                )
                if len(rows) < self.minimum_train_sessions:
                    if not self.pit_v2_mode:
                        # v1 语义：任何活跃成员历史不足即整 run fail-closed
                        raise WalkForwardDataError(
                            "TRAINING_HISTORY_INSUFFICIENT",
                            f"{symbol}:{len(rows)}",
                        )
                    # v2 语义：成员级剔除并计数；剔除后活跃数不足才在
                    # 宇宙层级 fail-closed（见下方复查）
                    train_history_excluded += 1
                    continue
                training[symbol] = rows
            if self.pit_v2_mode:
                active_symbols = tuple(sorted(training))
                if len(active_symbols) < self.minimum_universe_size:
                    raise WalkForwardDataError(
                        "POINT_IN_TIME_UNIVERSE_TOO_SMALL",
                        f"window={index},count={len(active_symbols)},"
                        f"train_history_excluded={train_history_excluded}",
                    )
            context = WindowContext(
                index=index,
                train_start=window["train_start"].isoformat(),
                train_end=window["train_end"].isoformat(),
                planned_test_start=window["test_start"].isoformat(),
                planned_test_end=window["test_end"].isoformat(),
                universe=active_symbols,
            )
            target = _weights(
                strategy_func(training, context),
                set(active_symbols),
            )
            selected = sorted(target)
            if self.pit_v2_mode:
                # v2 语义：并集日历 + 逐标的可得性（退市强制退出 / 停牌结转）
                exec_plan = self._pit_v2_exec_plan(
                    window=window,
                    index=index,
                    selected=selected,
                    benchmark_symbol=benchmark_symbol,
                    all_prices=all_prices,
                    membership_end=membership_end,
                )
            else:
                # v1 语义：所选标的与基准日历全局交集，逐位保持原行为
                exec_plan = self._v1_exec_plan(
                    window=window,
                    index=index,
                    selected=selected,
                    benchmark_symbol=benchmark_symbol,
                    all_prices=all_prices,
                )
            exec_dates: list[date] = exec_plan["dates"]
            entry_open: dict[str, float] = exec_plan["entry_open"]
            exec_closes: dict[str, dict[date, float]] = exec_plan["closes"]
            exit_date = exec_dates[-1]
            turnover = _turnover(prior_weights, target)
            cost_return = turnover * cost_model.total_bps / 10000.0
            cash_weight = max(0.0, 1.0 - sum(target.values()))
            gross_nav = []
            for day in exec_dates:
                value = cash_weight + sum(
                    weight
                    * (exec_closes[symbol][day] / entry_open[symbol])
                    for symbol, weight in target.items()
                )
                gross_nav.append(float(value))
            gross_return = gross_nav[-1] - 1.0
            net_return = gross_return - cost_return
            if net_return <= -1.0:
                raise WalkForwardDataError("PORTFOLIO_INSOLVENT")
            benchmark_return = (
                exec_plan["benchmark_exit_close"]
                / exec_plan["benchmark_entry_open"]
                - 1.0
            )
            excess_return = net_return - benchmark_return

            net_nav = [max(1e-12, value - cost_return) for value in gross_nav]
            window_daily = []
            previous = 1.0
            for value in net_nav:
                window_daily.append(value / previous - 1.0)
                previous = value
            all_daily_returns.extend(window_daily)
            # 期末估值统一走有效收盘（v2 下退市标的为退出日冻结价）
            final_values = {
                symbol: weight
                * (exec_closes[symbol][exit_date] / entry_open[symbol])
                for symbol, weight in target.items()
            }
            final_total = cash_weight + sum(final_values.values())
            prior_weights = {
                symbol: value / final_total
                for symbol, value in final_values.items()
            }
            prior_weights["CASH"] = cash_weight / final_total
            output_windows.append(
                {
                    "train_start": window["train_start"].isoformat(),
                    "train_end": window["train_end"].isoformat(),
                    "test_start": exec_plan["entry_date"].isoformat(),
                    "test_end": exit_date.isoformat(),
                    "gross_return": float(gross_return),
                    "cost_return": float(cost_return),
                    "net_return": float(net_return),
                    "benchmark_return": float(benchmark_return),
                    "excess_return": float(excess_return),
                    "turnover": float(turnover),
                    "sharpe": _annualized_sharpe(window_daily),
                    "max_drawdown": _maximum_drawdown(window_daily),
                    "sessions": len(exec_dates),
                    "selected_assets": len(selected),
                    "daily_net_returns": [
                        float(value) for value in window_daily
                    ],
                }
            )
            # 逐窗宇宙诊断（报告 universe_diagnostics 段的原始素材）：
            # 活跃数 / 历史不足剔除数 / 退市退出数 / 停牌结转日数
            diagnostics_rows.append(
                {
                    "window": index,
                    "active_members": len(active),
                    "selected_assets": len(selected),
                    "train_history_excluded": train_history_excluded,
                    "delisting_exits": exec_plan["delisting_exits"],
                    "suspension_carry_days": exec_plan["suspension_carry_days"],
                    "test_sessions": len(exec_dates),
                }
            )
            for symbol in [*active_symbols, benchmark_symbol]:
                for day, row in all_prices[symbol].items():
                    if window["train_start"] <= day <= exit_date:
                        provenance_rows.append(
                            {
                                "window": index,
                                "symbol": symbol,
                                **row,
                            }
                        )

        total_test_days = sum(
            (
                _iso_date(item["test_end"], "TEST_END_INVALID")
                - _iso_date(item["test_start"], "TEST_START_INVALID")
            ).days
            + 1
            for item in output_windows
        )
        oos_years = total_test_days / 365.25
        compounded = float(
            np.prod([1.0 + row["net_return"] for row in output_windows]) - 1.0
        )
        avg_excess = float(
            np.mean([row["excess_return"] for row in output_windows])
        )
        summary = {
            "avg_annual_return": (
                float((1.0 + compounded) ** (1.0 / oos_years) - 1.0)
                if oos_years > 0 and compounded > -1.0
                else -1.0
            ),
            "positive_window_ratio": float(
                np.mean(
                    [row["excess_return"] > 0 for row in output_windows]
                )
            ),
            "positive_window_definition": "excess_return_after_cost",
            "avg_sharpe": float(
                np.mean([row["sharpe"] for row in output_windows])
            ),
            "max_drawdown": _maximum_drawdown(all_daily_returns),
            "turnover": float(
                sum(row["turnover"] for row in output_windows) / oos_years
            ),
            "avg_excess_return": avg_excess,
        }
        provenance_payload = {
            "memberships": [asdict(item) for item in memberships],
            "price_evidence": [asdict(item) for item in price_evidence],
            "rows": provenance_rows,
        }
        return {
            "schema_version": EVIDENCE_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cost_model": {
                "included": True,
                "id": cost_model.model_id,
                "commission_bps": float(cost_model.commission_bps),
                "slippage_bps": float(cost_model.slippage_bps),
            },
            "benchmark": {
                "id": benchmark_id,
                "symbol": benchmark_symbol,
                "price_basis": TOTAL_RETURN_BASIS,
            },
            "oos_protocol": {
                "point_in_time": True,
                "execution_lag_sessions": 1,
                "train_months": self.train_months,
                "test_months": self.test_months,
                "step_months": self.step_months,
                "universe_membership_sources": sorted(
                    {item.source_id for item in memberships}
                ),
            },
            "data_provenance": {
                "sha256": hashlib.sha256(
                    json.dumps(
                        provenance_payload,
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest(),
                "price_source_ids": sorted(
                    {item.source_id for item in price_evidence}
                ),
                "missing_prices_zero_filled": False,
                "survivorship_free_membership_required": True,
            },
            # 逐窗宇宙诊断（schema 仍为 foundf.walk_forward_evidence.v1，
            # evidence_adapter 只读取既有字段，新增段不触发合同拒绝）：
            # 活跃数 / 历史不足剔除数 / 退市强制退出数 / 停牌结转日数
            "universe_diagnostics": {
                "pit_v2_semantics": self.pit_v2_mode,
                "windows": diagnostics_rows,
                "totals": {
                    "train_history_excluded": sum(
                        row["train_history_excluded"] for row in diagnostics_rows
                    ),
                    "delisting_exits": sum(
                        row["delisting_exits"] for row in diagnostics_rows
                    ),
                    "suspension_carry_days": sum(
                        row["suspension_carry_days"] for row in diagnostics_rows
                    ),
                },
            },
            "windows": output_windows,
            "summary": summary,
            "research_only": True,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    @staticmethod
    def generate_report(
        evidence: Mapping[str, Any],
        output_dir: str | Path = "strategy_report",
    ) -> Path:
        if evidence.get("schema_version") != EVIDENCE_SCHEMA:
            raise WalkForwardDataError("UNKNOWN_EVIDENCE_SCHEMA")
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        generated = str(evidence.get("generated_at", "")).replace(":", "").replace(
            "-", ""
        )
        suffix = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
        path = out / f"walk_forward_{generated}_{suffix}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path


__all__ = [
    "CostModel",
    "EVIDENCE_SCHEMA",
    "PitStatusView",
    "PriceSeriesEvidence",
    "TOTAL_RETURN_BASIS",
    "UniverseMembership",
    "WalkForwardDataError",
    "WalkForwardEngine",
    "WindowContext",
]
