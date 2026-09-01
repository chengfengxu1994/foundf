"""Fail-closed, read-only projection of strategy-governance evidence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "foundf.strategy_evolution.v1"
ALLOWED_STAGES = {
    "BLOCKED_DATA",
    "BLOCKED_FACTORS",
    "BLOCKED_BACKTEST",
    "ELIGIBLE_FOR_PAPER",
    "PENDING_HUMAN_REVIEW",
}


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "schema_version": "foundf.strategy_governance_projection.v1",
        "status": "UNAVAILABLE",
        "stage": "BLOCKED_DATA",
        "evidence_ready": False,
        "evidence_fresh": False,
        "blockers": [reason],
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
        "human_approval_required": True,
        "data_gate": {"passed": False, "failures": [reason]},
        "factor_gate": {"passed": False, "failures": [reason]},
        "backtest_gate": {"passed": False, "failures": [reason]},
        "paper_gate": {"passed": False, "days": 0, "minimum_days": 90},
        "walk_forward_evidence": {
            "status": "UNAVAILABLE",
            "rejection_code": reason,
        },
        "walk_forward_inputs": {
            "status": "UNAVAILABLE",
            "distinct_symbol_count": 0,
            "minimum_active_universe": 0,
            "series_expected": 0,
            "series_verified": 0,
            "basis_approved": False,
            "blockers": [reason],
        },
    }


def load_governance_status(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_age_hours: int = 36,
    walk_forward_input_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a governance status without running research or mutating state."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blocked("STRATEGY_GOVERNANCE_STATUS_MISSING")
    except (OSError, json.JSONDecodeError):
        return _blocked("STRATEGY_GOVERNANCE_STATUS_INVALID")
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return _blocked("STRATEGY_GOVERNANCE_SCHEMA_UNSUPPORTED")
    stage = raw.get("stage")
    if stage not in ALLOWED_STAGES:
        return _blocked("STRATEGY_GOVERNANCE_STAGE_INVALID")
    if (
        raw.get("production_change_allowed") is not False
        or raw.get("automatic_trade_allowed") is not False
        or raw.get("human_approval_required") is not True
    ):
        return _blocked("STRATEGY_GOVERNANCE_SAFETY_INVARIANT_FAILED")

    try:
        generated_at = datetime.fromisoformat(
            str(raw["generated_at"]).replace("Z", "+00:00")
        )
        if generated_at.tzinfo is None:
            raise ValueError
        generated_at = generated_at.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return _blocked("STRATEGY_GOVERNANCE_TIMESTAMP_INVALID")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current - generated_at
    if age < timedelta(minutes=-5):
        return _blocked("STRATEGY_GOVERNANCE_TIMESTAMP_IN_FUTURE")
    fresh = age <= timedelta(hours=max_age_hours)

    gates: dict[str, dict[str, Any]] = {}
    for name in ("data_gate", "factor_gate", "backtest_gate", "paper_gate"):
        value = raw.get(name)
        if not isinstance(value, dict) or not isinstance(value.get("passed"), bool):
            return _blocked("STRATEGY_GOVERNANCE_GATE_INVALID")
        gates[name] = value

    blockers = []
    if not fresh:
        blockers.append("STRATEGY_GOVERNANCE_STATUS_STALE")
    for name in ("data_gate", "factor_gate", "backtest_gate"):
        blockers.extend(str(item) for item in gates[name].get("failures", []))
    if not gates["paper_gate"]["passed"]:
        blockers.append("PAPER_OBSERVATION_INSUFFICIENT")
    raw_walk_forward = raw.get("walk_forward_evidence", {})
    if not isinstance(raw_walk_forward, dict):
        raw_walk_forward = {}
    walk_forward_evidence = {
        "status": str(raw_walk_forward.get("status", "MISSING")),
        "rejection_code": raw_walk_forward.get("rejection_code"),
        "source_schema_version": raw_walk_forward.get(
            "source_schema_version"
        ),
        "strategy_id": raw_walk_forward.get("strategy_id"),
        "strategy_version": raw_walk_forward.get("strategy_version"),
    }
    raw_inputs = (
        walk_forward_input_status
        if isinstance(walk_forward_input_status, Mapping)
        else raw.get("walk_forward_inputs", {})
    )
    if not isinstance(raw_inputs, Mapping):
        raw_inputs = {}
    input_status = str(raw_inputs.get("status", "MISSING"))
    allowed_input_statuses = {
        "MISSING",
        "NOT_READY",
        "READY_FOR_RESEARCH",
        "UNAVAILABLE",
    }
    safety_valid = (
        raw_inputs.get("production_change_allowed", False) is False
        and raw_inputs.get("automatic_trade_allowed", False) is False
    )
    if input_status not in allowed_input_statuses or not safety_valid:
        input_status = "UNAVAILABLE"
        input_blockers = ["WALK_FORWARD_INPUT_STATUS_INVALID"]
    else:
        raw_input_blockers = raw_inputs.get("blockers", [])
        input_blockers = (
            [str(item) for item in raw_input_blockers]
            if isinstance(raw_input_blockers, list)
            else ["WALK_FORWARD_INPUT_STATUS_INVALID"]
        )

    def input_count(name: str, default: int = 0) -> int:
        nonlocal input_status, input_blockers
        value = raw_inputs.get(name, default)
        if isinstance(value, bool):
            input_status = "UNAVAILABLE"
            input_blockers = ["WALK_FORWARD_INPUT_STATUS_INVALID"]
            return default
        try:
            result = int(value)
        except (TypeError, ValueError):
            input_status = "UNAVAILABLE"
            input_blockers = ["WALK_FORWARD_INPUT_STATUS_INVALID"]
            return default
        if result < 0:
            input_status = "UNAVAILABLE"
            input_blockers = ["WALK_FORWARD_INPUT_STATUS_INVALID"]
            return default
        return result

    minimum_universe_size = input_count("minimum_universe_size", 100)
    distinct_symbol_count = input_count("distinct_symbol_count")
    minimum_active_universe = input_count("minimum_active_universe")
    series_expected = input_count("series_expected")
    series_verified = input_count("series_verified")
    if input_status != "READY_FOR_RESEARCH":
        blockers.extend(input_blockers or ["WALK_FORWARD_INPUTS_NOT_READY"])
    walk_forward_inputs = {
        "status": input_status,
        "dataset_id": raw_inputs.get("dataset_id"),
        "universe_id": raw_inputs.get("universe_id"),
        "as_of": raw_inputs.get("as_of"),
        "research_start": raw_inputs.get("research_start"),
        "research_end": raw_inputs.get("research_end"),
        "minimum_universe_size": minimum_universe_size,
        "distinct_symbol_count": distinct_symbol_count,
        "minimum_active_universe": minimum_active_universe,
        "series_expected": series_expected,
        "series_verified": series_verified,
        "basis_approved": raw_inputs.get("basis_approved") is True,
        "blockers": input_blockers,
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
    }

    return {
        "schema_version": "foundf.strategy_governance_projection.v1",
        "status": "READY" if fresh else "STALE",
        "stage": stage,
        "generated_at": generated_at.isoformat(),
        "age_hours": round(max(age.total_seconds(), 0) / 3600, 2),
        "evidence_ready": fresh,
        "evidence_fresh": fresh,
        "blockers": list(dict.fromkeys(blockers)),
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
        "human_approval_required": True,
        "policy_id": raw.get("policy_id"),
        "policy_version": raw.get("policy_version"),
        "requirements": raw.get("requirements", {}),
        "evidence_hash": raw.get("evidence_hash"),
        "data_gate": gates["data_gate"],
        "factor_gate": gates["factor_gate"],
        "backtest_gate": gates["backtest_gate"],
        "paper_gate": gates["paper_gate"],
        "walk_forward_evidence": walk_forward_evidence,
        "walk_forward_inputs": walk_forward_inputs,
        "factor_lifecycle": raw.get("factor_lifecycle", {}),
        "terminology": {
            "paper": "前向研究观察",
            "broker_simulation": "国信模拟成交",
        },
        "disclaimer": (
            "此状态只展示策略证据门禁，不授权改权重、生成交易金额或提交订单。"
        ),
    }
