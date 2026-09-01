"""投资政策声明（IPS）的版本化模型与中文文档投影。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class InvestmentPolicyStatement:
    ips_id: str
    version: str
    confirmed: bool
    effective_date: str | None
    clauses: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "InvestmentPolicyStatement":
        clauses = tuple(dict(item) for item in raw.get("clauses", []))
        ids = [str(item.get("id", "")) for item in clauses]
        if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("IPS clauses require unique non-empty ids")
        return cls(
            ips_id=str(raw.get("ips_id", "foundf_ips")),
            version=str(raw.get("version", "1")),
            confirmed=bool(raw.get("confirmed", False)),
            effective_date=raw.get("effective_date"),
            clauses=clauses,
        )

    @classmethod
    def load(cls, path: str | Path = "config/ips.example.json") -> "InvestmentPolicyStatement":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def clause(self, clause_id: str) -> dict[str, Any] | None:
        return next(
            (dict(item) for item in self.clauses if item["id"] == clause_id),
            None,
        )

    def as_api(self) -> dict[str, Any]:
        return {
            "ips_id": self.ips_id,
            "version": self.version,
            "confirmed": self.confirmed,
            "effective_date": self.effective_date,
            "clauses": [dict(item) for item in self.clauses],
            "status": "ACTIVE" if self.confirmed else "DRAFT_UNCONFIRMED",
            "disclaimer": "IPS 用于约束风险预算，不构成收益承诺或自动交易授权。",
        }


def explain_violation(
    violation: Mapping[str, Any],
    *,
    contributions: list[Mapping[str, Any]],
    data_as_of: str | None,
    lookthrough_method: str,
    ips_clause_ids: list[str],
) -> dict[str, Any]:
    """把规则违规转换为可展开的完整解释链。"""

    return {
        "rule_id": violation.get("rule_id"),
        "key": violation.get("key"),
        "current_value": violation.get("current_value"),
        "threshold": violation.get("threshold"),
        "excess": violation.get("excess"),
        "affected_holdings": violation.get("affected_holdings", []),
        "recommended_action": violation.get("recommended_action"),
        "contributions": [dict(item) for item in contributions],
        "lookthrough_method": lookthrough_method,
        "data_as_of": data_as_of,
        "ips_clause_ids": ips_clause_ids,
        "uncertainty": (
            "基金披露可能滞后；数据缺失或过期时风险结论将降级，"
            "不得视为实时或完整穿透。"
        ),
    }
