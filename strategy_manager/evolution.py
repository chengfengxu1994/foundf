"""Evidence-first factor and strategy evolution governance.

The governor never optimizes a strategy itself and never approves production
deployment.  It validates evidence, preserves market-factor anchors, applies
bounded factor weakening, and decides the highest stage a candidate may enter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from foundf_db.strategy_freeze_store import StrategyFreezeStore
from foundf_db.walk_forward_input_store import inspect_walk_forward_inputs

from .evidence_adapter import (
    EvidenceContractError,
    adapt_walk_forward_evidence,
)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class EvolutionPolicy:
    policy_id: str = "evidence_first_strategy_evolution"
    policy_version: str = "1"
    min_universe_size: int = 100
    min_sample_years: float = 10.0
    min_factor_periods: int = 120
    max_research_age_days: int = 35
    max_factor_data_lag_days: int = 7
    min_independent_factor_runs: int = 3
    min_walk_forward_windows: int = 6
    min_out_of_sample_years: float = 3.0
    min_positive_window_ratio: float = 0.55
    min_avg_sharpe: float = 0.30
    max_drawdown: float = 0.25
    max_turnover: float = 1.0
    require_positive_excess_return: bool = True
    min_paper_days: int = 90
    anchor_factors: tuple[str, ...] = (
        "momentum_3m",
        "momentum_6m",
        "momentum_12m",
        "low_volatility",
    )
    anchor_categories: tuple[tuple[str, float], ...] = (
        ("momentum", 0.10),
        ("risk", 0.10),
    )
    anchor_multiplier_floor: float = 0.50
    proxy_multiplier_cap: float = 0.25
    max_multiplier_step: float = 0.25

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EvolutionPolicy":
        policy = cls(
            policy_id=str(raw.get("policy_id", cls.policy_id)),
            policy_version=str(raw.get("policy_version", cls.policy_version)),
            min_universe_size=int(raw.get("min_universe_size", 100)),
            min_sample_years=float(raw.get("min_sample_years", 10)),
            min_factor_periods=int(raw.get("min_factor_periods", 120)),
            max_research_age_days=int(raw.get("max_research_age_days", 35)),
            max_factor_data_lag_days=int(
                raw.get("max_factor_data_lag_days", 7)
            ),
            min_independent_factor_runs=int(
                raw.get("min_independent_factor_runs", 3)
            ),
            min_walk_forward_windows=int(
                raw.get("min_walk_forward_windows", 6)
            ),
            min_out_of_sample_years=float(
                raw.get("min_out_of_sample_years", 3)
            ),
            min_positive_window_ratio=float(
                raw.get("min_positive_window_ratio", 0.55)
            ),
            min_avg_sharpe=float(raw.get("min_avg_sharpe", 0.30)),
            max_drawdown=float(raw.get("max_drawdown", 0.25)),
            max_turnover=float(raw.get("max_turnover", 1.0)),
            require_positive_excess_return=bool(
                raw.get("require_positive_excess_return", True)
            ),
            min_paper_days=int(raw.get("min_paper_days", 90)),
            anchor_factors=tuple(raw.get("anchor_factors", cls.anchor_factors)),
            anchor_categories=tuple(
                (str(key), float(value))
                for key, value in raw.get(
                    "anchor_categories", dict(cls.anchor_categories)
                ).items()
            ),
            anchor_multiplier_floor=float(
                raw.get("anchor_multiplier_floor", 0.50)
            ),
            proxy_multiplier_cap=float(
                raw.get("proxy_multiplier_cap", 0.25)
            ),
            max_multiplier_step=float(raw.get("max_multiplier_step", 0.25)),
        )
        if policy.min_universe_size < 30 or policy.min_sample_years < 3:
            raise ValueError("research sample boundary is too weak")
        if policy.min_independent_factor_runs < 2:
            raise ValueError("factor lifecycle requires repeated evidence")
        if not 0 <= policy.anchor_multiplier_floor <= 1:
            raise ValueError("invalid anchor multiplier floor")
        if not 0 <= policy.proxy_multiplier_cap <= 1:
            raise ValueError("invalid proxy multiplier cap")
        if not 0 < policy.max_multiplier_step <= 0.5:
            raise ValueError("invalid factor multiplier step")
        return policy

    @classmethod
    def load(
        cls, path: str | Path = "config/strategy_evolution.json"
    ) -> "EvolutionPolicy":
        return cls.from_mapping(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )

    @property
    def category_floors(self) -> dict[str, float]:
        return dict(self.anchor_categories)


class StrategyEvolutionGovernor:
    """Evaluate data, factor, backtest and paper-trading evidence."""

    def __init__(self, policy: EvolutionPolicy):
        self.policy = policy

    def data_gate(
        self,
        factor_research: Mapping[str, Any] | None,
        as_of: str | date | None = None,
    ) -> dict[str, Any]:
        today = (
            as_of
            if isinstance(as_of, date)
            else _parse_date(as_of)
            or date.today()
        )
        failures: list[str] = []
        research = factor_research or {}
        if not research:
            return {"passed": False, "failures": ["FACTOR_RESEARCH_MISSING"]}
        generated = _parse_date(research.get("generated_at"))
        if generated is None:
            failures.append("RESEARCH_TIMESTAMP_MISSING")
        elif (today - generated).days > self.policy.max_research_age_days:
            failures.append("RESEARCH_REPORT_STALE")
        universe = int(research.get("universe_size", 0) or 0)
        if universe < self.policy.min_universe_size:
            failures.append("UNIVERSE_TOO_SMALL")
        start, end = None, None
        date_range = str(research.get("date_range", ""))
        if " ~ " in date_range:
            start, end = (_parse_date(item) for item in date_range.split(" ~ ", 1))
        sample_years = (
            (end - start).days / 365.25 if start is not None and end is not None else 0
        )
        if sample_years < self.policy.min_sample_years:
            failures.append("SAMPLE_HISTORY_TOO_SHORT")
        if end is None:
            failures.append("RESEARCH_DATA_END_MISSING")
            freshness_ref = None
        else:
            # 新鲜度以研究可见的底层数据截止日（data_as_of，2026-08 起由
            # research_engine 记录为全 symbol 最大行情日）为准；date_range
            # 末端只是 IC 采样网格点（周频采样恒为最近周一），直接用它
            # 会把「网格间距」误报成「数据陈旧」（周一至周六结构性必红）。
            freshness_ref = _parse_date(research.get("data_as_of")) or end
        if freshness_ref is not None and (
            (today - freshness_ref).days > self.policy.max_factor_data_lag_days
        ):
            failures.append("RESEARCH_DATA_STALE")
        factor_periods = [
            int(item.get("ic", {}).get("periods", 0) or 0)
            for item in research.get("factors", {}).values()
        ]
        if not factor_periods or min(factor_periods) < self.policy.min_factor_periods:
            failures.append("FACTOR_PERIODS_INSUFFICIENT")
        return {
            "passed": not failures,
            "failures": failures,
            "universe_size": universe,
            "sample_years": round(sample_years, 2),
            "research_data_end": end.isoformat() if end else None,
            "data_as_of": (
                freshness_ref.isoformat() if freshness_ref is not None else None
            ),
            "research_age_days": (
                (today - generated).days if generated is not None else None
            ),
            "data_lag_days": (
                (today - freshness_ref).days
                if freshness_ref is not None else None
            ),
            "minimum_factor_periods": min(factor_periods) if factor_periods else 0,
        }

    def backtest_gate(
        self, walk_forward: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        failures: list[str] = []
        report = walk_forward or {}
        windows = report.get("windows", [])
        summary = report.get("summary", {})
        if len(windows) < self.policy.min_walk_forward_windows:
            failures.append("WALK_FORWARD_WINDOWS_INSUFFICIENT")
        test_dates = []
        for row in windows:
            raw = row.get("test") or row.get("test_end")
            if raw and "~" in str(raw):
                raw = str(raw).split("~")[-1]
            parsed = _parse_date(raw)
            if parsed:
                test_dates.append(parsed)
        oos_years = (
            (max(test_dates) - min(test_dates)).days / 365.25
            if len(test_dates) >= 2
            else 0.0
        )
        if oos_years < self.policy.min_out_of_sample_years:
            failures.append("OUT_OF_SAMPLE_HISTORY_TOO_SHORT")
        positive_ratio = _number(
            summary.get("positive_window_ratio", summary.get("win_rate"))
        )
        if (
            positive_ratio is None
            or positive_ratio < self.policy.min_positive_window_ratio
        ):
            failures.append("POSITIVE_WINDOWS_TOO_LOW")
        sharpe = _number(summary.get("avg_sharpe"))
        if sharpe is None or sharpe < self.policy.min_avg_sharpe:
            failures.append("SHARPE_TOO_LOW")
        drawdown = _number(summary.get("max_drawdown"))
        if drawdown is None or abs(drawdown) > self.policy.max_drawdown:
            failures.append("MAX_DRAWDOWN_BREACH")
        turnover = _number(summary.get("turnover"))
        if turnover is None or turnover > self.policy.max_turnover:
            failures.append("TURNOVER_BREACH_OR_MISSING")
        excess = _number(summary.get("avg_excess_return"))
        if self.policy.require_positive_excess_return and (
            excess is None or excess <= 0
        ):
            failures.append("EXCESS_RETURN_NOT_POSITIVE")
        return {
            "passed": not failures,
            "failures": failures,
            "windows": len(windows),
            "out_of_sample_years": round(oos_years, 2),
            "positive_window_ratio": positive_ratio,
            "avg_sharpe": sharpe,
            "max_drawdown": drawdown,
            "turnover": turnover,
            "avg_excess_return": excess,
        }

    def candidate_factor_gate(
        self,
        candidate_config: Mapping[str, Any] | None,
        factor_research: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        failures: list[str] = []
        if candidate_config is None:
            proxies = [
                name
                for name, item in (factor_research or {}).get("factors", {}).items()
                if item.get("is_price_proxy")
            ]
            return {
                "passed": False,
                "failures": ["CANDIDATE_CONFIG_MISSING"],
                "anchor_category_floors": self.policy.category_floors,
                "proxy_factors": proxies,
            }
        config = candidate_config or {}
        weights = config.get("factor_weights", {})
        for category, floor in self.policy.category_floors.items():
            if float(weights.get(category, 0) or 0) < floor:
                failures.append(f"ANCHOR_CATEGORY_FLOOR_{category.upper()}")
        proxies = [
            name
            for name, item in (factor_research or {}).get("factors", {}).items()
            if item.get("is_price_proxy")
        ]
        if proxies and any(
            float(weights.get(category, 0) or 0) > 0
            for category in ("value", "quality", "growth")
        ):
            failures.append("FUNDAMENTAL_FACTORS_STILL_PRICE_PROXIES")
        total_weight = sum(float(value or 0) for value in weights.values())
        if abs(total_weight - 1) > 1e-6:
            failures.append("FACTOR_WEIGHTS_MUST_SUM_TO_ONE")
        return {
            "passed": not failures,
            "failures": failures,
            "anchor_category_floors": self.policy.category_floors,
            "proxy_factors": proxies,
        }

    def evaluate(
        self,
        *,
        factor_research: Mapping[str, Any] | None,
        walk_forward: Mapping[str, Any] | None,
        candidate_config: Mapping[str, Any] | None = None,
        paper_metrics: Mapping[str, Any] | None = None,
        as_of: str | date | None = None,
    ) -> dict[str, Any]:
        data = self.data_gate(factor_research, as_of=as_of)
        factors = self.candidate_factor_gate(candidate_config, factor_research)
        backtest = self.backtest_gate(walk_forward)
        paper = paper_metrics or {}
        paper_days = int(paper.get("days", 0) or 0)
        paper_ready = paper_days >= self.policy.min_paper_days

        if not data["passed"]:
            stage = "BLOCKED_DATA"
        elif not factors["passed"]:
            stage = "BLOCKED_FACTORS"
        elif not backtest["passed"]:
            stage = "BLOCKED_BACKTEST"
        elif not paper_ready:
            stage = "ELIGIBLE_FOR_PAPER"
        else:
            stage = "PENDING_HUMAN_REVIEW"
        return {
            "schema_version": "foundf.strategy_evolution.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "policy_id": self.policy.policy_id,
            "policy_version": self.policy.policy_version,
            "requirements": {
                "minimum_universe_size": self.policy.min_universe_size,
                "minimum_sample_years": self.policy.min_sample_years,
                "minimum_factor_periods": self.policy.min_factor_periods,
                "maximum_factor_data_lag_days": (
                    self.policy.max_factor_data_lag_days
                ),
                "minimum_walk_forward_windows": (
                    self.policy.min_walk_forward_windows
                ),
                "minimum_out_of_sample_years": (
                    self.policy.min_out_of_sample_years
                ),
                "minimum_paper_days": self.policy.min_paper_days,
            },
            "stage": stage,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
            "data_gate": data,
            "factor_gate": factors,
            "backtest_gate": backtest,
            "paper_gate": {
                "passed": paper_ready,
                "days": paper_days,
                "minimum_days": self.policy.min_paper_days,
            },
            "human_approval_required": True,
            "disclaimer": (
                "模型改进只能逐级晋升；任何单次高收益、样本内最优或AI建议"
                "都不能直接修改生产策略。"
            ),
        }


def _research_data_id(factor_research: Mapping[str, Any]) -> str:
    """研究证据的底层数据指纹：数据截止日 + 样本规模。

    用于 FactorLifecycle 的"独立运行"判定：同一批数据重复生成的报告
    （generated_at 不同但数据未变）不算新的独立证据，防止靠反复重跑
    同一数据凑齐 keep_streak / cull_streak。无数据信息返回 ""，
    调用方按非独立处理（fail-closed）。
    """
    end = str(factor_research.get("data_as_of") or "")
    if not end:
        date_range = str(factor_research.get("date_range", ""))
        if " ~ " in date_range:
            end = date_range.split(" ~ ", 1)[1].strip()
    if not end:
        return ""
    return "|".join(
        [
            end,
            str(factor_research.get("sample_dates", "")),
            str(factor_research.get("universe_size", "")),
        ]
    )


class FactorLifecycle:
    """Persist bounded KEEP/WATCH/CULL evidence without abrupt deletion.

    连续 N 次"独立运行"的判定要求底层数据指纹（`_research_data_id`）
    不同——相同数据重复运行只算一次证据。
    """

    def __init__(
        self,
        policy: EvolutionPolicy,
        state_path: str | Path = "data/governance/factor_lifecycle.json",
    ):
        self.policy = policy
        self.state_path = Path(state_path)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": "foundf.factor_lifecycle.v1", "factors": {}}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"schema_version": "foundf.factor_lifecycle.v1", "factors": {}}
        return raw if isinstance(raw.get("factors"), dict) else {
            "schema_version": "foundf.factor_lifecycle.v1",
            "factors": {},
        }

    def update(
        self,
        factor_research: Mapping[str, Any],
        data_gate: Mapping[str, Any],
        write: bool = True,
    ) -> dict[str, Any]:
        state = self._load()
        evidence_id = str(factor_research.get("generated_at", ""))
        processed = set(state.get("processed_evidence_ids", []))
        is_new = bool(evidence_id and evidence_id not in processed)
        # 独立证据判定（2026-08-13 修复）：只凭 generated_at 会把"同一批
        # 数据重复跑"也算成独立运行；要求底层数据指纹（截止日+样本规模）
        # 不同才算新的独立证据，否则 streak 不推进
        data_id = _research_data_id(factor_research)
        processed_data = set(state.get("processed_data_ids", []))
        is_new_data = bool(data_id and data_id not in processed_data)
        for name, evidence in factor_research.get("factors", {}).items():
            anchor = name in self.policy.anchor_factors
            proxy = bool(evidence.get("is_price_proxy"))
            current = state["factors"].get(
                name,
                {
                    "status": "ANCHOR_ACTIVE" if anchor else "WATCH",
                    "multiplier": 1.0 if anchor else 0.5,
                    "observations": 0,
                    "keep_streak": 0,
                    "cull_streak": 0,
                    "history": [],
                },
            )
            if proxy:
                current["multiplier"] = min(
                    float(current["multiplier"]),
                    self.policy.proxy_multiplier_cap,
                )
                current["status"] = "PROXY_WATCH"
            elif not data_gate.get("passed"):
                current["status"] = (
                    "ANCHOR_FROZEN" if anchor else "FROZEN_DATA_UNREADY"
                )
            elif is_new and is_new_data:
                verdict = str(evidence.get("verdict", "WATCH")).upper()
                current["observations"] += 1
                if verdict == "KEEP":
                    current["keep_streak"] += 1
                    current["cull_streak"] = 0
                    if current["keep_streak"] >= self.policy.min_independent_factor_runs:
                        current["status"] = "ANCHOR_ACTIVE" if anchor else "ACTIVE"
                        current["multiplier"] = min(
                            1.0,
                            float(current["multiplier"])
                            + self.policy.max_multiplier_step,
                        )
                    else:
                        current["status"] = "KEEP_PENDING_REPEAT"
                elif verdict == "CULL":
                    current["cull_streak"] += 1
                    current["keep_streak"] = 0
                    floor = (
                        self.policy.anchor_multiplier_floor if anchor else 0.0
                    )
                    current["multiplier"] = max(
                        floor,
                        float(current["multiplier"])
                        - self.policy.max_multiplier_step,
                    )
                    if current["cull_streak"] >= self.policy.min_independent_factor_runs:
                        current["status"] = (
                            "ANCHOR_FLOOR" if anchor else "QUARANTINED"
                        )
                    else:
                        current["status"] = "REDUCED"
                else:
                    current["keep_streak"] = 0
                    current["cull_streak"] = 0
                    current["status"] = "ANCHOR_WATCH" if anchor else "WATCH"
            current["anchor"] = anchor
            current["price_proxy"] = proxy
            current["latest_verdict"] = evidence.get("verdict")
            current["latest_evidence_id"] = evidence_id
            if is_new:
                current["history"] = (
                    current.get("history", [])
                    + [
                        {
                            "evidence_id": evidence_id,
                            "verdict": evidence.get("verdict"),
                            "data_gate_passed": bool(data_gate.get("passed")),
                            "data_id": data_id,
                            "independent": is_new_data,
                        }
                    ]
                )[-12:]
            state["factors"][name] = current
        if is_new:
            processed.add(evidence_id)
        state["processed_evidence_ids"] = sorted(processed)[-24:]
        if is_new_data:
            processed_data.add(data_id)
        state["processed_data_ids"] = sorted(processed_data)[-24:]
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["data_gate_passed"] = bool(data_gate.get("passed"))
        state["summary"] = {
            "anchors": [
                name for name, item in state["factors"].items() if item["anchor"]
            ],
            "active": [
                name
                for name, item in state["factors"].items()
                if item["status"] in {"ACTIVE", "ANCHOR_ACTIVE"}
            ],
            "watch": [
                name
                for name, item in state["factors"].items()
                if "WATCH" in item["status"] or "FROZEN" in item["status"]
            ],
            "quarantined": [
                name
                for name, item in state["factors"].items()
                if item["status"] == "QUARANTINED"
            ],
        }
        if write:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.state_path)
        return state


def _load_latest_walk_forward(
    directory: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    paths = sorted(directory.glob("walk_forward_*.json"))
    if not paths:
        return None, {
            "status": "MISSING",
            "path": None,
            "rejection_code": "WALK_FORWARD_EVIDENCE_MISSING",
        }
    path = paths[-1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, {
            "status": "REJECTED",
            "path": str(path),
            "rejection_code": type(exc).__name__,
        }
    try:
        adapted = adapt_walk_forward_evidence(raw)
    except EvidenceContractError as exc:
        return None, {
            "status": "REJECTED",
            "path": str(path),
            "rejection_code": exc.code,
        }
    return adapted, {
        "status": "ACCEPTED",
        "path": str(path),
        "rejection_code": None,
        "source_schema_version": adapted["source_schema_version"],
        "strategy_id": adapted["strategy_id"],
        "strategy_version": adapted["strategy_version"],
    }


def _latest_walk_forward(directory: Path) -> dict[str, Any] | None:
    evidence, _ = _load_latest_walk_forward(directory)
    return evidence


def _strategy_freeze_status(data_root: Path) -> dict[str, Any]:
    """预注册冻结信息段（2026-08-13 新增，只读 fail-open）。

    活跃冻结时给出 freeze_id/strategy_version/freeze_date 与
    ``observation_days``（freeze_date 到今天的自然日数，样本外观察进度）；
    无冻结或读取失败时返回 NO_ACTIVE_FREEZE。不参与任何 gate 判定。
    """

    try:
        freeze = StrategyFreezeStore(data_root=data_root).get_active_freeze()
    except Exception:
        return {"status": "NO_ACTIVE_FREEZE"}
    if not freeze:
        return {"status": "NO_ACTIVE_FREEZE"}
    freeze_date = _parse_date(freeze.get("freeze_date"))
    return {
        "status": "FROZEN",
        "freeze_id": freeze.get("freeze_id"),
        "strategy_version": freeze.get("strategy_version"),
        "freeze_date": freeze_date.isoformat() if freeze_date else None,
        "observation_days": (
            (date.today() - freeze_date).days if freeze_date else None
        ),
    }


def _current_paper_metrics(
    db_path: str | Path = "data/finance.duckdb",
    sim_targets_dir: str | Path = "reports/sim_targets",
) -> dict[str, Any]:
    """从冻结记录 + 模拟目标产出推导模拟观察天数。

    口径：最新 FROZEN 记录的 freeze_date 起，sim_targets 目录中日期
    >= freeze_date 的产出文件数（每天一份，缺日如实缺）。任何一步失败
    都 fail-closed 为 0 天，绝不放大观察期。
    """
    try:
        import duckdb

        con = duckdb.connect(str(db_path), read_only=True)
        try:
            row = con.execute(
                "SELECT freeze_id, freeze_date::VARCHAR FROM strategy_freeze "
                "WHERE status = 'FROZEN' ORDER BY freeze_date DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if not row:
            return {"days": 0, "reason": "NO_FROZEN_RECORD"}
        freeze_date = str(row[1])
        days = 0
        targets_dir = Path(sim_targets_dir)
        if targets_dir.exists():
            for path in targets_dir.glob("*.json"):
                if path.stem >= freeze_date:
                    days += 1
        return {
            "days": days,
            "freeze_id": str(row[0]),
            "freeze_date": freeze_date,
        }
    except Exception:
        return {"days": 0, "reason": "PAPER_METRICS_UNAVAILABLE"}


def run_current_governance(
    *,
    policy_path: str | Path = "config/strategy_evolution.json",
    factor_report_path: str | Path = "reports/factor_research/factor_research.json",
    walk_forward_dir: str | Path = "strategy_report",
    candidate_config: Mapping[str, Any] | None = None,
    lifecycle_path: str | Path = "data/governance/factor_lifecycle.json",
    status_path: str | Path = "data/governance/strategy_evolution_status.json",
    walk_forward_input_status: Mapping[str, Any] | None = None,
    sim_targets_dir: str | Path = "reports/sim_targets",
) -> dict[str, Any]:
    policy = EvolutionPolicy.load(policy_path)
    try:
        research = json.loads(Path(factor_report_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        research = {}
    walk_forward, walk_forward_status = _load_latest_walk_forward(
        Path(walk_forward_dir)
    )
    governor = StrategyEvolutionGovernor(policy)
    result = governor.evaluate(
        factor_research=research,
        walk_forward=walk_forward,
        candidate_config=candidate_config,
        paper_metrics=_current_paper_metrics(
            db_path=Path(status_path).parent.parent / "finance.duckdb",
            sim_targets_dir=sim_targets_dir,
        ),
    )
    result["walk_forward_evidence"] = walk_forward_status
    status_file = Path(status_path)
    # 预注册冻结信息段（只读观测，不影响 gate 判定）
    result["strategy_freeze"] = _strategy_freeze_status(status_file.parent.parent)
    input_status = (
        dict(walk_forward_input_status)
        if isinstance(walk_forward_input_status, Mapping)
        else inspect_walk_forward_inputs(
            data_root=status_file.parent.parent,
            minimum_universe_size=policy.min_universe_size,
        )
    )
    result["walk_forward_inputs"] = input_status
    if input_status.get("status") != "READY_FOR_RESEARCH":
        input_failures = [
            str(item)
            for item in input_status.get(
                "blockers", ["WALK_FORWARD_INPUTS_NOT_READY"]
            )
        ]
        result["data_gate"]["failures"] = list(
            dict.fromkeys(
                [*result["data_gate"].get("failures", []), *input_failures]
            )
        )
        result["data_gate"]["passed"] = False
        result["stage"] = "BLOCKED_DATA"
    lifecycle = FactorLifecycle(policy, lifecycle_path).update(
        research, result["data_gate"]
    )
    result["factor_lifecycle"] = lifecycle["summary"]
    result["evidence_hash"] = hashlib.sha256(
        json.dumps(
            {
                "research": research.get("generated_at"),
                "walk_forward": walk_forward,
                "walk_forward_inputs": input_status,
                "candidate": candidate_config,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:16]
    path = status_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return result
