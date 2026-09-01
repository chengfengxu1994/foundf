"""
failure_memory.py — Phase R: Investment Failure Memory.

Analyzes losing trades to build a searchable failure case database.
Classifies failure types: chasing, overvaluation, sector reversal, fundamental deterioration,
bad timing, oversized position, cut-loss-too-late.

Output: failure_memory_report.md + failure_cases.csv
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPORTS = Path("reports/reconciliation")
FAILURE_DB = Path("data/failure_memory")


class FailureMemory:
    """Build and analyze investment failure case database."""

    FAILURE_TYPES = {
        "CHASING_HIGH": "Buying after significant price run-up — paying inflated prices",
        "OVERVALUATION": "Entering at valuation multiples above historical/sector norms",
        "SECTOR_REVERSAL": "Industry cycle turning against position after entry",
        "FUNDAMENTAL_DETERIORATION": "Company fundamentals worsening after purchase",
        "BAD_TIMING": "Entry near short-term peak despite reasonable fundamentals",
        "OVERSIZED_POSITION": "Position size too large relative to portfolio — amplified losses",
        "CUT_LOSS_TOO_LATE": "Holding a losing position too long before cutting",
        "AVERAGED_DOWN_INTO_LOSS": "Adding to a losing position that continued declining",
        "UNKNOWN": "Could not classify failure pattern",
    }

    def __init__(self):
        self._trades = self._load_trades()
        FAILURE_DB.mkdir(parents=True, exist_ok=True)

    def _load_trades(self) -> list[dict[str, Any]]:
        p = REPORTS / "trade_analysis_per_stock.csv"
        if not p.exists():
            return []
        with open(p, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["_pnl"] = float(r.get("net_realized_pnl", 0))
            r["_trades"] = int(float(r.get("total_trades", 0)))
            r["_wins"] = int(float(r.get("wins", 0)))
            r["_losses"] = int(float(r.get("losses", 0)))
            r["_win_rate"] = float(r.get("win_rate", 0))
            r["_avg_hold"] = float(r.get("avg_holding_days", 0))
            r["_total_comm"] = float(r.get("total_commission", 0))
        return rows

    def analyze(self) -> dict[str, Any]:
        """Build failure cases from losing trades."""
        if not self._trades:
            return {"error": "No trade data"}

        # Filter to losing symbols
        losers = [t for t in self._trades if t["_pnl"] < 0]
        winners = [t for t in self._trades if t["_pnl"] > 0]

        if not losers:
            return {"note": "No losing trades to analyze"}

        # Build case files for each loser
        cases = []
        for t in losers:
            case = self._build_case(t)
            cases.append(case)

        # Statistics
        total_lost = sum(t["_pnl"] for t in losers)
        total_won = sum(t["_pnl"] for t in winners)
        loser_count = len(losers)

        # Failure type distribution
        type_dist: dict[str, int] = defaultdict(int)
        for c in cases:
            type_dist[c["failure_type"]] += 1

        # Loss magnitude distribution
        loss_buckets = {"<1000": 0, "1000-5000": 0, "5000-10000": 0, ">10000": 0}
        for c in cases:
            amt = abs(c["loss_amount"])
            if amt < 1000:
                loss_buckets["<1000"] += 1
            elif amt < 5000:
                loss_buckets["1000-5000"] += 1
            elif amt < 10000:
                loss_buckets["5000-10000"] += 1
            else:
                loss_buckets[">10000"] += 1

        # Top 5 worst losses
        worst = sorted(cases, key=lambda c: c["loss_amount"])[:5]

        # Common patterns
        patterns = self._detect_patterns(cases)

        return {
            "total_losing_symbols": loser_count,
            "total_winning_symbols": len(winners),
            "total_loss_amount": round(total_lost, 2),
            "total_win_amount": round(total_won, 2),
            "net_realized_pnl": round(total_lost + total_won, 2),
            "loss_distribution": loss_buckets,
            "failure_type_distribution": dict(type_dist),
            "failure_types": self.FAILURE_TYPES,
            "top_worst_losses": [
                {"symbol": c["symbol"], "name": c["name"],
                 "loss": c["loss_amount"], "type": c["failure_type"],
                 "trades": c["trade_count"]}
                for c in worst
            ],
            "patterns": patterns,
            "cases": cases,
        }

    def _build_case(self, trade: dict) -> dict[str, Any]:
        """Build a failure case record."""
        sym = trade["symbol"]
        loss = trade["_pnl"]
        trades = trade["_trades"]
        wins = trade["_wins"]
        losses = trade["_losses"]
        win_rate = trade["_win_rate"]
        avg_hold = trade["_avg_hold"]
        comm = trade["_total_comm"]

        # Classify failure type
        failure_type = self._classify_failure(sym, loss, trades, wins, losses, win_rate, avg_hold, comm)

        return {
            "symbol": sym,
            "name": trade.get("name", ""),
            "loss_amount": round(loss, 2),
            "trade_count": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate * 100, 1),
            "avg_holding_days": round(avg_hold, 1),
            "total_commission": round(comm, 2),
            "commission_pct_of_loss": f"{abs(comm / loss * 100):.1f}%" if loss != 0 else "0%",
            "failure_type": failure_type,
            "failure_description": self.FAILURE_TYPES.get(failure_type, "Unknown"),
            "severity": "HIGH" if abs(loss) > 5000 else "MEDIUM" if abs(loss) > 1000 else "LOW",
            "tradable": trades >= 5,
            "pattern_note": self._pattern_note(sym, loss, trades, wins, losses, avg_hold),
        }

    def _classify_failure(
        self, sym: str, loss: float, trades: int,
        wins: int, losses: int, win_rate: float,
        avg_hold: float, comm: float
    ) -> str:
        """Classify the type of failure."""
        abs_loss = abs(loss)
        loss_ratio = losses / max(trades, 1)
        is_all_losses = (wins == 0 and losses > 0)

        # Single trade big loss → oversized position or bad timing
        if trades <= 2:
            if abs_loss > 3000:
                return "OVERSIZED_POSITION"
            return "BAD_TIMING"

        # Every single trade was a loss → clear strategy failure
        if is_all_losses:
            if avg_hold < 5:
                return "CHASING_HIGH"
            return "FUNDAMENTAL_DETERIORATION"

        # High-frequency trading with majority losses → chasing / overtrading
        if trades >= 10 and loss_ratio > 0.5:
            if avg_hold < 7:
                return "CHASING_HIGH"
            return "AVERAGED_DOWN_INTO_LOSS"

        # High-frequency trading with mixed results but net loss
        if trades >= 10 and loss_ratio <= 0.5:
            if avg_hold < 5:
                return "BAD_TIMING"
            return "AVERAGED_DOWN_INTO_LOSS"

        # Very short holds with big losses → cut too late or sized wrong
        if avg_hold < 3 and abs_loss > 2000:
            return "CUT_LOSS_TOO_LATE"

        # Extended holds with losses → fundamental deterioration
        if avg_hold > 20:
            return "FUNDAMENTAL_DETERIORATION"

        # Moderate trades, low win rate → averaging down
        if trades >= 5 and win_rate < 0.35:
            return "AVERAGED_DOWN_INTO_LOSS"

        # High commission relative to loss → overtrading
        if abs(comm / max(abs_loss, 1)) > 0.15:
            return "BAD_TIMING"

        return "CUT_LOSS_TOO_LATE"  # better default than UNKNOWN

    def _pattern_note(self, sym: str, loss: float, trades: int,
                      wins: int, losses: int, avg_hold: float) -> str:
        """Generate human-readable pattern note."""
        parts = []
        if trades >= 10:
            parts.append(f"{trades} trades — repeated attempts")
        if avg_hold < 3:
            parts.append(f"very short avg hold ({avg_hold:.0f}d)")
        if avg_hold > 30:
            parts.append(f"extended avg hold ({avg_hold:.0f}d)")
        if wins == 0 and trades > 0:
            parts.append("never won a trade")
        return "; ".join(parts) if parts else "Standard loss"

    def _detect_patterns(self, cases: list[dict]) -> list[str]:
        """Detect cross-cutting patterns."""
        patterns = []

        # Count failure types
        type_counts: dict[str, int] = defaultdict(int)
        total_loss_by_type: dict[str, float] = defaultdict(float)
        for c in cases:
            type_counts[c["failure_type"]] += 1
            total_loss_by_type[c["failure_type"]] += c["loss_amount"]

        dominant_type = max(type_counts, key=type_counts.get)
        dominant_loss_type = max(total_loss_by_type, key=total_loss_by_type.get)

        patterns.append(
            f"Most frequent failure type: {dominant_type} "
            f"({type_counts[dominant_type]} cases)"
        )
        patterns.append(
            f"Largest total loss by type: {dominant_loss_type} "
            f"({total_loss_by_type[dominant_loss_type]:,.0f})"
        )

        # Loss concentration
        total_loss = sum(c["loss_amount"] for c in cases)
        top3_loss = sum(abs(c["loss_amount"]) for c in sorted(cases, key=lambda x: x["loss_amount"])[:3])
        if total_loss != 0:
            patterns.append(
                f"Top 3 losses account for {top3_loss / abs(total_loss) * 100:.1f}% "
                f"of total losses ({top3_loss:,.0f} / {abs(total_loss):,.0f})"
            )

        # High-frequency losing
        high_freq = [c for c in cases if c["trade_count"] >= 10]
        if high_freq:
            hf_loss = sum(c["loss_amount"] for c in high_freq)
            patterns.append(
                f"{len(high_freq)} symbols traded 10+ times with net loss "
                f"(total loss: {hf_loss:,.0f})"
            )
            patterns.append(
                "  → Suggests overtrading / revenge trading on these names"
            )

        return patterns


# ── CLI ──────────────────────────────────────────────

if __name__ == "__main__":
    fm = FailureMemory()
    result = fm.analyze()

    if "error" in result:
        print(f"Error: {result['error']}")
        import sys; sys.exit(1)

    print("=" * 60)
    print("  Phase R: Investment Failure Memory")
    print("=" * 60)
    print()

    print(f"Losing symbols: {result['total_losing_symbols']} / {result['total_losing_symbols'] + result['total_winning_symbols']}")
    print(f"Total loss amount: {result['total_loss_amount']:+,.0f}")
    print(f"Net realized P&L: {result['net_realized_pnl']:+,.0f}")
    print()

    print("Failure Type Distribution:")
    for ft, count in sorted(result["failure_type_distribution"].items(), key=lambda x: -x[1]):
        desc = result["failure_types"].get(ft, "")
        print(f"  {count:2d}x {ft}: {desc}")
    print()

    print("Loss Distribution:")
    for bucket, count in result["loss_distribution"].items():
        print(f"  {bucket}: {count} cases")
    print()

    print("Patterns Detected:")
    for p in result["patterns"]:
        print(f"  ⚠️  {p}")
    print()

    print("Top 5 Worst Losses:")
    print(f"  {'Symbol':10s} {'Loss':>10s} {'Trades':>7s} {'Type':25s}")
    print(f"  {'-'*55}")
    for w in result["top_worst_losses"]:
        print(f"  {w['symbol']:10s} {w['loss']:>+10.0f} {w['trades']:>7d} {w['type']:25s}")
    print()

    # Save CSV database
    csv_path = FAILURE_DB / "failure_cases.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["symbol", "name", "loss_amount", "trade_count", "wins", "losses",
                      "win_rate", "avg_holding_days", "total_commission",
                      "failure_type", "failure_description", "severity", "pattern_note"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in result["cases"]:
            w.writerow({
                "symbol": c["symbol"],
                "name": c["name"],
                "loss_amount": c["loss_amount"],
                "trade_count": c["trade_count"],
                "wins": c["wins"],
                "losses": c["losses"],
                "win_rate": c["win_rate"],
                "avg_holding_days": c["avg_holding_days"],
                "total_commission": c["total_commission"],
                "failure_type": c["failure_type"],
                "failure_description": c["failure_description"],
                "severity": c["severity"],
                "pattern_note": c["pattern_note"],
            })
    print(f"CSV saved: {csv_path}")

    # Save report
    md = []
    md.append("# Investment Failure Memory (Phase R)")
    md.append("")
    md.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d')}")
    md.append("")
    md.append("## Summary")
    md.append("")
    md.append(f"| Metric | Value |")
    md.append(f"|--------|-------|")
    md.append(f"| Losing Symbols | {result['total_losing_symbols']} |")
    md.append(f"| Winning Symbols | {result['total_winning_symbols']} |")
    md.append(f"| Total Loss Amount | {result['total_loss_amount']:+,.0f} |")
    md.append(f"| Total Win Amount | {result['total_win_amount']:+,.0f} |")
    md.append(f"| Net Realized P&L | {result['net_realized_pnl']:+,.0f} |")
    md.append("")
    md.append("## Failure Type Distribution")
    md.append("")
    md.append("| Type | Count | Description |")
    md.append("|------|-------|-------------|")
    for ft, count in sorted(result["failure_type_distribution"].items(), key=lambda x: -x[1]):
        desc = result["failure_types"].get(ft, "")
        md.append(f"| {ft} | {count} | {desc} |")
    md.append("")
    md.append("## Loss Distribution")
    md.append("")
    md.append("| Bucket | Count |")
    md.append("|--------|-------|")
    for bucket, count in result["loss_distribution"].items():
        md.append(f"| {bucket} | {count} |")
    md.append("")
    md.append("## Detected Patterns")
    md.append("")
    for p in result["patterns"]:
        md.append(f"- ⚠️  {p}")
    md.append("")
    md.append("## Top 5 Worst Losses")
    md.append("")
    md.append("| Symbol | Loss | Trades | Type |")
    md.append("|--------|------|--------|------|")
    for w in result["top_worst_losses"]:
        md.append(f"| {w['symbol']} | {w['loss']:+,.0f} | {w['trades']} | {w['type']} |")
    md.append("")
    md.append("## All Failure Cases")
    md.append("")
    md.append("| Symbol | Name | Loss | Trades | Win Rate | Avg Hold(d) | Failure Type | Severity |")
    md.append("|--------|------|------|--------|----------|-------------|--------------|----------|")
    for c in sorted(result["cases"], key=lambda x: x["loss_amount"]):
        md.append(f"| {c['symbol']} | {c['name'][:15]} | {c['loss_amount']:+,.0f} | {c['trade_count']} | {c['win_rate']}% | {c['avg_holding_days']} | {c['failure_type']} | {c['severity']} |")

    report_path = FAILURE_DB / "failure_memory_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"Report saved: {report_path}")
