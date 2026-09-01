"""风险策略配置的内容哈希与显式变更审计。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def policy_hash(policy: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


def append_policy_audit(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    actor: str,
    reason: str,
    approved: bool,
    audit_path: str | Path = "data/governance/risk_policy_audit.jsonl",
) -> dict[str, Any]:
    """追加策略变更审计；调用方必须显式传入审批状态。"""

    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "reason": reason,
        "approved": bool(approved),
        "before_hash": policy_hash(before),
        "after_hash": policy_hash(after),
        "before_version": before.get("policy_version"),
        "after_version": after.get("policy_version"),
        "changes": _diff(before, after),
    }
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry

