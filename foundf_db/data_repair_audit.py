"""Immutable audit records for controlled data-asset repair batches."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backup import verify_backup
from .event_store import resolve_data_root


REPAIR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
VALID_STATUSES = {"COMPLETED", "PARTIAL", "FAILED"}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_data_repair(
    *,
    repair_id: str,
    scope: str,
    status: str,
    backup_path: str | Path,
    before_health: dict[str, Any],
    after_health: dict[str, Any],
    operations: list[dict[str, Any]],
    source_artifacts: list[str | Path],
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Write one immutable repair record after validating its evidence."""

    if not REPAIR_ID_RE.fullmatch(repair_id):
        raise ValueError("repair_id must be lowercase ASCII with '-'/'_' only")
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported repair status: {status}")
    if not scope.strip() or not operations:
        raise ValueError("scope and operations are required")

    root = resolve_data_root(data_root)
    backup = Path(backup_path)
    backup_result = verify_backup(backup)
    if backup_result["status"] != "VALID":
        raise ValueError("backup evidence is not valid")

    artifact_paths = []
    for raw_path in source_artifacts:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        artifact_paths.append(str(path.resolve()))

    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": "foundf.data_repair_audit.v1",
        "repair_id": repair_id,
        "scope": scope.strip(),
        "status": status,
        "created_at": created_at,
        "backup": {
            "path": str(backup.resolve()),
            "verification": backup_result,
        },
        "before_health": before_health,
        "before_health_sha256": _digest(before_health),
        "after_health": after_health,
        "after_health_sha256": _digest(after_health),
        "operations": operations,
        "source_artifacts": artifact_paths,
        "safety": {
            "placeholder_records_allowed": False,
            "missing_prices_filled_with_zero": False,
            "decision_gate_bypassed": False,
        },
    }
    payload["record_sha256"] = _digest(payload)

    audit_dir = root / "governance" / "data_repairs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    output = audit_dir / f"{repair_id}.json"
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return {
        "status": "RECORDED",
        "repair_id": repair_id,
        "path": str(output),
        "record_sha256": payload["record_sha256"],
    }
