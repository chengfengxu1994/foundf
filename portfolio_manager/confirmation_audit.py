"""导入与调仓计划的显式确认留痕，不包含任何券商执行能力。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _payload_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_explicit_confirmation(
    payload: Mapping[str, Any],
    *,
    action_type: str,
    actor: str,
    confirmed: bool,
    confirmation_text: str,
    audit_path: str | Path = "data/governance/user_confirmations.jsonl",
) -> dict[str, Any]:
    """只有 ``confirmed=True`` 且确认文字完全匹配时才追加审计记录。"""

    expected = f"CONFIRM {action_type.upper()}"
    if not confirmed or confirmation_text.strip().upper() != expected:
        return {
            "recorded": False,
            "reason": "EXPLICIT_CONFIRMATION_REQUIRED",
            "expected_confirmation": expected,
        }
    if not actor.strip():
        raise ValueError("actor is required")
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "action_type": action_type.upper(),
        "actor": actor,
        "payload_hash": _payload_hash(payload),
        "confirmation_text": expected,
        "execution_authorized": False,
        "note": "仅确认导入或模拟计划，不授权券商交易。",
    }
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"recorded": True, "entry": entry}

