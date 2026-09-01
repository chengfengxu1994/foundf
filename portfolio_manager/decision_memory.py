"""
decision_memory.py — Phase N/N2: Investment Decision Memory System.

Records AI investment decisions and evaluates accuracy over multiple horizons.
Supports 30d, 90d, 180d, 365d evaluation windows.

Schema:
    decision_log:
        date: str           # YYYY-MM-DD
        symbol: str         # stock code
        action: str         # HOLD | BUY | SELL | REDUCE | ADD
        reason: str         # AI's reasoning
        confidence: float   # 0.0 - 1.0
        price_at_decision: float

        # Multi-horizon evaluation (filled later):
        eval_30d: {return, outcome, correct, price_after, max_dd}
        eval_90d: {return, outcome, correct, price_after, max_dd}
        eval_180d: {return, outcome, correct, price_after, max_dd}
        eval_365d: {return, outcome, correct, price_after, max_dd}
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .market_data import MarketDataFetcher, is_hk_symbol

MEMORY_DIR = Path("data/decision_memory")
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_FILE = MEMORY_DIR / "decision_log.csv"
ACCURACY_FILE = MEMORY_DIR / "accuracy_report.json"
SOURCE_COMPARISON_FILE = MEMORY_DIR / "source_comparison.json"

FIELDS = [
    "entry_id", "date", "symbol", "name", "action", "reason",
    "confidence", "price_at_decision", "decision_source",
    "eval_30d_return", "eval_30d_outcome", "eval_30d_correct",
    "eval_90d_return", "eval_90d_outcome", "eval_90d_correct",
    "eval_180d_return", "eval_180d_outcome", "eval_180d_correct",
    "eval_365d_return", "eval_365d_outcome", "eval_365d_correct",
]

# Phase N3: 决策来源。历史记录（无此字段）默认 AI_AGENT。
DECISION_SOURCES = ("USER", "AI_AGENT", "FACTOR_MODEL", "COMBINED")


class DecisionMemory:
    """Records and evaluates investment decisions."""

    def __init__(self, db_path: str = "data/finance.duckdb"):
        self._log: list[dict[str, Any]] = []
        self._db_path = db_path
        self._load()

    def _load(self) -> None:
        if MEMORY_FILE.exists():
            with open(MEMORY_FILE, encoding="utf-8-sig", newline="") as f:
                self._log = list(csv.DictReader(f))
            # Phase N3: 旧记录无 decision_source 字段，默认 AI_AGENT
            for row in self._log:
                if not row.get("decision_source"):
                    row["decision_source"] = "AI_AGENT"

    def _save(self) -> None:
        # 原子写：tmp + replace，进程中途死掉不丢决策记忆
        tmp = MEMORY_FILE.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for row in self._log:
                w.writerow({k: row.get(k, "") for k in FIELDS})
        tmp.replace(MEMORY_FILE)

    def record(self, date_str: str, symbol: str, name: str, action: str,
               reason: str, confidence: float = 0.5,
               factor_score: dict[str, float] = None,
               risk_score: float = 0.3,
               decision_source: str = "AI_AGENT") -> int:
        """Record a decision.

        Args:
            decision_source: USER | AI_AGENT | FACTOR_MODEL | COMBINED (Phase N3)

        Returns: entry index
        """
        if decision_source not in DECISION_SOURCES:
            raise ValueError(
                f"decision_source 必须是 {DECISION_SOURCES} 之一，收到: {decision_source}")
        entry = {
            "entry_id": str(len(self._log) + 1),
            "date": date_str,
            "symbol": symbol,
            "name": name,
            "action": action,
            "reason": reason,
            "confidence": str(round(confidence, 2)),
            "price_at_decision": "",
            "decision_source": decision_source,
            "eval_30d_return": "", "eval_30d_outcome": "", "eval_30d_correct": "",
            "eval_90d_return": "", "eval_90d_outcome": "", "eval_90d_correct": "",
            "eval_180d_return": "", "eval_180d_outcome": "", "eval_180d_correct": "",
            "eval_365d_return": "", "eval_365d_outcome": "", "eval_365d_correct": "",
        }
        
        # Record current price
        try:
            fetcher = MarketDataFetcher(use_cache=True)
            market = "HK" if is_hk_symbol(symbol) else "CN"
            prices = fetcher.fetch_prices([{"symbol": symbol, "market": market}])
            price = prices.get(symbol, 0.0)
            if price > 0:
                entry["price_at_decision"] = str(round(price, 4))
        except Exception:
            pass
        
        self._log.append(entry)
        self._save()
        return len(self._log) - 1

    HORIZONS = {
        "30d": 30,
        "90d": 90,
        "180d": 180,
        "365d": 365,
    }

    def _historical_price(self, symbol: str, eval_date: date) -> float | None:
        """评估窗口定价：daily_price 中 eval_date（或之前最近交易日）收盘价。

        取不到（非交易日跨度超 7 天、HK/海外标的未入库）返回 None，
        调用方留空不评——绝不用当前价冒充历史价（2026-08-06 review 修复）。
        """
        try:
            from foundf_db import Warehouse
            with Warehouse(self._db_path) as wh:
                rows = wh.query(
                    "SELECT date::VARCHAR AS d, close FROM daily_price "
                    "WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                    [symbol, eval_date.isoformat()],
                )
        except Exception:
            return None
        if not rows:
            return None
        price_date = date.fromisoformat(str(rows[0]["d"]))
        if (eval_date - price_date).days > 7:
            return None
        close = rows[0]["close"]
        return float(close) if close and close > 0 else None

    def evaluate(self, symbol: str = None) -> int:
        """Evaluate past decisions across all horizons (30d, 90d, 180d, 365d).
        
        For each horizon where enough time has passed, fills:
        - eval_{X}d_return: price return
        - eval_{X}d_outcome: UP | DOWN | SAME
        - eval_{X}d_correct: TRUE | FALSE
        
        Args:
            symbol: Filter by symbol (None = all)
            
        Returns: Number of horizon-evaluations performed
        """
        count = 0
        today = date.today()
        
        for entry in self._log:
            if symbol and entry["symbol"] != symbol:
                continue
            
            try:
                entry_date = datetime.strptime(entry["date"], "%Y-%m-%d").date()
            except (ValueError, IndexError):
                continue
            
            sym = entry["symbol"]
            price_at = float(entry.get("price_at_decision", 0) or 0)
            if price_at <= 0:
                continue
            
            action = entry["action"]
            
            for horizon_label, horizon_days in self.HORIZONS.items():
                field_return = f"eval_{horizon_label}_return"
                field_correct = f"eval_{horizon_label}_correct"
                
                # Skip if already evaluated
                if entry.get(field_correct) in ("TRUE", "FALSE"):
                    continue
                
                eval_date = entry_date + timedelta(days=horizon_days)
                if eval_date > today:
                    continue  # not yet time
                
                # 取 eval_date（或之前最近交易日）历史收盘价；取不到留空不评
                eval_price = self._historical_price(sym, eval_date)
                if eval_price is None or eval_price <= 0:
                    continue

                actual_return = (eval_price - price_at) / price_at
                entry[field_return] = str(round(actual_return, 4))
                
                # Outcome
                if actual_return > 0.02:
                    entry[f"eval_{horizon_label}_outcome"] = "UP"
                elif actual_return < -0.02:
                    entry[f"eval_{horizon_label}_outcome"] = "DOWN"
                else:
                    entry[f"eval_{horizon_label}_outcome"] = "SAME"
                
                # Correctness
                if action == "HOLD":
                    entry[field_correct] = str(actual_return >= -0.05).upper()
                elif action in ("BUY", "ADD"):
                    entry[field_correct] = str(actual_return > 0).upper()
                elif action in ("SELL", "REDUCE"):
                    entry[field_correct] = str(actual_return < 0).upper()
                else:
                    entry[field_correct] = "UNKNOWN"
                
                count += 1
        
        self._save()
        return count

    def accuracy(self) -> dict[str, Any]:
        """Compute accuracy metrics across all horizons."""
        # Collect all horizon evaluations
        horizon_stats: dict[str, dict] = {}
        total_any = 0
        total_correct_any = 0
        
        for horizon in ["30d", "90d", "180d", "365d"]:
            field = f"eval_{horizon}_correct"
            evaluated = [e for e in self._log if e.get(field) in ("TRUE", "FALSE")]
            if not evaluated:
                continue
            
            total = len(evaluated)
            correct = sum(1 for e in evaluated if e[field] == "TRUE")
            
            # By action within this horizon
            by_action: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
            for e in evaluated:
                action = e["action"]
                by_action[action]["total"] += 1
                if e[field] == "TRUE":
                    by_action[action]["correct"] += 1
            
            horizon_stats[horizon] = {
                "total": total,
                "correct": correct,
                "accuracy_pct": round(correct / total * 100, 1) if total > 0 else 0.0,
                "by_action": dict(by_action),
            }
            total_any += total
            total_correct_any += correct
        
        # Confidence-based accuracy (across all horizons combined)
        by_confidence: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
        for e in self._log:
            try:
                conf = float(e.get("confidence", 0))
            except (ValueError, TypeError):
                conf = 0
            bucket = "high" if conf >= 0.7 else "medium" if conf >= 0.4 else "low"
            # Check any horizon
            for horizon in ["30d", "90d", "180d", "365d"]:
                val = e.get(f"eval_{horizon}_correct", "")
                if val in ("TRUE", "FALSE"):
                    by_confidence[bucket]["total"] += 1
                    if val == "TRUE":
                        by_confidence[bucket]["correct"] += 1
        
        report = {
            "total_evaluations": total_any,
            "total_correct": total_correct_any,
            "overall_accuracy_pct": round(total_correct_any / max(total_any, 1) * 100, 1),
            "by_horizon": horizon_stats,
            "by_confidence": dict(by_confidence),
            "recent_decisions": [
                {"date": e["date"], "symbol": e["symbol"], "action": e["action"],
                 "confidence": e.get("confidence"),
                 "eval_30d": e.get("eval_30d_correct", "PENDING"),
                 "eval_90d": e.get("eval_90d_correct", "PENDING")}
                for e in self._log[-10:]
            ],
        }
        
        ACCURACY_FILE.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        
        return report

    def source_comparison(self) -> dict[str, Any]:
        """Phase N3: 比较不同决策来源（USER / AI_AGENT / FACTOR_MODEL / COMBINED）
        在 30/90/180/365 天维度上的准确率与收益表现。

        目的：回答"哪种决策方式长期最好"，据此分配未来决策权重。
        注意：worst_return 是单笔最差评估收益，作为回撤代理指标；
        严格最大回撤需要价格路径，当前数据粒度不足以计算。
        """
        by_source: dict[str, Any] = {}

        for src in DECISION_SOURCES:
            entries = [e for e in self._log if e.get("decision_source") == src]
            if not entries:
                continue

            horizon_stats: dict[str, Any] = {}
            for horizon in ["30d", "90d", "180d", "365d"]:
                cfield = f"eval_{horizon}_correct"
                rfield = f"eval_{horizon}_return"
                evaluated = [e for e in entries if e.get(cfield) in ("TRUE", "FALSE")]
                if not evaluated:
                    continue
                correct = sum(1 for e in evaluated if e[cfield] == "TRUE")
                returns = [float(e[rfield]) for e in evaluated
                           if e.get(rfield) not in ("", None)]
                horizon_stats[horizon] = {
                    "total": len(evaluated),
                    "correct": correct,
                    "accuracy_pct": round(correct / len(evaluated) * 100, 1),
                    "avg_return_pct": round(
                        sum(returns) / len(returns) * 100, 2) if returns else None,
                    "worst_return_pct": round(min(returns) * 100, 2) if returns else None,
                }

            by_source[src] = {
                "total_decisions": len(entries),
                "by_horizon": horizon_stats,
            }

        # 综合排名：优先看最长可用 horizon 的准确率，其次平均收益
        ranking = []
        for src, stats in by_source.items():
            hz = stats["by_horizon"]
            best_horizon = next(
                (h for h in ["365d", "180d", "90d", "30d"] if h in hz), None)
            if best_horizon is None:
                ranking.append({"source": src, "status": "PENDING",
                                "decisions": stats["total_decisions"]})
                continue
            h = hz[best_horizon]
            ranking.append({
                "source": src,
                "status": "EVALUATED",
                "horizon": best_horizon,
                "accuracy_pct": h["accuracy_pct"],
                "avg_return_pct": h["avg_return_pct"],
                "evaluations": h["total"],
            })
        ranking.sort(
            key=lambda r: (r.get("accuracy_pct", -1), r.get("avg_return_pct") or -999),
            reverse=True)

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "by_source": by_source,
            "ranking": ranking,
            "conclusion": (
                f"当前表现最好的决策来源: {ranking[0]['source']} "
                f"({ranking[0]['horizon']} 准确率 {ranking[0]['accuracy_pct']}%)"
                if ranking and ranking[0]["status"] == "EVALUATED"
                else "评估数据不足 — 各来源决策均需经过 30 天以上跟踪期"
            ),
        }

        SOURCE_COMPARISON_FILE.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def summary(self) -> dict[str, Any]:
        return {
            "total_decisions": len(self._log),
            "evaluated": sum(1 for e in self._log if e.get("was_correct") in ("TRUE", "FALSE")),
            "pending_evaluation": sum(1 for e in self._log if not e.get("was_correct")),
            "accuracy": self.accuracy(),
        }


# ── CLI ────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    memory = DecisionMemory()
    
    if len(sys.argv) > 1 and sys.argv[1] == "record":
        # python -m portfolio_manager.decision_memory record 2026-07-20 00700 HOLD "估值合理" 0.7 AI_AGENT
        _, _, d, sym, action, reason, *rest = sys.argv
        conf = float(rest[0]) if rest else 0.5
        src = rest[1] if len(rest) > 1 else "AI_AGENT"
        idx = memory.record(d, sym, "", action, reason, conf, decision_source=src)
        print(f"Recorded decision #{idx}: {d} {sym} {action} (conf={conf}, source={src})")

    elif len(sys.argv) > 1 and sys.argv[1] == "compare":
        # Phase N3: 决策来源效果比较
        r = memory.source_comparison()
        print("=== Phase N3: Decision Source Comparison ===\n")
        for rank in r["ranking"]:
            if rank["status"] == "EVALUATED":
                print(f"  {rank['source']:15s} {rank['horizon']:>5s} "
                      f"准确率 {rank['accuracy_pct']:5.1f}%  "
                      f"平均收益 {rank['avg_return_pct']:+6.2f}%  "
                      f"({rank['evaluations']} 次评估)")
            else:
                print(f"  {rank['source']:15s} PENDING ({rank['decisions']} 条决策未到评估期)")
        print(f"\n{r['conclusion']}")
        print(f"\n已保存: {SOURCE_COMPARISON_FILE}")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "evaluate":
        n = memory.evaluate(days_later=30)
        print(f"Evaluated {n} decisions")
    
    else:
        print("=== Phase N: Decision Memory ===\n")
        s = memory.summary()
        print(f"Total decisions: {s['total_decisions']}")
        print(f"Evaluated: {s['evaluated']}")
        print(f"Pending: {s['pending_evaluation']}")
        
        if s["total_decisions"] > 0:
            print(f"\nPending decisions:")
            for e in memory._log:
                if not e.get("was_correct"):
                    print(f"  {e['date']} {e['symbol']} {e['action']} (conf={e.get('confidence')}) — {e.get('reason', '')[:40]}")
        
        acc = s["accuracy"]
        if acc.get("total_decisions", 0) > 0:
            print(f"\nAccuracy: {acc['accuracy_pct']}% ({acc['correct']}/{acc['total_decisions']})")
        
        if not memory._log:
            print("\nNo decisions recorded yet.")
            print("\nUsage:")
            print("  Record: python -m portfolio_manager.decision_memory record <date> <symbol> <action> <reason> [confidence]")
            print("  Evaluate: python -m portfolio_manager.decision_memory evaluate")
