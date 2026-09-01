"""Cash-flow-adjusted portfolio unit NAV ledger.

Only sufficiently priced portfolio snapshots are accepted.  External cash
flows change the number of units; investment performance changes unit NAV.
This prevents deposits and withdrawals from being mistaken for gains or
drawdowns.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


EXTERNAL_FLOWS = frozenset({"CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"})
TRUSTED_NAV_SOURCES = frozenset({"daily_pipeline"})


def _parse_date(value: str) -> date:
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    return date.fromisoformat(raw[:10])


def _external_flow_between(events_path: Path, after: date, through: date) -> float:
    if not events_path.exists():
        return 0.0
    total = 0.0
    with open(events_path, encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("event_type") not in EXTERNAL_FLOWS:
                continue
            event_date = _parse_date(row.get("date", ""))
            if after < event_date <= through:
                total += float(row.get("cash_impact", 0) or 0)
    return total


def load_trusted_nav_history(
    path: str | Path,
    *,
    min_valuation_coverage: float = 0.95,
) -> list[dict[str, Any]]:
    """Load only complete snapshots produced by the trusted NAV writer."""

    source = Path(path)
    if not source.exists():
        return []
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") not in {"1.0", "foundf.unit_nav.v1"}
        or not isinstance(raw.get("snapshots"), list)
    ):
        return []

    trusted: list[dict[str, Any]] = []
    previous_date: date | None = None
    previous_units: float | None = None
    previous_nav: float | None = None
    for row in raw["snapshots"]:
        if not isinstance(row, dict):
            return []
        try:
            as_of = _parse_date(str(row["date"]))
            total_asset = float(row["total_asset"])
            external_flow = float(row["external_flow"])
            units = float(row["units"])
            unit_nav = float(row["unit_nav"])
            coverage = float(row["valuation_coverage"])
            priced = int(row["priced_holdings"])
            total = int(row["total_holdings"])
            recorded_at = datetime.fromisoformat(
                str(row["recorded_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            return []
        if (
            total_asset <= 0
            or units <= 0
            or unit_nav <= 0
            or coverage < min_valuation_coverage
            or priced < 0
            or total < 0
            or (total > 0 and priced / total < min_valuation_coverage)
            or row.get("source") not in TRUSTED_NAV_SOURCES
            or recorded_at.tzinfo is None
            or recorded_at.astimezone(timezone.utc).date() < as_of
            or (previous_date is not None and as_of <= previous_date)
        ):
            return []
        if previous_units is None:
            if (
                not math.isclose(external_flow, 0.0, abs_tol=1e-8)
                or not math.isclose(units, total_asset, rel_tol=1e-8)
                or not math.isclose(unit_nav, 1.0, abs_tol=1e-8)
            ):
                return []
        else:
            expected_units = previous_units + external_flow / previous_nav
            if (
                expected_units <= 0
                or not math.isclose(
                    units, expected_units, rel_tol=1e-7, abs_tol=1e-7
                )
                or not math.isclose(
                    unit_nav,
                    total_asset / units,
                    rel_tol=1e-7,
                    abs_tol=1e-7,
                )
            ):
                return []
        trusted.append(row)
        previous_date = as_of
        previous_units = units
        previous_nav = unit_nav
    return trusted


class PortfolioNAVTracker:
    """Persist an idempotent daily unit-NAV history."""

    def __init__(
        self,
        history_path: str | Path = "data/portfolio_nav_history.json",
        events_path: str | Path = "reports/reconciliation/broker_economic_event_v4.csv",
        min_valuation_coverage: float = 0.95,
    ):
        self.history_path = Path(history_path)
        self.events_path = Path(events_path)
        self.min_valuation_coverage = float(min_valuation_coverage)

    def _load(self) -> dict[str, Any]:
        if not self.history_path.exists():
            return {"schema_version": "1.0", "snapshots": []}
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError("existing NAV history is unreadable") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") not in {"1.0", "foundf.unit_nav.v1"}
            or not isinstance(raw.get("snapshots"), list)
        ):
            raise ValueError("existing NAV history schema is invalid")
        if raw["snapshots"] and len(
            load_trusted_nav_history(
                self.history_path,
                min_valuation_coverage=self.min_valuation_coverage,
            )
        ) != len(raw["snapshots"]):
            raise ValueError("existing NAV history failed trusted read contract")
        return raw

    def record(
        self,
        as_of: str,
        total_asset: float,
        valuation_coverage: float,
        priced_holdings: int,
        total_holdings: int,
        source: str = "daily_pipeline",
    ) -> dict[str, Any]:
        """Record or replace one daily close.

        Returns ``accepted=False`` without writing when valuation quality is
        insufficient.
        """

        as_of_date = _parse_date(as_of)
        total_asset = float(total_asset)
        coverage = float(valuation_coverage)
        if total_asset <= 0:
            return {"accepted": False, "reason": "TOTAL_ASSET_INVALID"}
        if coverage < self.min_valuation_coverage:
            return {
                "accepted": False,
                "reason": "VALUATION_COVERAGE_LOW",
                "valuation_coverage": coverage,
            }
        if total_holdings > 0 and priced_holdings / total_holdings < self.min_valuation_coverage:
            return {
                "accepted": False,
                "reason": "PRICED_HOLDINGS_LOW",
                "priced_holdings": priced_holdings,
                "total_holdings": total_holdings,
            }

        ledger = self._load()
        snapshots = [
            row for row in ledger["snapshots"] if row.get("date") != as_of_date.isoformat()
        ]
        snapshots.sort(key=lambda row: row["date"])
        previous = snapshots[-1] if snapshots else None

        if previous is None:
            external_flow = 0.0
            units = total_asset
            unit_nav = 1.0
        else:
            previous_date = _parse_date(previous["date"])
            if as_of_date < previous_date:
                return {"accepted": False, "reason": "OUT_OF_ORDER_DATE"}
            external_flow = _external_flow_between(
                self.events_path, previous_date, as_of_date
            )
            previous_nav = float(previous["unit_nav"])
            units = float(previous["units"]) + external_flow / previous_nav
            if units <= 0:
                return {"accepted": False, "reason": "NON_POSITIVE_UNITS"}
            unit_nav = total_asset / units

        snapshot = {
            "date": as_of_date.isoformat(),
            "total_asset": round(total_asset, 2),
            "external_flow": round(external_flow, 2),
            "units": round(units, 8),
            "unit_nav": round(unit_nav, 8),
            "valuation_coverage": round(coverage, 6),
            "priced_holdings": int(priced_holdings),
            "total_holdings": int(total_holdings),
            "source": source,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        snapshots.append(snapshot)
        snapshots.sort(key=lambda row: row["date"])
        ledger["snapshots"] = snapshots
        ledger["updated_at"] = snapshot["recorded_at"]

        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.history_path)
        return {"accepted": True, "snapshot": snapshot}
