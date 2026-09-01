"""
source_registry — Phase O: Data Source Reliability System.

所有市场/基本面数据源都有可信评分，没有任何源等于真理。

与 source_quality_engine 的分工：
    source_quality_engine — 信息/新闻源的预测准确率（"说了什么"）
    source_registry       — 数据供应链的可靠性（"数据本身可不可用"）

每个数据源记录：
    source_name, data_types, update_frequency,
    error_rate (EMA), historical_accuracy (数据完整度 EMA),
    weight, status (active → reduced → deprecated)

每日更新 source_score；低于阈值自动降低其在采集与策略中的影响权重。

存储: data/source_registry/registry.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REGISTRY_DIR = Path("data/source_registry")
REGISTRY_FILE = REGISTRY_DIR / "registry.json"
PREDICTIONS_FILE = REGISTRY_DIR / "predictions.json"

# Alpha 评分：信息源的投资影响力（区别于数据可靠性 score）
ALPHA_EMA = 0.2                # alpha_score 的 EMA 系数
ALPHA_MIN_RESOLVED = 5         # 至少 N 条已验证预测才把 alpha 计入权重
PREDICT_HORIZON_DAYS = 30      # 预测验证期（天）

# 评分权重：错误率 / 完整度 / 新鲜度
W_ERROR = 0.40
W_ACCURACY = 0.40
W_FRESHNESS = 0.20

# 状态阈值
SCORE_REDUCED = 60.0     # 低于此分数 → 降权
SCORE_DEPRECATED = 40.0  # 低于此分数 → 弃用
WEIGHT_CUT = 0.5         # 降权时的权重乘数

# EMA 平滑系数（新观测占比）
EMA_ALPHA = 0.2

# 内置数据源（以项目实际使用的为准）
DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
    "tushare": {
        "data_types": ["cn_daily", "cn_basic", "cn_financial", "index"],
        "update_frequency": "daily",
        "initial_weight": 0.9,
    },
    "baostock": {
        "data_types": ["cn_daily", "cn_basic", "cn_minute"],
        "update_frequency": "daily",
        "initial_weight": 0.8,
    },
    "akshare": {
        "data_types": ["cn_daily", "hk_daily", "etf", "news"],
        "update_frequency": "daily",
        "initial_weight": 0.7,
    },
    "yfinance": {
        "data_types": ["us_daily", "hk_daily", "us_etf", "index"],
        "update_frequency": "daily",
        "initial_weight": 0.7,
    },
    "hkex": {
        "data_types": ["hk_announcement", "hk_basic"],
        "update_frequency": "daily",
        "initial_weight": 0.95,
    },
    "sec": {
        "data_types": ["us_filing", "us_financial"],
        "update_frequency": "daily",
        "initial_weight": 0.95,
    },
    "fred": {
        "data_types": ["macro"],
        "update_frequency": "daily",
        "initial_weight": 0.95,
    },
    "bloomberg": {
        "data_types": ["licensed_price", "reference", "fundamental", "news"],
        "update_frequency": "entitlement_dependent",
        "initial_weight": 0.95,
    },
    "investing_authorized_import": {
        "data_types": ["authorized_secondary_export"],
        "update_frequency": "manual",
        "initial_weight": 0.60,
    },
    "brave_search": {
        "data_types": ["web_discovery", "news_discovery"],
        "update_frequency": "on_demand",
        "initial_weight": 0.40,
    },
    "google_custom_search": {
        "data_types": ["legacy_web_discovery"],
        "update_frequency": "on_demand",
        "initial_weight": 0.30,
    },
}


class SourceRegistry:
    """数据源可靠性注册表。

    使用方式:
        registry = SourceRegistry()
        registry.record_fetch("tushare", success=True, records=5000, expected=5200)
        registry.update_scores()
        w = registry.get_weight("tushare")   # 策略/采集据此加权
    """

    def __init__(self, registry_dir: str | Path = REGISTRY_DIR):
        self.dir = Path(registry_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "registry.json"
        self._sources: dict[str, dict[str, Any]] = {}
        self._load()

    # ── 持久化 ───────────────────────────────────────

    def _load(self) -> None:
        if self.file.exists():
            self._sources = json.loads(self.file.read_text(encoding="utf-8"))
        else:
            # 首次初始化
            for name, info in DEFAULT_SOURCES.items():
                self._sources[name] = {
                    "source_name": name,
                    "data_types": info["data_types"],
                    "update_frequency": info["update_frequency"],
                    "weight": info["initial_weight"],
                    "initial_weight": info["initial_weight"],
                    "status": "active",
                    "total_fetches": 0,
                    "failed_fetches": 0,
                    "error_rate": 0.0,              # EMA
                    "historical_accuracy": 1.0,     # EMA，数据完整度
                    "avg_latency_s": 0.0,           # EMA
                    "freshness_days": 0.0,          # 距上次成功采集天数
                    "last_success": None,
                    "last_fetch": None,
                    "score": 100.0 * info["initial_weight"],
                    "score_history": [],
                }
            self._save()

    def _save(self) -> None:
        self.file.write_text(
            json.dumps(self._sources, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ── 观测记录 ─────────────────────────────────────

    def record_fetch(self, source: str, success: bool,
                     records: int = 0, expected: int = 0,
                     latency_s: float = 0.0) -> None:
        """记录一次采集结果，EMA 更新 error_rate / accuracy / latency。

        Args:
            success: 采集是否成功（未抛异常、返回非空）
            records: 实际获得记录数
            expected: 预期记录数（0 = 不评估完整度）
            latency_s: 采集耗时
        """
        if source not in self._sources:
            # 未注册源自动注册（低初始权重，靠表现挣分）
            self._sources[source] = {
                "source_name": source,
                "data_types": [],
                "update_frequency": "unknown",
                "weight": 0.3,
                "initial_weight": 0.3,
                "status": "active",
                "total_fetches": 0,
                "failed_fetches": 0,
                "error_rate": 0.0,
                "historical_accuracy": 1.0,
                "avg_latency_s": 0.0,
                "freshness_days": 0.0,
                "last_success": None,
                "last_fetch": None,
                "score": 30.0,
                "score_history": [],
            }

        s = self._sources[source]
        now = datetime.now(timezone.utc)

        s["total_fetches"] += 1
        if not success:
            s["failed_fetches"] += 1

        # EMA 更新错误率
        s["error_rate"] = round(
            (1 - EMA_ALPHA) * s["error_rate"] + EMA_ALPHA * (0.0 if success else 1.0), 4)

        # EMA 更新完整度（仅成功且给了预期值时）
        if success and expected > 0:
            completeness = min(records / expected, 1.0)
            s["historical_accuracy"] = round(
                (1 - EMA_ALPHA) * s["historical_accuracy"] + EMA_ALPHA * completeness, 4)
        elif not success:
            s["historical_accuracy"] = round(
                (1 - EMA_ALPHA) * s["historical_accuracy"], 4)

        # EMA 更新延迟
        if success:
            s["avg_latency_s"] = round(
                (1 - EMA_ALPHA) * s["avg_latency_s"] + EMA_ALPHA * latency_s, 2)
            s["last_success"] = now.isoformat()
        s["last_fetch"] = now.isoformat()

        self._save()

    # ── 评分与权重 ───────────────────────────────────

    def _freshness_score(self, s: dict[str, Any]) -> float:
        """新鲜度 0-1：距上次成功采集越久分越低。"""
        if not s.get("last_success"):
            return 0.5  # 从未采集：中性分，给新源机会
        last = datetime.fromisoformat(s["last_success"])
        days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        s["freshness_days"] = round(days, 1)
        if days <= 1:
            return 1.0
        if days <= 3:
            return 0.8
        if days <= 7:
            return 0.5
        return 0.2

    def update_scores(self) -> dict[str, float]:
        """重算所有源的 score，并按阈值调整 status / weight。"""
        scores = {}
        for name, s in self._sources.items():
            score = 100.0 * (
                W_ERROR * (1 - s["error_rate"])
                + W_ACCURACY * s["historical_accuracy"]
                + W_FRESHNESS * self._freshness_score(s)
            )
            score = round(score, 1)
            s["score"] = score
            scores[name] = score

            # 状态机 + 权重调整
            if score < SCORE_DEPRECATED:
                if s["status"] != "deprecated":
                    s["status"] = "deprecated"
                s["weight"] = 0.0
            elif score < SCORE_REDUCED:
                if s["status"] == "active":
                    s["status"] = "reduced"
                    s["weight"] = round(s["initial_weight"] * WEIGHT_CUT, 3)
            else:
                # 恢复：分数回到阈值之上
                if s["status"] in ("reduced", "deprecated"):
                    s["status"] = "active"
                    s["weight"] = s["initial_weight"]

            # 保留最近 90 条评分历史
            s["score_history"].append({
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "score": score,
            })
            s["score_history"] = s["score_history"][-90:]

        self._save()
        return scores

    def get_weight(self, source: str) -> float:
        """策略/采集层使用的源权重。deprecated 源恒为 0。

        若该源有 ≥ALPHA_MIN_RESOLVED 条已验证预测，权重按 alpha 调整：
        effective = weight * (0.5 + alpha_score)，alpha_score ∈ [0,1]。
        """
        s = self._sources.get(source)
        if s is None or s["status"] == "deprecated":
            return 0.0
        w = s["weight"]
        if s.get("alpha_resolved", 0) >= ALPHA_MIN_RESOLVED:
            w = round(w * (0.5 + s.get("alpha_score", 0.5)), 3)
        return w

    # ── Alpha：信息源投资影响力 ──────────────────────

    def _ensure_source(self, source: str) -> dict[str, Any]:
        """信息源（新闻/研报/自媒体）可能不在数据注册表中，自动登记。"""
        if source not in self._sources:
            self._sources[source] = {
                "source_name": source,
                "data_types": ["info"],
                "update_frequency": "ad_hoc",
                "weight": 0.5,
                "initial_weight": 0.5,
                "status": "active",
                "total_fetches": 0,
                "failed_fetches": 0,
                "error_rate": 0.0,
                "historical_accuracy": 1.0,
                "avg_latency_s": 0.0,
                "freshness_days": 0.0,
                "last_success": None,
                "last_fetch": None,
                "score": 50.0,
                "score_history": [],
            }
        return self._sources[source]

    def _load_predictions(self) -> list[dict[str, Any]]:
        if PREDICTIONS_FILE.exists():
            return json.loads(PREDICTIONS_FILE.read_text(encoding="utf-8"))
        return []

    def _save_predictions(self, preds: list[dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        PREDICTIONS_FILE.write_text(
            json.dumps(preds, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_prediction(self, source: str, symbol: str, direction: str,
                          event: str = "", predicted_at: str | None = None) -> None:
        """记录一条信息源预测。

        Args:
            direction: "up" | "down"（该源暗示的价格方向）
            event: 事件描述，如 "某公司利好"
        """
        if direction not in ("up", "down"):
            raise ValueError("direction 必须是 up 或 down")
        preds = self._load_predictions()
        preds.append({
            "source": source,
            "symbol": symbol,
            "direction": direction,
            "event": event,
            "predicted_at": predicted_at
            or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "resolved": False,
            "forward_return": None,
            "correct": None,
        })
        self._save_predictions(preds)

    @staticmethod
    def _resolve_symbol(symbol: str) -> list[str]:
        """候选的 daily_price 代码格式。"""
        candidates = [symbol]
        if symbol.startswith("0") and len(symbol) == 5 and symbol.isdigit():
            candidates.append(f"{symbol[1:]}.HK")
        return candidates

    def resolve_predictions(self, duckdb_path: str | Path = "data/finance.duckdb",
                            horizon_days: int = PREDICT_HORIZON_DAYS) -> int:
        """验证到期预测：用 daily_price 的实际远期收益判定对错，EMA 更新 alpha。"""
        from foundf_db import Warehouse

        preds = self._load_predictions()
        due = [p for p in preds if not p["resolved"]]
        if not due:
            return 0

        wh = Warehouse(duckdb_path)
        wh.init()
        today = datetime.now(timezone.utc).date()
        resolved = 0

        for p in due:
            pred_date = datetime.strptime(p["predicted_at"], "%Y-%m-%d").date()
            if (today - pred_date).days < horizon_days:
                continue
            target = pred_date.fromordinal(pred_date.toordinal() + horizon_days)

            rows = []
            for cand in self._resolve_symbol(p["symbol"]):
                rows = wh.query(
                    "SELECT date, close FROM daily_price WHERE symbol = ? "
                    "AND date >= ? ORDER BY date LIMIT 1", [cand, str(pred_date)])
                if rows:
                    rows_after = wh.query(
                        "SELECT date, close FROM daily_price WHERE symbol = ? "
                        "AND date >= ? ORDER BY date LIMIT 1", [cand, str(target)])
                    if rows_after:
                        rows = (rows, rows_after)
                        break
            if not rows or not isinstance(rows, tuple):
                continue

            entry, exit_ = rows[0][0], rows[1][0]
            if entry["close"] <= 0:
                continue
            fwd = exit_["close"] / entry["close"] - 1
            correct = (fwd > 0) if p["direction"] == "up" else (fwd < 0)

            p["resolved"] = True
            p["forward_return"] = round(fwd, 4)
            p["correct"] = correct
            resolved += 1

            s = self._ensure_source(p["source"])
            alpha = s.get("alpha_score", 0.5)
            s["alpha_score"] = round(
                (1 - ALPHA_EMA) * alpha + ALPHA_EMA * (1.0 if correct else 0.0), 4)
            s["alpha_resolved"] = s.get("alpha_resolved", 0) + 1

        self._save_predictions(preds)
        self._save()
        return resolved

    def get_alpha(self, source: str) -> dict[str, Any]:
        """某源的 alpha 统计。"""
        s = self._sources.get(source, {})
        preds = [p for p in self._load_predictions() if p["source"] == source]
        resolved = [p for p in preds if p["resolved"]]
        correct = sum(1 for p in resolved if p["correct"])
        return {
            "source": source,
            "alpha_score": s.get("alpha_score", 0.5),
            "total_predictions": len(preds),
            "resolved": len(resolved),
            "accuracy": round(correct / len(resolved), 3) if resolved else None,
            "counts_toward_weight": len(resolved) >= ALPHA_MIN_RESOLVED,
        }

    def report(self) -> dict[str, Any]:
        """完整可靠性报告。"""
        self.update_scores()
        rows = sorted(self._sources.values(), key=lambda s: -s["score"])
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": [
                {
                    "source_name": s["source_name"],
                    "score": s["score"],
                    "status": s["status"],
                    "weight": s["weight"],
                    "error_rate": s["error_rate"],
                    "historical_accuracy": s["historical_accuracy"],
                    "freshness_days": s.get("freshness_days"),
                    "total_fetches": s["total_fetches"],
                    "data_types": s["data_types"],
                }
                for s in rows
            ],
        }


# ── CLI ────────────────────────────────────────────

def main() -> None:
    import sys

    reg = SourceRegistry()

    if len(sys.argv) > 1 and sys.argv[1] == "record":
        # python -m source_registry record tushare ok 5000 5200 3.2
        source = sys.argv[2]
        success = sys.argv[3] == "ok"
        records = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        expected = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        latency = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
        reg.record_fetch(source, success, records, expected, latency)
        reg.update_scores()
        s = reg._sources[source]
        print(f"{source}: score={s['score']} status={s['status']} weight={s['weight']}")

    else:
        r = reg.report()
        print("=== Phase O: Data Source Reliability ===\n")
        print(f"{'Source':12s} {'Score':>6s} {'Status':10s} {'Weight':>6s} "
              f"{'ErrRate':>8s} {'Accuracy':>8s} {'Fresh(d)':>8s} {'Fetches':>7s}")
        print("-" * 75)
        for s in r["sources"]:
            fresh = f"{s['freshness_days']:.1f}" if s["freshness_days"] is not None else "-"
            print(f"{s['source_name']:12s} {s['score']:6.1f} {s['status']:10s} "
                  f"{s['weight']:6.2f} {s['error_rate']:8.2f} "
                  f"{s['historical_accuracy']:8.2f} {fresh:>8s} {s['total_fetches']:7d}")
        deprecated = [s for s in r["sources"] if s["status"] == "deprecated"]
        reduced = [s for s in r["sources"] if s["status"] == "reduced"]
        if reduced or deprecated:
            print()
            for s in reduced:
                print(f"⚠️  {s['source_name']} 已降权 (weight={s['weight']})")
            for s in deprecated:
                print(f"⛔ {s['source_name']} 已弃用 (weight=0)")


if __name__ == "__main__":
    main()
