"""分层资产清单校验：汇总控制数与明细只在树上计算一次。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def validate_asset_inventory(
    payload_or_path: dict[str, Any] | str | Path,
    *,
    tolerance_cny: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    payload = (
        json.loads(Path(payload_or_path).read_text(encoding="utf-8"))
        if isinstance(payload_or_path, (str, Path))
        else payload_or_path
    )
    if payload.get("schema_version") != "foundf.asset_inventory.v1":
        raise ValueError("unsupported asset inventory schema")
    root = payload.get("asset_tree")
    if not isinstance(root, dict):
        raise ValueError("asset_tree is required")
    reconciliation = []
    blockers = set(payload.get("declared_blockers") or [])

    def walk(node: dict[str, Any], path: str) -> Decimal:
        amount = _money(node.get("amount_cny", 0))
        children = list(node.get("children") or [])
        if children:
            child_total = sum(
                (walk(child, f"{path}/{child.get('node_id', '?')}") for child in children),
                Decimal("0.00"),
            )
            difference = (amount - child_total).quantize(Decimal("0.01"))
            reconciliation.append(
                {
                    "path": path,
                    "control_total_cny": float(amount),
                    "children_total_cny": float(child_total),
                    "difference_cny": float(difference),
                    "passed": abs(difference) <= tolerance_cny,
                }
            )
            if abs(difference) > tolerance_cny:
                blockers.add("HIERARCHY_RECONCILIATION_FAILED")
        if node.get("classification") == "UNRESOLVED":
            blockers.add("UNRESOLVED_ASSET_AMOUNT")
        details = node.get("details") or {}
        if details.get("quantity") not in (None, 0):
            if not details.get("price_date"):
                blockers.add("PRICE_DATE_MISSING")
            if not details.get("instrument_id"):
                blockers.add("INSTRUMENT_ID_MISSING")
            if details.get("native_currency") not in (None, "CNY"):
                if details.get("fx_to_cny") is None:
                    blockers.add("FX_RATE_MISSING")
        return amount

    total = walk(root, str(root.get("node_id", "TOTAL")))
    declared = _money(payload.get("declared_total_asset_cny", 0))
    total_difference = (declared - total).quantize(Decimal("0.01"))
    if abs(total_difference) > tolerance_cny:
        blockers.add("DECLARED_TOTAL_MISMATCH")
    ready = not blockers and payload.get("confirmation_status") == "CONFIRMED"
    return {
        "status": "READY" if ready else "REVIEW_REQUIRED",
        "ready_for_valuation": ready,
        "declared_total_asset_cny": float(declared),
        "tree_total_asset_cny": float(total),
        "total_difference_cny": float(total_difference),
        "reconciliation": reconciliation,
        "blockers": sorted(blockers),
    }
