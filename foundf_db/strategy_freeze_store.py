"""策略预注册冻结的不可变存储（schema ``foundf.strategy_freeze.v1``）。

预注册机制（pre-registration freeze）回答外部审查的「真正的样本外测试
不存在」：冻结时把策略版本、参数、宇宙和评价指标做成快照 + 哈希，
``freeze_date`` 是样本外观察起点——观察只允许用此日之后新发生的数据。

边界（与系统治理一致，不可突破）：

- 冻结后规则不可变：本模块不提供任何修改 params/universe/metrics 的接口，
  只能退役（RETIRED_*）或被同 strategy_id 的新冻结取代（SUPERSEDED）。
- 创建即 FROZEN 且必须带人工审批（reviewer + confirmation_reference），
  缺失 fail-closed 拒绝。
- 失败版本保留：RETIRED_FAILED 是终态且记录永不删除，防选择性报告。
- 每个 strategy_id 最多一条 FROZEN；单向状态机 + 状态审计。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .event_store import resolve_data_root
from .warehouse import Warehouse


SCHEMA_VERSION = "foundf.strategy_freeze.v1"
STATUSES = {"FROZEN", "SUPERSEDED", "RETIRED_FAILED", "RETIRED_SUCCESS"}
ALLOWED_TRANSITIONS = {
    "FROZEN": {"SUPERSEDED", "RETIRED_FAILED", "RETIRED_SUCCESS"},
    "SUPERSEDED": set(),
    "RETIRED_FAILED": set(),
    "RETIRED_SUCCESS": set(),
}
RETIRE_OUTCOMES = {"FAILED": "RETIRED_FAILED", "SUCCESS": "RETIRED_SUCCESS"}
# 模拟观察期主线策略的默认 strategy_id（daily_candidates / 治理评估共用）。
DEFAULT_SIM_STRATEGY_ID = "multifactor_sim"


class StrategyFreezeError(ValueError):
    """稳定的校验失败，可安全暴露在治理层。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _required(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyFreezeError(code)
    return value.strip()


def _iso_date(value: Any, code: str) -> str:
    raw = _required(value, code)
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise StrategyFreezeError(code) from exc


def _canonical_json(payload: Any) -> str:
    """规范化 JSON 串（sort_keys），入库与哈希的统一口径。"""

    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _spec(value: Any, code: str, *, required: bool) -> tuple[str | None, str | None]:
    """校验一段规格（params/universe/metrics），返回 (canonical_json, hash16)。"""

    if value is None:
        if required:
            raise StrategyFreezeError(code)
        return None, None
    if not isinstance(value, Mapping) or not value:
        raise StrategyFreezeError(code)
    canonical = _canonical_json(dict(value))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return canonical, digest


def _git_code_ref() -> str | None:
    """freeze 时自动取 `git rev-parse --short HEAD`；取不到记 NULL。"""

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ref = out.stdout.strip()
    return ref if out.returncode == 0 and ref else None


class StrategyFreezeStore:
    """预注册冻结的写入与只读查询；无任何交易行为。"""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.data_root = resolve_data_root(data_root)
        self.db_path = Path(
            db_path or os.getenv("DUCKDB_PATH", "") or self.data_root / "finance.duckdb"
        )

    def create_freeze(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        params: Mapping[str, Any],
        freeze_date: str,
        reviewer: str,
        confirmation_reference: str,
        universe_spec: Mapping[str, Any] | None = None,
        metrics_spec: Mapping[str, Any] | None = None,
        supersede: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """创建一条预注册冻结（创建即 FROZEN，必须带人工审批）。

        同 strategy_id 已有 FROZEN 时：未显式 ``supersede=True`` 直接拒绝；
        显式 supersede 则旧记录在同一事务内转移 SUPERSEDED 并写审计。
        """

        record = {
            "strategy_id": _required(strategy_id, "STRATEGY_ID_MISSING"),
            "strategy_version": _required(
                strategy_version, "STRATEGY_VERSION_MISSING"
            ),
            "freeze_date": _iso_date(freeze_date, "FREEZE_DATE_INVALID"),
            "reviewer": _required(reviewer, "REVIEWER_MISSING"),
            "confirmation_reference": _required(
                confirmation_reference, "CONFIRMATION_REFERENCE_MISSING"
            ),
        }
        params_json, params_hash = _spec(params, "PARAMS_MISSING", required=True)
        universe_json, universe_hash = _spec(
            universe_spec, "UNIVERSE_SPEC_INVALID", required=False
        )
        metrics_json, _ = _spec(
            metrics_spec, "METRICS_SPEC_INVALID", required=False
        )
        content_hash = hashlib.sha256(
            _canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "strategy_id": record["strategy_id"],
                    "strategy_version": record["strategy_version"],
                    "params_hash": params_hash,
                    "universe_hash": universe_hash,
                    "metrics_spec_json": metrics_json,
                    "freeze_date": record["freeze_date"],
                }
            ).encode("utf-8")
        ).hexdigest()
        freeze_id = f"sf_{content_hash[:20]}"
        now = datetime.now(timezone.utc).isoformat()

        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            existing = warehouse.query(
                "SELECT freeze_id, status FROM strategy_freeze WHERE freeze_id = ?",
                [freeze_id],
            )
            if existing:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "EXISTS",
                    "freeze_id": freeze_id,
                    "freeze_status": existing[0]["status"],
                    "production_change_allowed": False,
                    "automatic_trade_allowed": False,
                }
            active = warehouse.query(
                "SELECT freeze_id FROM strategy_freeze "
                "WHERE strategy_id = ? AND status = 'FROZEN'",
                [record["strategy_id"]],
            )
            superseded_id: str | None = None
            if active:
                if not supersede:
                    raise StrategyFreezeError(
                        "FREEZE_ALREADY_ACTIVE",
                        f"{record['strategy_id']} 已有活跃冻结 "
                        f"{active[0]['freeze_id']}，需显式 --supersede 取代",
                    )
                superseded_id = active[0]["freeze_id"]
            warehouse.execute("BEGIN TRANSACTION")
            try:
                if superseded_id is not None:
                    self._transition(
                        warehouse,
                        superseded_id,
                        from_status="FROZEN",
                        to_status="SUPERSEDED",
                        actor=record["reviewer"],
                        reason=reason
                        or f"被新冻结 {freeze_id} 取代"
                        f"（依据 {record['confirmation_reference']}）",
                    )
                warehouse.insert(
                    "strategy_freeze",
                    [
                        {
                            "freeze_id": freeze_id,
                            **record,
                            "params_json": params_json,
                            "params_hash": params_hash,
                            "universe_spec_json": universe_json,
                            "universe_hash": universe_hash,
                            "metrics_spec_json": metrics_json,
                            "code_ref": _git_code_ref(),
                            "status": "FROZEN",
                            "created_at": now,
                            "updated_at": now,
                        }
                    ],
                )
                self._audit(
                    warehouse,
                    freeze_id,
                    from_status=None,
                    to_status="FROZEN",
                    actor=record["reviewer"],
                    reason=record["confirmation_reference"],
                )
                warehouse.execute("COMMIT")
            except Exception:
                warehouse.execute("ROLLBACK")
                raise
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "FROZEN",
            "freeze_id": freeze_id,
            "freeze_status": "FROZEN",
            "strategy_id": record["strategy_id"],
            "freeze_date": record["freeze_date"],
            "superseded_freeze_id": superseded_id,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    def retire(
        self,
        freeze_id: str,
        *,
        outcome: str,
        reviewer: str,
        confirmation_reference: str,
        reason: str,
    ) -> dict[str, Any]:
        """FROZEN → RETIRED_FAILED / RETIRED_SUCCESS（终态，记录保留不删除）。"""

        identity = _required(freeze_id, "FREEZE_ID_MISSING")
        normalized = _required(outcome, "OUTCOME_MISSING").upper()
        if normalized not in RETIRE_OUTCOMES:
            raise StrategyFreezeError("OUTCOME_UNSUPPORTED", normalized)
        target = RETIRE_OUTCOMES[normalized]
        actor = _required(reviewer, "REVIEWER_MISSING")
        confirmation = _required(
            confirmation_reference, "CONFIRMATION_REFERENCE_MISSING"
        )
        detail = _required(reason, "REASON_MISSING")
        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            rows = warehouse.query(
                "SELECT freeze_id, status FROM strategy_freeze WHERE freeze_id = ?",
                [identity],
            )
            if not rows:
                raise StrategyFreezeError("FREEZE_NOT_FOUND", identity)
            previous = rows[0]["status"]
            if target not in ALLOWED_TRANSITIONS.get(previous, set()):
                raise StrategyFreezeError(
                    "STATUS_TRANSITION_FORBIDDEN", f"{previous}->{target}"
                )
            warehouse.execute("BEGIN TRANSACTION")
            try:
                self._transition(
                    warehouse,
                    identity,
                    from_status=previous,
                    to_status=target,
                    actor=actor,
                    reason=f"{detail}（依据 {confirmation}）",
                )
                warehouse.execute("COMMIT")
            except Exception:
                warehouse.execute("ROLLBACK")
                raise
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "STATUS_UPDATED",
            "freeze_id": identity,
            "freeze_status": target,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    @staticmethod
    def _transition(
        warehouse: Warehouse,
        freeze_id: str,
        *,
        from_status: str,
        to_status: str,
        actor: str,
        reason: str,
    ) -> None:
        warehouse.execute(
            "UPDATE strategy_freeze SET status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE freeze_id = ?",
            [to_status, freeze_id],
        )
        StrategyFreezeStore._audit(
            warehouse,
            freeze_id,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            reason=reason,
        )

    @staticmethod
    def _audit(
        warehouse: Warehouse,
        freeze_id: str,
        *,
        from_status: str | None,
        to_status: str,
        actor: str,
        reason: str,
    ) -> None:
        audit_id = hashlib.sha256(
            f"{freeze_id}|{from_status}|{to_status}|{actor}|{reason}|"
            f"{datetime.now(timezone.utc).isoformat()}".encode("utf-8")
        ).hexdigest()[:20]
        # "at" 是 DuckDB 保留字，warehouse.insert 不转义列名，改用显式 SQL。
        warehouse.execute(
            'INSERT INTO strategy_freeze_audit '
            '(audit_id, freeze_id, from_status, to_status, actor, reason) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            [f"sfa_{audit_id}", freeze_id, from_status, to_status, actor, reason],
        )

    def get_active_freeze(
        self, strategy_id: str = DEFAULT_SIM_STRATEGY_ID
    ) -> dict[str, Any] | None:
        """该 strategy_id 当前 FROZEN 记录；库/表不存在或无冻结返回 None。"""

        if not self.db_path.is_file():
            return None
        with Warehouse(self.db_path) as warehouse:
            if not warehouse.table_exists("strategy_freeze"):
                return None
            rows = warehouse.query(
                "SELECT * FROM strategy_freeze "
                "WHERE strategy_id = ? AND status = 'FROZEN'",
                [strategy_id],
            )
        return rows[0] if rows else None

    def observation_start(
        self, strategy_id: str = DEFAULT_SIM_STRATEGY_ID
    ) -> str | None:
        """样本外观察起点（FROZEN 记录的 freeze_date，ISO 串）；无冻结 None。"""

        freeze = self.get_active_freeze(strategy_id)
        if not freeze:
            return None
        raw = freeze["freeze_date"]
        return raw.isoformat() if hasattr(raw, "isoformat") else str(raw)

    def get_freeze(self, freeze_id: str) -> dict[str, Any]:
        """按 freeze_id 只读查询单条冻结及其状态审计。"""

        identity = _required(freeze_id, "FREEZE_ID_MISSING")
        if not self.db_path.is_file():
            return {"schema_version": SCHEMA_VERSION, "status": "NOT_FOUND",
                    "freeze_id": identity}
        with Warehouse(self.db_path) as warehouse:
            if not warehouse.table_exists("strategy_freeze"):
                return {"schema_version": SCHEMA_VERSION, "status": "NOT_FOUND",
                        "freeze_id": identity}
            rows = warehouse.query(
                "SELECT * FROM strategy_freeze WHERE freeze_id = ?",
                [identity],
            )
            audits = warehouse.query(
                'SELECT freeze_id, from_status, to_status, actor, reason, "at" '
                "FROM strategy_freeze_audit WHERE freeze_id = ? "
                'ORDER BY "at", audit_id',
                [identity],
            )
        if not rows:
            return {"schema_version": SCHEMA_VERSION, "status": "NOT_FOUND",
                    "freeze_id": identity}
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "OK",
            "freeze": rows[0],
            "status_audit": audits,
        }

    def list_freezes(
        self,
        *,
        status: str | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """只读列出冻结记录；可选按状态或 strategy_id 过滤（含失败终态）。"""

        filters: list[str] = []
        params: list[Any] = []
        if status is not None:
            normalized = status.strip().upper()
            if normalized not in STATUSES:
                raise StrategyFreezeError("STATUS_UNSUPPORTED", normalized)
            filters.append("status = ?")
            params.append(normalized)
        if strategy_id is not None:
            filters.append("strategy_id = ?")
            params.append(strategy_id.strip())
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        if not self.db_path.is_file():
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "DATABASE_MISSING",
                "total": 0,
                "freezes": [],
            }
        with Warehouse(self.db_path) as warehouse:
            if not warehouse.table_exists("strategy_freeze"):
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "OK",
                    "total": 0,
                    "freezes": [],
                }
            rows = warehouse.query(
                "SELECT freeze_id, strategy_id, strategy_version, params_hash, "
                "universe_hash, freeze_date, code_ref, status, reviewer, "
                "confirmation_reference, created_at, updated_at "
                "FROM strategy_freeze"
                f"{where} ORDER BY freeze_date, freeze_id",
                params or None,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "OK",
            "total": len(rows),
            "freezes": rows,
        }
