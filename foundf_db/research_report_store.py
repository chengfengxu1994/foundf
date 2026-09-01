"""研报长期归档、可检验主张和机构可靠性治理。

机构得分只影响该来源在证据链中的权重，不作为标的收益预测因子。自动计算只能给出
降权/阻断建议；真正将机构设为 BLOCKED 必须显式人工确认并写审计。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb

from .event_store import (
    _canonical_json,
    _confirmation,
    _exclusive_json,
    _hash,
    _iso_date,
    _iso_datetime,
    _score,
    resolve_data_root,
)
from .warehouse import Warehouse


DIRECTIONS = {"BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"}
CLAIM_TYPES = {
    "DIRECTION",
    "TARGET_PRICE",
    "EARNINGS",
    "REVENUE",
    "VALUATION",
    "RISK",
    "INDUSTRY_TREND",
}
INSTITUTION_STATUSES = {"ACTIVE", "WATCH", "REDUCED", "BLOCKED"}
METHODOLOGY_VERSION = "bayesian_directional_v1"


class ResearchReportStore:
    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ):
        self.data_root = resolve_data_root(data_root)
        self.db_path = Path(db_path or self.data_root / "finance.duckdb")
        self.archive_root = self.data_root / "research_archive"

    def initialize(self) -> None:
        with Warehouse(self.db_path) as warehouse:
            warehouse.init()

    def register_institution(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        jurisdiction: str | None = None,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        confirmation = _confirmation(confirmation_reference)
        canonical_name = name.strip()
        if not canonical_name:
            raise ValueError("institution name is required")
        institution_id = f"ins_{_hash(canonical_name.casefold())[:20]}"
        payload_hash = _hash(
            {
                "name": canonical_name,
                "aliases": sorted(set(aliases or [])),
                "jurisdiction": jurisdiction,
            }
        )
        warehouse = Warehouse(self.db_path)
        warehouse.init()
        existing = warehouse.query(
            "SELECT institution_id, status FROM research_institution "
            "WHERE institution_id = ?",
            [institution_id],
        )
        if existing:
            warehouse.close()
            return {**existing[0], "result": "EXISTS"}
        warehouse.conn.execute("BEGIN TRANSACTION")
        try:
            warehouse.insert(
                "research_institution",
                [
                    {
                        "institution_id": institution_id,
                        "canonical_name": canonical_name,
                        "aliases_json": _canonical_json(
                            sorted(set(aliases or []))
                        ),
                        "jurisdiction": jurisdiction,
                    }
                ],
            )
            self._audit(
                warehouse,
                "CREATE",
                "RESEARCH_INSTITUTION",
                institution_id,
                confirmation,
                payload_hash,
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        return {
            "result": "CREATED",
            "institution_id": institution_id,
            "status": "ACTIVE",
        }

    def ingest_report(
        self,
        payload: dict[str, Any],
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        confirmation = _confirmation(confirmation_reference)
        institution_id = str(payload.get("institution_id", "")).strip()
        title = str(payload.get("title", "")).strip()
        if not institution_id or not title:
            raise ValueError("institution_id and title are required")
        published_at = _iso_datetime(
            payload.get("published_at"), "published_at"
        )
        claims = list(payload.get("claims") or [])
        if not claims:
            raise ValueError("at least one evaluable or explicitly unevaluable claim is required")
        url = str(payload.get("source_url", "")).strip()
        domain = (urlparse(url).hostname or "").removeprefix("www.").lower()
        storage_allowed = bool(payload.get("storage_allowed", False))
        normalized_claims = [
            self._normalize_claim(item, published_at) for item in claims
        ]
        canonical = {
            "institution_id": institution_id,
            "title": title,
            "published_at": published_at,
            "source_domain": domain,
            "source_url_hash": _hash(url) if url else None,
            "license_mode": str(payload.get("license_mode", "UNKNOWN")),
            "storage_allowed": storage_allowed,
            "analyst_names": sorted(
                {str(name).strip() for name in payload.get("analyst_names", [])}
            ),
            "claims": normalized_claims,
        }
        content_hash = str(payload.get("content_hash") or _hash(canonical))
        report_id = f"rpt_{content_hash[:20]}"
        year = datetime.fromisoformat(published_at).year
        report_dir = self.archive_root / str(year) / institution_id / report_id
        report_relpath = report_dir.relative_to(self.data_root).as_posix()

        warehouse = Warehouse(self.db_path)
        warehouse.init()
        if not warehouse.query(
            "SELECT 1 AS ok FROM research_institution WHERE institution_id = ?",
            [institution_id],
        ):
            warehouse.close()
            raise KeyError(f"institution not found: {institution_id}")
        existing = warehouse.query(
            "SELECT report_id, archive_relpath FROM research_report "
            "WHERE content_hash = ?",
            [content_hash],
        )
        if existing:
            warehouse.close()
            return {**existing[0], "result": "EXISTS"}

        archive_payload = {
            "schema_version": "foundf.research_report.v1",
            "report_id": report_id,
            "content_hash": content_hash,
            "confirmation_reference": confirmation,
            "institution_id": institution_id,
            "title": title,
            "published_at": published_at,
            "source_domain": domain,
            "source_url": url if storage_allowed else None,
            "license_mode": canonical["license_mode"],
            "storage_allowed": storage_allowed,
            "analyst_names": canonical["analyst_names"],
            "user_synthesis": str(payload.get("user_synthesis", "")).strip(),
            "raw_content": (
                str(payload.get("raw_content", "")) if storage_allowed else None
            ),
            "claims": normalized_claims,
        }
        _exclusive_json(report_dir / "report_v1.json", archive_payload)

        claim_rows = []
        for claim in normalized_claims:
            claim_id = f"clm_{_hash([report_id, claim])[:20]}"
            claim_rows.append(
                {
                    "claim_id": claim_id,
                    "report_id": report_id,
                    **claim,
                }
            )
        warehouse.conn.execute("BEGIN TRANSACTION")
        try:
            warehouse.insert(
                "research_report",
                [
                    {
                        "report_id": report_id,
                        "institution_id": institution_id,
                        "title": title,
                        "published_at": published_at,
                        "source_domain": domain or None,
                        "source_url": url if storage_allowed else None,
                        "license_mode": canonical["license_mode"],
                        "storage_allowed": storage_allowed,
                        "archive_relpath": report_relpath,
                        "content_hash": content_hash,
                        "verification_status": str(
                            payload.get(
                                "verification_status", "REVIEW_REQUIRED"
                            )
                        ).upper(),
                    }
                ],
            )
            warehouse.insert("research_claim", claim_rows)
            self._audit(
                warehouse,
                "CREATE",
                "RESEARCH_REPORT",
                report_id,
                confirmation,
                content_hash,
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        return {
            "result": "CREATED",
            "report_id": report_id,
            "claim_ids": [row["claim_id"] for row in claim_rows],
            "archive_relpath": report_relpath,
        }

    def evaluate_claim(
        self,
        claim_id: str,
        payload: dict[str, Any],
        *,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        confirmation = _confirmation(confirmation_reference)
        warehouse = Warehouse(self.db_path)
        warehouse.init()
        claims = warehouse.query(
            "SELECT c.*, r.institution_id FROM research_claim c "
            "JOIN research_report r ON r.report_id = c.report_id "
            "WHERE c.claim_id = ?",
            [claim_id],
        )
        if not claims:
            warehouse.close()
            raise KeyError(f"claim not found: {claim_id}")
        claim = claims[0]
        data_as_of = _iso_date(payload.get("data_as_of"), "data_as_of")
        if data_as_of < str(claim["horizon_date"]):
            warehouse.close()
            raise ValueError("claim cannot be evaluated before horizon_date")
        asset_return = self._optional_float(payload.get("asset_return"))
        benchmark_return = self._optional_float(payload.get("benchmark_return"))
        actual_value = self._optional_float(payload.get("actual_value"))
        benchmark_value = self._optional_float(payload.get("benchmark_value"))
        direction_correct = self._direction_result(
            str(claim["direction"]),
            asset_return,
            benchmark_return,
        )
        normalized_error = None
        if claim["target_value"] is not None and actual_value is not None:
            denominator = max(abs(float(claim["target_value"])), 1e-12)
            normalized_error = abs(actual_value - float(claim["target_value"])) / denominator
        if direction_correct is True:
            result = "CORRECT"
        elif direction_correct is False:
            result = "WRONG"
        elif normalized_error is not None and normalized_error <= 0.10:
            result = "CORRECT"
        elif normalized_error is not None and normalized_error <= 0.25:
            result = "PARTIAL"
        else:
            result = "NOT_EVALUABLE"
        evaluated_at = _iso_datetime(
            payload.get("evaluated_at") or datetime.now(timezone.utc),
            "evaluated_at",
        )
        canonical = {
            "claim_id": claim_id,
            "evaluated_at": evaluated_at,
            "data_as_of": data_as_of,
            "actual_value": actual_value,
            "benchmark_value": benchmark_value,
            "asset_return": asset_return,
            "benchmark_return": benchmark_return,
            "direction_correct": direction_correct,
            "normalized_error": normalized_error,
            "result": result,
            "evaluation_source": str(
                payload.get("evaluation_source", "")
            ).strip(),
            "notes": str(payload.get("notes", "")).strip() or None,
        }
        if not canonical["evaluation_source"]:
            warehouse.close()
            raise ValueError("evaluation_source is required")
        outcome_id = f"out_{_hash(canonical)[:20]}"
        warehouse.conn.execute("BEGIN TRANSACTION")
        try:
            warehouse.insert(
                "research_claim_outcome",
                [{"outcome_id": outcome_id, **canonical}],
            )
            self._audit(
                warehouse,
                "CREATE",
                "RESEARCH_CLAIM_OUTCOME",
                outcome_id,
                confirmation,
                _hash(canonical),
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        score = self.recalculate_institution_score(claim["institution_id"])
        return {
            "result": "CREATED",
            "outcome_id": outcome_id,
            "claim_result": result,
            "direction_correct": direction_correct,
            "institution_score": score,
        }

    def recalculate_institution_score(
        self, institution_id: str
    ) -> dict[str, Any]:
        """贝叶斯收缩评分，避免少量样本造成极端结论。"""

        warehouse = Warehouse(self.db_path)
        warehouse.init()
        rows = warehouse.query(
            "SELECT o.direction_correct, o.result "
            "FROM research_claim_outcome o "
            "JOIN research_claim c ON c.claim_id = o.claim_id "
            "JOIN research_report r ON r.report_id = c.report_id "
            "WHERE r.institution_id = ? "
            "AND o.result IN ('CORRECT', 'PARTIAL', 'WRONG')",
            [institution_id],
        )
        resolved = len(rows)
        correct = sum(
            1.0 if row["result"] == "CORRECT"
            else 0.5 if row["result"] == "PARTIAL"
            else 0.0
            for row in rows
        )
        directional_rows = [
            row for row in rows if row["direction_correct"] is not None
        ]
        directional_accuracy = (
            sum(bool(row["direction_correct"]) for row in directional_rows)
            / len(directional_rows)
            if directional_rows
            else None
        )
        # Beta(5,5) 中性先验，等价于先加入 10 个五对五样本。
        shrunk_accuracy = (correct + 5.0) / (resolved + 10.0)
        reliability = round(shrunk_accuracy * 100, 4)
        if resolved < 10:
            recommended = "INSUFFICIENT_SAMPLE"
            weight = 1.0
        elif resolved >= 20 and reliability < 35:
            recommended = "BLOCK_REVIEW"
            weight = 0.25
        elif reliability < 45:
            recommended = "REDUCED"
            weight = 0.50
        elif reliability < 55:
            recommended = "WATCH"
            weight = 0.75
        else:
            recommended = "ACTIVE"
            weight = 1.0
        score = {
            "institution_id": institution_id,
            "resolved_claims": resolved,
            "correct_claims": correct,
            "directional_accuracy": directional_accuracy,
            "shrunk_accuracy": shrunk_accuracy,
            "reliability_score": reliability,
            "evidence_weight": weight,
            "recommended_status": recommended,
            "methodology_version": METHODOLOGY_VERSION,
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }
        warehouse.insert(
            "research_institution_score",
            [score],
            conflict_strategy="replace",
        )
        warehouse.close()
        return score

    def set_institution_status(
        self,
        institution_id: str,
        status: str,
        *,
        reason: str,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """人工治理动作；BLOCKED 不会由准确率评分自动执行。"""

        confirmation = _confirmation(confirmation_reference)
        normalized = status.strip().upper()
        if normalized not in INSTITUTION_STATUSES:
            raise ValueError(f"unsupported institution status: {normalized}")
        reason = reason.strip()
        if not reason:
            raise ValueError("status reason is required")
        warehouse = Warehouse(self.db_path)
        warehouse.init()
        if not warehouse.query(
            "SELECT 1 AS ok FROM research_institution WHERE institution_id = ?",
            [institution_id],
        ):
            warehouse.close()
            raise KeyError(f"institution not found: {institution_id}")
        payload_hash = _hash([institution_id, normalized, reason])
        warehouse.conn.execute("BEGIN TRANSACTION")
        try:
            warehouse.execute(
                "UPDATE research_institution SET status = ?, status_reason = ?, "
                "human_confirmed = TRUE, updated_at = CURRENT_TIMESTAMP "
                "WHERE institution_id = ?",
                [normalized, reason, institution_id],
            )
            self._audit(
                warehouse,
                "STATUS_CHANGE",
                "RESEARCH_INSTITUTION",
                institution_id,
                confirmation,
                payload_hash,
            )
            warehouse.conn.execute("COMMIT")
        except Exception:
            warehouse.conn.execute("ROLLBACK")
            raise
        finally:
            warehouse.close()
        return {
            "institution_id": institution_id,
            "status": normalized,
            "human_confirmed": True,
        }

    def list_institutions(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        conn = duckdb.connect(str(self.db_path), read_only=True)
        try:
            return self._query(
                conn,
                "SELECT i.*, s.resolved_claims, s.directional_accuracy, "
                "s.reliability_score, s.evidence_weight, s.recommended_status, "
                "s.methodology_version, s.calculated_at "
                "FROM research_institution i "
                "LEFT JOIN research_institution_score s "
                "ON s.institution_id = i.institution_id "
                "ORDER BY i.canonical_name",
                [],
            )
        except duckdb.CatalogException:
            return []
        finally:
            conn.close()

    def list_reports(
        self,
        *,
        institution_id: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        where = []
        params: list[Any] = []
        if institution_id:
            where.append("r.institution_id = ?")
            params.append(institution_id)
        if symbol:
            where.append("c.symbol = ?")
            params.append(symbol.upper())
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        conn = duckdb.connect(str(self.db_path), read_only=True)
        try:
            return self._query(
                conn,
                "SELECT DISTINCT r.report_id, r.institution_id, i.canonical_name, "
                "r.title, r.published_at, r.source_domain, r.license_mode, "
                "r.storage_allowed, r.archive_relpath, r.verification_status "
                "FROM research_report r "
                "JOIN research_institution i ON i.institution_id = r.institution_id "
                "JOIN research_claim c ON c.report_id = r.report_id "
                f"{clause} ORDER BY r.published_at DESC LIMIT ?",
                [*params, max(1, min(1000, int(limit)))],
            )
        except duckdb.CatalogException:
            return []
        finally:
            conn.close()

    @staticmethod
    def _normalize_claim(item: dict[str, Any], published_at: str) -> dict[str, Any]:
        claim_type = str(item.get("claim_type", "")).strip().upper()
        direction = str(item.get("direction", "UNCERTAIN")).strip().upper()
        if claim_type not in CLAIM_TYPES:
            raise ValueError(f"unsupported claim_type: {claim_type}")
        if direction not in DIRECTIONS:
            raise ValueError(f"unsupported direction: {direction}")
        horizon_date = _iso_date(item.get("horizon_date"), "horizon_date")
        if horizon_date < published_at[:10]:
            raise ValueError("horizon_date cannot precede report publication")
        symbol = str(item.get("symbol", "")).strip().upper()
        summary = str(item.get("claim_summary", "")).strip()
        if not symbol or not summary:
            raise ValueError("claim symbol and summary are required")
        return {
            "symbol": symbol,
            "claim_type": claim_type,
            "claim_summary": summary,
            "direction": direction,
            "target_value": ResearchReportStore._optional_float(
                item.get("target_value")
            ),
            "target_unit": (
                str(item.get("target_unit", "")).strip() or None
            ),
            "base_value": ResearchReportStore._optional_float(
                item.get("base_value")
            ),
            "base_value_date": (
                _iso_date(item.get("base_value_date"), "base_value_date")
                if item.get("base_value_date")
                else None
            ),
            "benchmark_symbol": (
                str(item.get("benchmark_symbol", "")).strip().upper() or None
            ),
            "horizon_date": horizon_date,
            "confidence_score": _score(
                "claim.confidence_score", item.get("confidence_score", 0)
            ),
            "evaluable": bool(item.get("evaluable", True)),
        }

    @staticmethod
    def _direction_result(
        direction: str,
        asset_return: float | None,
        benchmark_return: float | None,
    ) -> bool | None:
        if asset_return is None or direction == "UNCERTAIN":
            return None
        comparison = (
            asset_return - benchmark_return
            if benchmark_return is not None
            else asset_return
        )
        if direction == "BULLISH":
            return comparison > 0
        if direction == "BEARISH":
            return comparison < 0
        if direction == "NEUTRAL":
            return abs(comparison) <= 0.05
        return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None or value == "" else float(value)

    @staticmethod
    def _query(
        conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
    ) -> list[dict[str, Any]]:
        cursor = conn.execute(sql, params)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _audit(
        warehouse: Warehouse,
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
