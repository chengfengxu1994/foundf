"""Validation contract for externally prepared daily portfolio snapshots."""

from __future__ import annotations

from datetime import date
import math
from typing import Any, Mapping


SUPPORTED_CURRENCIES = {"CNY", "HKD", "USD"}
READY_STATUS = "READY"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _close(left: float, right: float, absolute: float = 1.0) -> bool:
    return abs(left - right) <= max(absolute, abs(right) * 0.002)


def validate_daily_position_update(
    payload: Mapping[str, Any],
    reconciliation_tolerance_cny: float = 10.0,
) -> dict[str, Any]:
    """Validate totals, dates, currencies and FX conversion without guessing."""

    errors: list[str] = []
    warnings: list[str] = []
    if payload.get("schema_version") != "foundf.daily_position.v1":
        errors.append("SCHEMA_VERSION_INVALID")
    try:
        as_of = date.fromisoformat(str(payload.get("as_of", ""))[:10])
    except ValueError:
        as_of = None
        errors.append("AS_OF_INVALID")
    if payload.get("base_currency") != "CNY":
        errors.append("BASE_CURRENCY_MUST_BE_CNY")

    positions = payload.get("positions")
    cash_balances = payload.get("cash_balances")
    totals = payload.get("totals")
    if not isinstance(positions, list):
        positions = []
        errors.append("POSITIONS_INVALID")
    if not isinstance(cash_balances, list):
        cash_balances = []
        errors.append("CASH_BALANCES_INVALID")
    if not isinstance(totals, Mapping):
        totals = {}
        errors.append("TOTALS_INVALID")

    seen: set[str] = set()
    position_sum = 0.0
    priced_count = 0
    for index, row in enumerate(positions):
        prefix = f"POSITION_{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}_INVALID")
            continue
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            errors.append(f"{prefix}_SYMBOL_MISSING")
        elif symbol in seen:
            errors.append(f"{prefix}_SYMBOL_DUPLICATE")
        seen.add(symbol)
        currency = str(row.get("currency", "")).upper()
        if currency not in SUPPORTED_CURRENCIES:
            errors.append(f"{prefix}_CURRENCY_INVALID")
        shares = _number(row.get("shares"))
        price = _number(row.get("close_price_native"))
        fx = _number(row.get("fx_to_cny"))
        value_native = _number(row.get("market_value_native"))
        value_cny = _number(row.get("market_value_cny"))
        if any(value is None for value in (shares, price, fx, value_native, value_cny)):
            errors.append(f"{prefix}_VALUATION_MISSING")
            continue
        assert shares is not None and price is not None and fx is not None
        assert value_native is not None and value_cny is not None
        if shares < 0 or price <= 0 or fx <= 0 or value_native < 0 or value_cny < 0:
            errors.append(f"{prefix}_VALUATION_NON_POSITIVE")
            continue
        if currency == "CNY" and not _close(fx, 1.0, absolute=0.000001):
            errors.append(f"{prefix}_CNY_FX_MUST_EQUAL_ONE")
        if not _close(value_native, shares * price):
            errors.append(f"{prefix}_NATIVE_VALUE_MISMATCH")
        if not _close(value_cny, value_native * fx):
            errors.append(f"{prefix}_CNY_VALUE_MISMATCH")
        try:
            price_date = date.fromisoformat(str(row.get("price_date", ""))[:10])
            if as_of and price_date > as_of:
                errors.append(f"{prefix}_PRICE_DATE_AFTER_AS_OF")
        except ValueError:
            errors.append(f"{prefix}_PRICE_DATE_INVALID")
        if not row.get("price_source"):
            errors.append(f"{prefix}_PRICE_SOURCE_MISSING")
        if not row.get("fx_source"):
            errors.append(f"{prefix}_FX_SOURCE_MISSING")
        try:
            datetime_value = str(row.get("fx_timestamp", ""))
            if "T" not in datetime_value:
                raise ValueError
            # 这里只验证 ISO-8601 结构；时区是否符合来源由采集层继续检查。
            from datetime import datetime

            datetime.fromisoformat(datetime_value.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{prefix}_FX_TIMESTAMP_INVALID")
        position_sum += value_cny
        priced_count += 1

    cash_sum = 0.0
    for index, row in enumerate(cash_balances):
        prefix = f"CASH_{index}"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}_INVALID")
            continue
        currency = str(row.get("currency", "")).upper()
        native = _number(row.get("balance_native"))
        fx = _number(row.get("fx_to_cny"))
        value_cny = _number(row.get("balance_cny"))
        if currency not in SUPPORTED_CURRENCIES:
            errors.append(f"{prefix}_CURRENCY_INVALID")
        if any(value is None for value in (native, fx, value_cny)):
            errors.append(f"{prefix}_VALUATION_MISSING")
            continue
        assert native is not None and fx is not None and value_cny is not None
        if fx <= 0 or not _close(value_cny, native * fx):
            errors.append(f"{prefix}_CNY_VALUE_MISMATCH")
        if currency == "CNY" and not _close(fx, 1.0, absolute=0.000001):
            errors.append(f"{prefix}_CNY_FX_MUST_EQUAL_ONE")
        if not row.get("fx_source"):
            errors.append(f"{prefix}_FX_SOURCE_MISSING")
        try:
            datetime_value = str(row.get("fx_timestamp", ""))
            if "T" not in datetime_value:
                raise ValueError
            from datetime import datetime

            datetime.fromisoformat(datetime_value.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{prefix}_FX_TIMESTAMP_INVALID")
        cash_sum += value_cny

    stated_positions = _number(totals.get("positions_market_value_cny"))
    stated_cash = _number(totals.get("cash_cny"))
    stated_total = _number(totals.get("total_asset_cny"))
    if stated_positions is None or not _close(stated_positions, position_sum):
        errors.append("POSITIONS_TOTAL_MISMATCH")
    if stated_cash is None or not _close(stated_cash, cash_sum):
        errors.append("CASH_TOTAL_MISMATCH")
    calculated_total = position_sum + cash_sum
    if stated_total is None or not _close(stated_total, calculated_total):
        errors.append("TOTAL_ASSET_MISMATCH")

    broker_total = _number(totals.get("broker_total_asset_cny"))
    difference = _number(totals.get("reconciliation_difference_cny"))
    if broker_total is None:
        errors.append("BROKER_TOTAL_MISSING")
    elif stated_total is not None:
        calculated_difference = stated_total - broker_total
        if difference is None or not _close(
            difference, calculated_difference, absolute=0.01
        ):
            errors.append("RECONCILIATION_DIFFERENCE_MISMATCH")
        if abs(calculated_difference) > reconciliation_tolerance_cny:
            errors.append("BROKER_RECONCILIATION_FAILED")

    coverage = priced_count / len(positions) if positions else 0.0
    if coverage < 1:
        errors.append("VALUATION_COVERAGE_INCOMPLETE")
    if payload.get("status") == READY_STATUS and errors:
        errors.append("READY_STATUS_CONTRADICTS_VALIDATION")
    if payload.get("status") != READY_STATUS:
        warnings.append("UPSTREAM_STATUS_BLOCKED")

    return {
        "valid": not errors and payload.get("status") == READY_STATUS,
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
        "as_of": as_of.isoformat() if as_of else None,
        "positions_count": len(positions),
        "priced_positions_count": priced_count,
        "valuation_coverage": round(coverage, 6),
        "calculated_positions_market_value_cny": round(position_sum, 2),
        "calculated_cash_cny": round(cash_sum, 2),
        "calculated_total_asset_cny": round(calculated_total, 2),
    }
