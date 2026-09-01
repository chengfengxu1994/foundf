"""NAS 长期重大事件资产库。

DuckDB 保存可查询索引，``data/event_archive`` 保存不可变的详细记录和授权证据。
所有写操作都要求显式确认引用；搜索摘要没有存储许可时只留下哈希审计。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import duckdb

from .warehouse import Warehouse


EVENT_TYPES = {
    "COMPANY",
    "INDUSTRY",
    "MACRO",
    "REGULATORY",
    "MARKET",
    "PORTFOLIO",
    "GEOPOLITICAL",
}
IMPACT_DIRECTIONS = {"POSITIVE", "NEGATIVE", "MIXED", "UNCERTAIN", "NEUTRAL"}
THESIS_RESULTS = {"SUPPORTED", "PARTIAL", "INVALIDATED", "INCONCLUSIVE"}
LESSON_STATUSES = {"CANDIDATE", "REVIEWED", "REJECTED", "PRODUCTION_REFERENCE"}
AUTHORITATIVE_LEVELS = {"PRIMARY", "LICENSED_PRIMARY"}


def resolve_data_root(data_root: str | Path | None = None) -> Path:
    """解析 NAS 数据根目录；容器可用 ``FOUNDF_DATA_ROOT=/app/data``。"""

    return Path(data_root or os.getenv("FOUNDF_DATA_ROOT", "data")).expanduser()


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _score(name: str, value: Any) -> int:
    score = int(value)
    if score < 0 or score > 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return score


def _iso_datetime(value: Any, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise ValueError(f"{name} is required")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=" ")


def _iso_date(value: Any, name: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return date.fromisoformat(text).isoformat()


def _confirmation(reference: str | None) -> str:
    value = str(reference or "").strip()
    if not value:
        raise PermissionError("长期事件写入必须提供 confirmation_reference")
    return value


def _exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    """创建不可变 JSON；存在即拒绝覆盖。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def _assessment(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    domains = {
        (urlparse(str(item.get("url", ""))).hostname or "")
        .removeprefix("www.")
        .lower()
        for item in evidence
        if item.get("url")
    }
    authoritative = any(
        str(item.get("authority_level", "")).upper() in AUTHORITATIVE_LEVELS
        for item in evidence
    )
    authorized_secondary = any(
        str(item.get("authority_level", "")).upper() == "AUTHORIZED_SECONDARY"
        for item in evidence
    )
    if authoritative and len(domains) >= 2:
        status = "CORROBORATED"
    elif authoritative or authorized_secondary:
        status = "REVIEW_REQUIRED"
    else:
        status = "DISCOVERY_ONLY"
    return {
        "verification_status": status,
        "decision_eligible": status == "CORROBORATED",
        "source_count": len(evidence),
        "independent_domains": len(domains),
    }


class InvestmentEventStore:
    """长期事件、证据、影响假设、事后复核和经验候选仓库。"""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ):
        self.data_root = resolve_data_root(data_root)
        self.db_path = Path(
            db_path
            or os.getenv("DUCKDB_PATH", "")
            or self.data_root / "finance.duckdb"
        )
        self.archive_root = self.data_root / "event_archive"

    def initialize(self) -> None:
        with Warehouse(self.db_path) as warehouse:
            warehouse.init()

    def ingest_event(
        self,
        payload: dict[str, Any],
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """写入重大事件；同一规范内容重复导入时保持幂等。"""

        confirmation = _confirmation(confirmation_reference)
        occurred_at = _iso_datetime(payload.get("occurred_at"), "occurred_at")
        event_type = str(payload.get("event_type", "")).strip().upper()
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {event_type}")
        title = str(payload.get("title", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        if not title or not summary:
            raise ValueError("title and summary are required")
        evidence = list(payload.get("evidence") or [])
        links = list(payload.get("asset_links") or [])
        assessment = _assessment(evidence)
        canonical = {
            "occurred_at": occurred_at,
            "event_type": event_type,
            "scope": str(payload.get("scope", "MARKET")).strip().upper(),
            "title": title,
            "summary": summary,
            "details": str(payload.get("details", "")).strip(),
            "materiality_score": _score(
                "materiality_score", payload.get("materiality_score", 0)
            ),
            "risk_score": _score("risk_score", payload.get("risk_score", 0)),
            "confidence_score": _score(
                "confidence_score", payload.get("confidence_score", 0)
            ),
            "market_regime": str(payload.get("market_regime", "")).strip() or None,
            "expected_horizon": (
                str(payload.get("expected_horizon", "")).strip() or None
            ),
            "asset_links": links,
            "evidence_hashes": [
                str(item.get("content_hash") or _hash(item)) for item in evidence
            ],
        }
        content_hash = _hash(canonical)
        event_id = f"evt_{content_hash[:20]}"
        archive_time = datetime.fromisoformat(occurred_at)
        event_dir = (
            self.archive_root
            / f"{archive_time.year:04d}"
            / f"{archive_time.month:02d}"
            / event_id
        )
        archive_relpath = event_dir.relative_to(self.data_root).as_posix()

        warehouse = Warehouse(self.db_path)
        warehouse.init()
        existing = warehouse.query(
            "SELECT event_id, verification_status, archive_relpath "
            "FROM investment_event WHERE content_hash = ?",
            [content_hash],
        )
        if existing:
            warehouse.close()
            return {**existing[0], "status": "EXISTS", "content_hash": content_hash}

        discovered_at = _iso_datetime(
            payload.get("discovered_at") or datetime.now(timezone.utc),
            "discovered_at",
        )
        sanitized_evidence = []
        evidence_rows = []
        for item in evidence:
            item_hash = str(item.get("content_hash") or _hash(item))
            evidence_id = f"evd_{item_hash[:20]}"
            url = str(item.get("url", "")).strip()
            domain = (urlparse(url).hostname or "").removeprefix("www.").lower()
            storage_allowed = bool(item.get("storage_allowed", False))
            evidence_relpath = None
            if storage_allowed:
                evidence_payload = {
                    "evidence_id": evidence_id,
                    "event_id": event_id,
                    "provider": str(item.get("provider", "unknown")),
                    "title": str(item.get("title", "")),
                    "url": url,
                    "content": str(
                        item.get("content", item.get("snippet", ""))
                    ),
                    "published_at": item.get("published_at"),
                    "retrieved_at": item.get("retrieved_at") or discovered_at,
                    "authority_level": str(
                        item.get("authority_level", "DISCOVERY_ONLY")
                    ),
                    "license_mode": str(item.get("license_mode", "UNKNOWN")),
                    "content_hash": item_hash,
                }
                evidence_path = event_dir / "evidence" / f"{evidence_id}.json"
                _exclusive_json(evidence_path, evidence_payload)
                evidence_relpath = evidence_path.relative_to(
                    self.data_root
                ).as_posix()
            sanitized_evidence.append(
                {
                    "evidence_id": evidence_id,
                    "provider": str(item.get("provider", "unknown")),
                    "source_domain": domain,
                    "authority_level": str(
                        item.get("authority_level", "DISCOVERY_ONLY")
                    ),
                    "license_mode": str(item.get("license_mode", "UNKNOWN")),
                    "storage_allowed": storage_allowed,
                    "content_hash": item_hash,
                    "archive_relpath": evidence_relpath,
                }
            )
            evidence_rows.append(
                {
                    "evidence_id": evidence_id,
                    "event_id": event_id,
                    "provider": str(item.get("provider", "unknown")),
                    "source_domain": domain or None,
                    "source_title": (
                        str(item.get("title", "")) if storage_allowed else None
                    ),
                    "source_url": url if storage_allowed else None,
                    "published_at": item.get("published_at") or None,
                    "retrieved_at": item.get("retrieved_at") or discovered_at,
                    "authority_level": str(
                        item.get("authority_level", "DISCOVERY_ONLY")
                    ),
                    "license_mode": str(item.get("license_mode", "UNKNOWN")),
                    "storage_allowed": storage_allowed,
                    "content_hash": item_hash,
                    "archive_relpath": evidence_relpath,
                }
            )

        link_rows = []
        for item in links:
            symbol = str(item.get("symbol", "")).strip().upper()
            relation = str(item.get("relation_type", "DIRECT")).strip().upper()
            direction = str(
                item.get("impact_direction", "UNCERTAIN")
            ).strip().upper()
            if not symbol:
                raise ValueError("asset link symbol is required")
            if direction not in IMPACT_DIRECTIONS:
                raise ValueError(f"unsupported impact_direction: {direction}")
            link_id = f"lnk_{_hash([event_id, symbol, relation])[:20]}"
            link_rows.append(
                {
                    "link_id": link_id,
                    "event_id": event_id,
                    "symbol": symbol,
                    "relation_type": relation,
                    "impact_direction": direction,
                    "impact_channels_json": _canonical_json(
                        item.get("impact_channels") or []
                    ),
                    "expected_effect": (
                        str(item.get("expected_effect", "")).strip() or None
                    ),
                    "confidence_score": _score(
                        "asset_link.confidence_score",
                        item.get("confidence_score", 0),
                    ),
                }
            )

        archive_payload = {
            "schema_version": "foundf.investment_event.v1",
            "event_id": event_id,
            "content_hash": content_hash,
            "recorded_at": discovered_at,
            "confirmation_reference": confirmation,
            "event": canonical,
            "assessment": assessment,
            "evidence_index": sanitized_evidence,
        }
        _exclusive_json(event_dir / "event_v1.json", archive_payload)

        try:
            warehouse.conn.execute("BEGIN TRANSACTION")
            warehouse.insert(
                "investment_event",
                [
                    {
                        "event_id": event_id,
                        "occurred_at": occurred_at,
                        "discovered_at": discovered_at,
                        "event_type": event_type,
                        "scope": canonical["scope"],
                        "title": title,
                        "summary": summary,
                        "materiality_score": canonical["materiality_score"],
                        "risk_score": canonical["risk_score"],
                        "confidence_score": canonical["confidence_score"],
                        **assessment,
                        "archive_relpath": archive_relpath,
                        "content_hash": content_hash,
                    }
                ],
            )
            warehouse.insert("event_evidence", evidence_rows)
            warehouse.insert("event_asset_link", link_rows)
            self._write_audit(
                warehouse,
                action="CREATE",
                object_type="INVESTMENT_EVENT",
                object_id=event_id,
                confirmation_reference=confirmation,
                payload_hash=content_hash,
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        return {
            "status": "CREATED",
            "event_id": event_id,
            "content_hash": content_hash,
            "verification_status": assessment["verification_status"],
            "decision_eligible": assessment["decision_eligible"],
            "archive_relpath": archive_relpath,
        }

    def queue_external_candidate(
        self,
        payload: dict[str, Any],
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """把自动采集结果放入待审核队列，不升级为重大事件。"""

        confirmation = _confirmation(confirmation_reference)
        detected_at = _iso_datetime(
            payload.get("detected_at") or datetime.now(timezone.utc),
            "detected_at",
        )
        query_hash = str(payload.get("query_hash", "")).strip()
        assessment = dict(payload.get("assessment") or {})
        if not query_hash:
            raise ValueError("query_hash is required")
        canonical = {
            "detected_at": detected_at,
            "query_hash": query_hash,
            "assessment_status": str(
                assessment.get("status", "DISCOVERY_ONLY")
            ),
            "decision_eligible": bool(
                assessment.get("decision_eligible", False)
            ),
            "evidence_count": int(assessment.get("evidence_count", 0)),
            "stored_evidence_count": int(payload.get("stored_count", 0)),
            "provider_status_json": _canonical_json(
                payload.get("provider_status") or {}
            ),
            "audit_relpath": payload.get("audit_relpath"),
        }
        candidate_id = f"can_{_hash(canonical)[:20]}"
        warehouse = Warehouse(self.db_path)
        warehouse.init()
        warehouse.conn.execute("BEGIN TRANSACTION")
        try:
            warehouse.insert(
                "event_candidate",
                [{"candidate_id": candidate_id, **canonical}],
            )
            self._write_audit(
                warehouse,
                action="QUEUE",
                object_type="EVENT_CANDIDATE",
                object_id=candidate_id,
                confirmation_reference=confirmation,
                payload_hash=_hash(canonical),
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        return {
            "status": "QUEUED",
            "candidate_id": candidate_id,
            "review_status": "PENDING",
        }

    def record_outcome(
        self,
        event_id: str,
        payload: dict[str, Any],
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """记录未来窗口的实际结果，不修改原始事件。"""

        confirmation = _confirmation(confirmation_reference)
        thesis_result = str(payload.get("thesis_result", "")).strip().upper()
        if thesis_result not in THESIS_RESULTS:
            raise ValueError(f"unsupported thesis_result: {thesis_result}")
        review_date = _iso_date(payload.get("review_date"), "review_date")
        data_as_of = _iso_date(payload.get("data_as_of"), "data_as_of")
        if data_as_of > review_date:
            raise ValueError("data_as_of cannot be after review_date")
        horizon_days = int(payload.get("horizon_days", 0))
        if horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        observed = str(payload.get("observed_outcome", "")).strip()
        lesson = str(payload.get("lesson", "")).strip()
        if not observed or not lesson:
            raise ValueError("observed_outcome and lesson are required")
        canonical = {
            "event_id": event_id,
            "review_date": review_date,
            "horizon_days": horizon_days,
            "observed_outcome": observed,
            "asset_return": payload.get("asset_return"),
            "benchmark_return": payload.get("benchmark_return"),
            "thesis_result": thesis_result,
            "causal_confidence": _score(
                "causal_confidence", payload.get("causal_confidence", 0)
            ),
            "lesson": lesson,
            "data_as_of": data_as_of,
        }
        review_id = f"rev_{_hash(canonical)[:20]}"
        warehouse = Warehouse(self.db_path)
        warehouse.init()
        if not warehouse.query(
            "SELECT 1 AS ok FROM investment_event WHERE event_id = ?", [event_id]
        ):
            warehouse.close()
            raise KeyError(f"event not found: {event_id}")
        try:
            warehouse.conn.execute("BEGIN TRANSACTION")
            warehouse.insert(
                "event_outcome_review",
                [{"review_id": review_id, **canonical}],
            )
            self._write_audit(
                warehouse,
                action="CREATE",
                object_type="EVENT_OUTCOME_REVIEW",
                object_id=review_id,
                confirmation_reference=confirmation,
                payload_hash=_hash(canonical),
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        review_path = (
            self.archive_root
            / "reviews"
            / review_date[:4]
            / f"{review_id}.json"
        )
        _exclusive_json(
            review_path,
            {
                "schema_version": "foundf.event_review.v1",
                "review_id": review_id,
                "confirmation_reference": confirmation,
                **canonical,
            },
        )
        return {
            "status": "CREATED",
            "review_id": review_id,
            "archive_relpath": review_path.relative_to(self.data_root).as_posix(),
        }

    def create_lesson(
        self,
        payload: dict[str, Any],
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """建立经验候选；没有人工确认时禁止进入生产参考状态。"""

        confirmation = _confirmation(confirmation_reference)
        status = str(payload.get("status", "CANDIDATE")).strip().upper()
        if status not in LESSON_STATUSES:
            raise ValueError(f"unsupported lesson status: {status}")
        human_confirmed = bool(payload.get("human_confirmed", False))
        if status == "PRODUCTION_REFERENCE" and not human_confirmed:
            raise PermissionError("生产经验必须 human_confirmed=true")
        event_ids = sorted({str(x) for x in payload.get("evidence_event_ids", [])})
        sample_size = int(payload.get("sample_size", len(event_ids)))
        if sample_size < len(event_ids) or sample_size <= 0:
            raise ValueError("sample_size must cover at least one evidence event")
        canonical = {
            "pattern_name": str(payload.get("pattern_name", "")).strip(),
            "event_type": str(payload.get("event_type", "")).strip().upper(),
            "market_regime": (
                str(payload.get("market_regime", "")).strip() or None
            ),
            "applicable_conditions": str(
                payload.get("applicable_conditions", "")
            ).strip(),
            "invalidation_conditions": str(
                payload.get("invalidation_conditions", "")
            ).strip(),
            "evidence_event_ids_json": _canonical_json(event_ids),
            "sample_size": sample_size,
            "status": status,
            "human_confirmed": human_confirmed,
        }
        if (
            not canonical["pattern_name"]
            or canonical["event_type"] not in EVENT_TYPES
            or not canonical["applicable_conditions"]
            or not canonical["invalidation_conditions"]
        ):
            raise ValueError("lesson identity, conditions and invalidation are required")
        lesson_id = f"les_{_hash(canonical)[:20]}"
        warehouse = Warehouse(self.db_path)
        warehouse.init()
        known = {
            row["event_id"]
            for row in warehouse.query(
                "SELECT event_id FROM investment_event WHERE event_id IN "
                f"({','.join('?' for _ in event_ids)})",
                event_ids,
            )
        }
        if known != set(event_ids):
            warehouse.close()
            raise KeyError("lesson references unknown event_id")
        try:
            warehouse.conn.execute("BEGIN TRANSACTION")
            warehouse.insert(
                "investment_lesson",
                [{"lesson_id": lesson_id, **canonical}],
            )
            self._write_audit(
                warehouse,
                action="CREATE",
                object_type="INVESTMENT_LESSON",
                object_id=lesson_id,
                confirmation_reference=confirmation,
                payload_hash=_hash(canonical),
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        return {"status": "CREATED", "lesson_id": lesson_id}

    def list_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        min_materiality: int = 0,
        event_type: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"total": 0, "events": [], "database_ready": False}
        where = ["e.materiality_score >= ?"]
        params: list[Any] = [max(0, min(100, int(min_materiality)))]
        join = ""
        if event_type:
            where.append("e.event_type = ?")
            params.append(event_type.upper())
        if symbol:
            join = " JOIN event_asset_link l ON l.event_id = e.event_id "
            where.append("l.symbol = ?")
            params.append(symbol.upper())
        where_sql = " AND ".join(where)
        conn = duckdb.connect(str(self.db_path), read_only=True)
        try:
            total = conn.execute(
                f"SELECT COUNT(DISTINCT e.event_id) FROM investment_event e "
                f"{join} WHERE {where_sql}",
                params,
            ).fetchone()[0]
            cursor = conn.execute(
                "SELECT DISTINCT e.event_id, e.occurred_at, e.event_type, "
                "e.scope, e.title, e.summary, e.materiality_score, e.risk_score, "
                "e.confidence_score, e.verification_status, e.decision_eligible, "
                "e.market_regime, e.expected_horizon, e.source_count, "
                "e.independent_domains, e.archive_relpath "
                f"FROM investment_event e {join} WHERE {where_sql} "
                "ORDER BY e.occurred_at DESC, e.event_id LIMIT ? OFFSET ?",
                [*params, max(1, min(1000, int(limit))), max(0, int(offset))],
            )
            columns = [item[0] for item in cursor.description]
            events = [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
        return {"total": total, "events": events, "database_ready": True}

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        conn = duckdb.connect(str(self.db_path), read_only=True)
        try:
            event = self._one(
                conn,
                "SELECT * FROM investment_event WHERE event_id = ?",
                [event_id],
            )
            if event is None:
                return None
            event["evidence"] = self._many(
                conn,
                "SELECT * FROM event_evidence WHERE event_id = ? "
                "ORDER BY created_at",
                [event_id],
            )
            event["asset_links"] = self._many(
                conn,
                "SELECT * FROM event_asset_link WHERE event_id = ? "
                "ORDER BY symbol",
                [event_id],
            )
            event["outcome_reviews"] = self._many(
                conn,
                "SELECT * FROM event_outcome_review WHERE event_id = ? "
                "ORDER BY review_date, horizon_days",
                [event_id],
            )
            return event
        finally:
            conn.close()

    @staticmethod
    def _many(
        conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any]
    ) -> list[dict[str, Any]]:
        cursor = conn.execute(sql, list(params))
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @classmethod
    def _one(
        cls, conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any]
    ) -> dict[str, Any] | None:
        rows = cls._many(conn, sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _write_audit(
        warehouse: Warehouse,
        *,
        action: str,
        object_type: str,
        object_id: str,
        confirmation_reference: str,
        payload_hash: str,
    ) -> None:
        warehouse.insert(
            "event_write_audit",
            [
                {
                    "audit_id": f"aud_{_hash([action, object_id, payload_hash])[:20]}",
                    "action": action,
                    "object_type": object_type,
                    "object_id": object_id,
                    "confirmation_reference": confirmation_reference,
                    "payload_hash": payload_hash,
                }
            ],
        )
