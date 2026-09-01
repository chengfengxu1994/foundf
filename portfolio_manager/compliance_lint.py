"""AI/规则建议文本的合规措辞与可追溯元数据检查。"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


PROHIBITED_PATTERNS = {
    "PROMISE_GUARANTEED_RETURN": re.compile(r"保证获利|保证盈利|稳赚不赔|必然盈利"),
    "PROMISE_PRICE_DIRECTION": re.compile(r"必涨|一定上涨|确定翻倍"),
    "FALSE_NO_RISK": re.compile(r"零风险|绝无风险"),
    "CAPITAL_GUARANTEE": re.compile(r"绝对保本|保证本金"),
}


def lint_advice(advice: Mapping[str, Any]) -> dict[str, Any]:
    text = str(advice.get("text", ""))
    violations = [
        code for code, pattern in PROHIBITED_PATTERNS.items() if pattern.search(text)
    ]
    required = {
        "rule_ids": advice.get("rule_ids"),
        "data_as_of": advice.get("data_as_of"),
        "uncertainty": advice.get("uncertainty"),
        "ips_clause_ids": advice.get("ips_clause_ids"),
    }
    for field, value in required.items():
        if value is None or value == "" or value == []:
            violations.append(f"TRACEABILITY_{field.upper()}_MISSING")
    return {"passed": not violations, "violations": violations}


def lint_advice_batch(advice_items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    results = [lint_advice(item) for item in advice_items]
    return {
        "passed": all(item["passed"] for item in results),
        "checked": len(results),
        "failed": sum(not item["passed"] for item in results),
        "results": results,
    }

