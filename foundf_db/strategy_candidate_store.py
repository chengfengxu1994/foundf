"""策略候选信号的不可变存储（schema ``foundf.strategy_candidate.v1``）。

本模块只记录研究信号与证据：生成时点、数据截止日、策略版本、标的、方向、
置信度和证据哈希。它不生成交易金额、不计算数量、不下单，也不输出任何
“建议买入多少钱”。``generated_at``（必须含时区）是防未来数据穿越的锚点：
任何成交都必须晚于候选生成时点。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .event_store import resolve_data_root
from .warehouse import Warehouse


SCHEMA_VERSION = "foundf.strategy_candidate.v1"
SIDES = {"BUY", "SELL", "HOLD_REDUCE"}
STATUSES = {"CANDIDATE", "USER_SUBMITTED", "EXPIRED", "REJECTED"}
# 与 portfolio_manager.decision_memory.DECISION_SOURCES 保持一致；
# 不直接 import，避免数据底座反向依赖上层组合模块。
SOURCES = {"USER", "AI_AGENT", "FACTOR_MODEL", "COMBINED"}
ALLOWED_TRANSITIONS = {
    "CANDIDATE": {"USER_SUBMITTED", "EXPIRED", "REJECTED"},
    "USER_SUBMITTED": {"EXPIRED"},
    "EXPIRED": set(),
    "REJECTED": set(),
}


class StrategyCandidateError(ValueError):
    """稳定的校验失败，可安全暴露在治理层。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _required(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyCandidateError(code)
    return value.strip()


def _iso_date(value: Any, code: str) -> str:
    raw = _required(value, code)
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise StrategyCandidateError(code) from exc


def _timestamp(value: Any, code: str) -> str:
    raw = _required(value, code).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise StrategyCandidateError(code) from exc
    if parsed.tzinfo is None:
        raise StrategyCandidateError(code)
    return parsed.astimezone(timezone.utc).isoformat()


def _conviction(value: Any, code: str) -> float:
    if isinstance(value, bool):
        raise StrategyCandidateError(code)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyCandidateError(code) from exc
    if not 0.0 <= result <= 1.0:
        raise StrategyCandidateError(code)
    return result


def _canonical_content(record: Mapping[str, Any]) -> str:
    """候选内容的规范化串；用于派生幂等的 candidate_id。"""

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": record["generated_at"],
        "data_as_of": record["data_as_of"],
        "strategy_version": record["strategy_version"],
        "symbol": record["symbol"],
        "side": record["side"],
        "conviction": record["conviction"],
        "evidence_hash": record["evidence_hash"],
        "source": record["source"],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class StrategyCandidateStore:
    """候选信号的幂等写入与只读查询；无任何交易行为。"""

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

    def record_candidate(
        self,
        *,
        generated_at: str,
        data_as_of: str,
        strategy_version: str,
        symbol: str,
        side: str,
        conviction: float,
        evidence_hash: str,
        source: str,
        decision_price: float | None = None,
    ) -> dict[str, Any]:
        """记录一条策略候选信号；相同内容重复记录返回 EXISTS（幂等）。

        ``decision_price`` 为可选的信号决策价（回归偏差口径基准），
        不参与内容哈希，缺失时保持 NULL 由调用方后续回填。
        """

        record = {
            "generated_at": _timestamp(generated_at, "GENERATED_AT_INVALID"),
            "data_as_of": _iso_date(data_as_of, "DATA_AS_OF_INVALID"),
            "strategy_version": _required(
                strategy_version, "STRATEGY_VERSION_MISSING"
            ),
            "symbol": _required(symbol, "SYMBOL_MISSING"),
            "side": _required(side, "SIDE_MISSING").upper(),
            "conviction": _conviction(conviction, "CONVICTION_INVALID"),
            "evidence_hash": _required(evidence_hash, "EVIDENCE_HASH_MISSING"),
            "source": _required(source, "SOURCE_MISSING").upper(),
        }
        if record["side"] not in SIDES:
            raise StrategyCandidateError("SIDE_UNSUPPORTED", record["side"])
        if record["source"] not in SOURCES:
            raise StrategyCandidateError("SOURCE_UNSUPPORTED", record["source"])
        content_hash = hashlib.sha256(
            _canonical_content(record).encode("utf-8")
        ).hexdigest()
        candidate_id = f"sc_{content_hash[:20]}"

        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            existing = warehouse.query(
                "SELECT candidate_id, status FROM strategy_candidate "
                "WHERE candidate_id = ?",
                [candidate_id],
            )
            if existing:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "EXISTS",
                    "candidate_id": candidate_id,
                    "candidate_status": existing[0]["status"],
                    "production_change_allowed": False,
                    "automatic_trade_allowed": False,
                }
            warehouse.insert(
                "strategy_candidate",
                [
                    {
                        "candidate_id": candidate_id,
                        **record,
                        "status": "CANDIDATE",
                        "content_sha256": content_hash,
                        "decision_price": (
                            float(decision_price)
                            if decision_price is not None else None
                        ),
                    }
                ],
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "RECORDED",
            "candidate_id": candidate_id,
            "candidate_status": "CANDIDATE",
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    def update_status(
        self,
        candidate_id: str,
        new_status: str,
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """按状态机推进候选状态并写审计；信号内容本身不可改写。"""

        identity = _required(candidate_id, "CANDIDATE_ID_MISSING")
        target = _required(new_status, "STATUS_MISSING").upper()
        if target not in STATUSES:
            raise StrategyCandidateError("STATUS_UNSUPPORTED", target)
        confirmation = _required(
            confirmation_reference, "CONFIRMATION_REFERENCE_MISSING"
        )
        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            rows = warehouse.query(
                "SELECT candidate_id, status FROM strategy_candidate "
                "WHERE candidate_id = ?",
                [identity],
            )
            if not rows:
                raise StrategyCandidateError("CANDIDATE_NOT_FOUND", identity)
            previous = rows[0]["status"]
            if target == previous:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "EXISTS",
                    "candidate_id": identity,
                    "candidate_status": previous,
                    "production_change_allowed": False,
                    "automatic_trade_allowed": False,
                }
            if target not in ALLOWED_TRANSITIONS.get(previous, set()):
                raise StrategyCandidateError(
                    "STATUS_TRANSITION_FORBIDDEN", f"{previous}->{target}"
                )
            written_at = datetime.now(timezone.utc).isoformat()
            audit_id = hashlib.sha256(
                f"{identity}|{previous}|{target}|{confirmation}".encode("utf-8")
            ).hexdigest()[:20]
            warehouse.execute("BEGIN TRANSACTION")
            try:
                warehouse.execute(
                    "UPDATE strategy_candidate SET status = ? WHERE candidate_id = ?",
                    [target, identity],
                )
                warehouse.insert(
                    "strategy_candidate_status_audit",
                    [
                        {
                            "audit_id": f"sca_{audit_id}",
                            "candidate_id": identity,
                            "previous_status": previous,
                            "new_status": target,
                            "confirmation_reference": confirmation,
                        }
                    ],
                )
                warehouse.execute("COMMIT")
            except Exception:
                warehouse.execute("ROLLBACK")
                raise
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "STATUS_UPDATED",
            "candidate_id": identity,
            "candidate_status": target,
            "updated_at": written_at,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """按 candidate_id 只读查询单条候选及其状态审计。"""

        identity = _required(candidate_id, "CANDIDATE_ID_MISSING")
        if not self.db_path.is_file():
            return {"schema_version": SCHEMA_VERSION, "status": "NOT_FOUND",
                    "candidate_id": identity}
        with Warehouse(self.db_path) as warehouse:
            if not warehouse.table_exists("strategy_candidate"):
                return {"schema_version": SCHEMA_VERSION, "status": "NOT_FOUND",
                        "candidate_id": identity}
            rows = warehouse.query(
                "SELECT * FROM strategy_candidate WHERE candidate_id = ?",
                [identity],
            )
            audits = warehouse.query(
                "SELECT candidate_id, previous_status, new_status, "
                "confirmation_reference, written_at "
                "FROM strategy_candidate_status_audit WHERE candidate_id = ? "
                "ORDER BY written_at",
                [identity],
            )
        if not rows:
            return {"schema_version": SCHEMA_VERSION, "status": "NOT_FOUND",
                    "candidate_id": identity}
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "OK",
            "candidate": rows[0],
            "status_audit": audits,
        }

    def list_candidates(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """只读列出候选；可选按状态或标的过滤。"""

        filters: list[str] = []
        params: list[Any] = []
        if status is not None:
            normalized = status.strip().upper()
            if normalized not in STATUSES:
                raise StrategyCandidateError("STATUS_UNSUPPORTED", normalized)
            filters.append("status = ?")
            params.append(normalized)
        if symbol is not None:
            filters.append("symbol = ?")
            params.append(symbol.strip())
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        if not self.db_path.is_file():
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "DATABASE_MISSING",
                "total": 0,
                "candidates": [],
            }
        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            rows = warehouse.query(
                "SELECT candidate_id, generated_at, data_as_of, strategy_version, "
                "symbol, side, conviction, evidence_hash, status, source, "
                "created_at FROM strategy_candidate"
                f"{where} ORDER BY generated_at, candidate_id",
                params or None,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "OK",
            "total": len(rows),
            "candidates": rows,
        }


def export_candidates(
    *,
    data_root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Sequence[Mapping[str, Any]]:
    """只读导出全部候选，供回归数据集构建器等下游纯计算消费。"""

    store = StrategyCandidateStore(data_root=data_root, db_path=db_path)
    return store.list_candidates()["candidates"]
