"""
failure_feedback.py — Phase R2: Failure Memory Integration.

失败案例库参与未来决策，而不只是事后报告。

核心机制（Pre-trade Risk Check）：
    AI / 量化模型 / 人工准备建议买入之前，必须先执行 failure_check()。
    若该标的（或类似交易模式）历史上曾导致亏损，降低信心并输出风险警告。

工具：
    query_failure_cases(symbol)  — 查询该标的 + 类似模式的历史失败记录
    failure_check(symbol, ...)   — 交易前强制检查，返回调整后信心 + risk_warning

数据来源：data/failure_memory/failure_cases.csv（Phase R 沉淀的 25 个亏损标的、
总损失 -102,610，含 BAD_TIMING / CHASING_HIGH / AVERAGED_DOWN / CUT_LOSS_TOO_LATE 分类）

纪律守门（冲动检测、冷静期）由 portfolio_manager.discipline_engine 负责，
本模块在此之上叠加"类似历史交易"维度。
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

FAILURE_DB = Path("data/failure_memory")

# 信心扣分规则（基于历史亏损严重度）
SEVERITY_PENALTY = {"HIGH": 0.30, "MEDIUM": 0.20, "LOW": 0.10}
SIMILAR_CASE_PENALTY = 0.05   # 每个类似失败案例额外扣分（封顶）
SIMILAR_PENALTY_CAP = 0.15


def _load_cases() -> list[dict[str, Any]]:
    p = FAILURE_DB / "failure_cases.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_loss"] = float(r.get("loss_amount", 0) or 0)
        r["_win_rate"] = float(r.get("win_rate", 0) or 0)
        r["_trades"] = int(float(r.get("trade_count", 0) or 0))
        r["_avg_hold"] = float(r.get("avg_holding_days", 0) or 0)
    return rows


def query_failure_cases(symbol: str, include_similar: bool = True) -> dict[str, Any]:
    """查询某标的的失败记忆 + 类似交易模式的失败记录。

    Args:
        symbol: 股票代码
        include_similar: 是否包含"类似模式"案例
            （同失败类型 / 同交易频率与持仓周期特征的其他标的）

    Returns:
        exact: 该标的自身的失败记录（无则 None）
        similar_count: 类似模式失败案例数
        similar_avg_loss: 类似案例平均亏损
        similar_avg_win_rate: 类似案例平均胜率
        failure_reasons: 失败原因分布
    """
    cases = _load_cases()
    exact = next((c for c in cases if c["symbol"] == symbol), None)

    similar: list[dict[str, Any]] = []
    if include_similar and cases:
        if exact:
            # 有自身记录：找同失败类型的其他标的
            similar = [c for c in cases
                       if c["symbol"] != symbol
                       and c["failure_type"] == exact["failure_type"]]
        else:
            # 无自身记录：找"短线高频亏损"这一用户主导失败模式的案例
            similar = [c for c in cases
                       if c["_avg_hold"] < 7 and c["_trades"] >= 3]

    reasons: dict[str, int] = defaultdict(int)
    for c in ([exact] if exact else []) + similar:
        reasons[c["failure_type"]] += 1

    return {
        "symbol": symbol,
        "exact": {
            "loss_amount": exact["_loss"],
            "win_rate_pct": exact["_win_rate"],
            "trade_count": exact["_trades"],
            "avg_holding_days": exact["_avg_hold"],
            "failure_type": exact["failure_type"],
            "failure_description": exact.get("failure_description", ""),
            "severity": exact.get("severity", "LOW"),
            "pattern_note": exact.get("pattern_note", ""),
        } if exact else None,
        "similar_count": len(similar),
        "similar_avg_loss": round(sum(c["_loss"] for c in similar) / len(similar), 2) if similar else 0.0,
        "similar_avg_win_rate": round(
            sum(c["_win_rate"] for c in similar) / len(similar), 1) if similar else None,
        "failure_reasons": dict(reasons),
    }


def failure_check(symbol: str, action: str = "BUY",
                  confidence: float = 0.5,
                  run_discipline_gate: bool = True) -> dict[str, Any]:
    """交易前强制失败检查。

    任何 BUY 建议（AI / FACTOR_MODEL / USER）生成后、记录或执行前必须调用。

    Returns:
        allowed: 是否允许（BLOCK 时 False）
        original_confidence / adjusted_confidence
        risk_warning: 人类可读警告（无风险时为空串）
        failure_evidence: query_failure_cases 的完整证据
        discipline: 纪律守门结果（run_discipline_gate=True 时）
    """
    action = action.upper()
    evidence = query_failure_cases(symbol)
    warning_parts: list[str] = []
    penalty = 0.0

    # 自身失败记录
    exact = evidence["exact"]
    if exact and action in ("BUY", "ADD"):
        sev = exact["severity"]
        penalty += SEVERITY_PENALTY.get(sev, 0.10)
        warning_parts.append(
            f"类似历史交易曾导致亏损: {symbol} 历史净亏损 {exact['loss_amount']:+,.0f}，"
            f"胜率 {exact['win_rate_pct']:.0f}%，失败类型 {exact['failure_type']}"
            f"（{exact['pattern_note']}）"
        )

    # 类似模式失败记录
    if evidence["similar_count"] >= 3 and action in ("BUY", "ADD"):
        similar_penalty = min(evidence["similar_count"] * SIMILAR_CASE_PENALTY,
                              SIMILAR_PENALTY_CAP)
        penalty += similar_penalty
        avg_wr = evidence["similar_avg_win_rate"]
        warning_parts.append(
            f"发现 {evidence['similar_count']} 例类似模式的失败交易"
            f"（平均亏损 {evidence['similar_avg_loss']:+,.0f}"
            + (f"，平均胜率 {avg_wr:.0f}%" if avg_wr is not None else "")
            + f"，主因: {', '.join(f'{k}x{v}' for k, v in evidence['failure_reasons'].items())}）"
        )

    # 纪律守门（Phase S）
    discipline = None
    if run_discipline_gate:
        from portfolio_manager.discipline_engine import DisciplineEngine
        discipline = DisciplineEngine().pre_trade_check(symbol, action, confidence)
        penalty = max(penalty, discipline["confidence_penalty"])
        if discipline["verdict"] == "BLOCK":
            warning_parts.append("纪律守门判定 BLOCK（冲动交易/冷静期信号叠加）")

    adjusted = max(0.0, round(confidence - penalty, 2))
    blocked = discipline is not None and discipline["verdict"] == "BLOCK"

    return {
        "symbol": symbol,
        "action": action,
        "allowed": not blocked,
        "original_confidence": confidence,
        "adjusted_confidence": adjusted,
        "confidence_penalty": round(penalty, 2),
        "risk_warning": "；".join(warning_parts),
        "failure_evidence": evidence,
        "discipline": discipline,
    }


# ── CLI ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python -m investment_agent.failure_feedback query <symbol>")
        print("  python -m investment_agent.failure_feedback check <symbol> [BUY|SELL] [confidence]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "query" and len(sys.argv) >= 3:
        r = query_failure_cases(sys.argv[2])
        print(f"=== Failure Memory Query: {r['symbol']} ===\n")
        if r["exact"]:
            e = r["exact"]
            print(f"自身记录: 亏损 {e['loss_amount']:+,.0f} | 胜率 {e['win_rate_pct']:.0f}% | "
                  f"{e['trade_count']}笔 | 平均持仓 {e['avg_holding_days']:.0f}天")
            print(f"失败类型: {e['failure_type']} ({e['severity']}) — {e['failure_description']}")
        else:
            print("自身记录: 无")
        print(f"\n类似模式案例: {r['similar_count']} 例"
              + (f"，平均亏损 {r['similar_avg_loss']:+,.0f}" if r["similar_count"] else ""))
        if r["failure_reasons"]:
            print(f"失败原因分布: {r['failure_reasons']}")

    elif cmd == "check" and len(sys.argv) >= 3:
        symbol = sys.argv[2]
        action = sys.argv[3] if len(sys.argv) > 3 else "BUY"
        conf = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
        r = failure_check(symbol, action, conf)
        print(f"=== Pre-trade Failure Check: {symbol} {action} ===\n")
        print(f"Allowed: {r['allowed']}")
        print(f"Confidence: {r['original_confidence']} → {r['adjusted_confidence']}")
        if r["risk_warning"]:
            print(f"\nrisk_warning:\n  ⚠️  {r['risk_warning']}")
        else:
            print("\n无失败记忆风险。")
        sys.exit(0 if r["allowed"] else 2)

    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
