"""长期数据资产健康检查；只读，不修饰或填补缺失值。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from .backup import verify_backup
from .event_store import resolve_data_root


CORE_TABLES = (
    "stock_basic",
    "daily_price",
    "financial_statement",
    "macro_data",
    "news_event",
    "portfolio",
    "investment_event",
    "event_candidate",
    "event_evidence",
    "event_outcome_review",
    "investment_lesson",
    "research_institution",
    "research_report",
    "research_claim",
    "research_claim_outcome",
    "research_institution_score",
    "event_write_audit",
    "market_watchlist",
    "market_watchlist_item",
    "market_quote_snapshot",
    "market_quote_collection_run",
    "market_watch_write_audit",
    "broker_sim_export_archive",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_drill_projection(
    root: Path,
    latest_backup: Path | None,
    today: date,
) -> dict[str, Any]:
    evidence_root = root / "governance" / "restore_drills"
    paths = (
        sorted(evidence_root.glob("*.json"), reverse=True)
        if evidence_root.exists()
        else []
    )
    if not paths:
        return {
            "status": "MISSING",
            "completed_at": None,
            "backup_id": None,
            "evidence_path": None,
        }
    for path in paths:
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            completed_at = datetime.fromisoformat(evidence["completed_at"])
            backup_id = Path(str(evidence["backup_path"])).name
            manifest_hash = evidence["backup_manifest_sha256"]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        status = "FAILED"
        if evidence.get("status") == "PASSED":
            if latest_backup is None or backup_id != latest_backup.name:
                status = "STALE_BACKUP"
            else:
                manifest_path = latest_backup / "manifest.json"
                try:
                    status = (
                        "PASSED"
                        if manifest_path.is_file()
                        and _file_sha256(manifest_path) == manifest_hash
                        else "EVIDENCE_MISMATCH"
                    )
                except OSError:
                    status = "EVIDENCE_MISMATCH"
        age_days = max(0, (today - completed_at.date()).days)
        if status == "PASSED" and age_days > 31:
            status = "STALE_TIME"
        return {
            "status": status,
            "completed_at": completed_at.isoformat(),
            "age_days": age_days,
            "backup_id": backup_id,
            "evidence_path": str(path),
        }
    return {
        "status": "INVALID",
        "completed_at": None,
        "backup_id": None,
        "evidence_path": None,
    }


def inspect_data_assets(
    *,
    data_root: str | Path | None = None,
    db_path: str | Path | None = None,
    now: date | None = None,
) -> dict[str, Any]:
    root = resolve_data_root(data_root)
    database = Path(db_path or root / "finance.duckdb")
    today = now or datetime.now(timezone.utc).date()
    if not database.exists():
        return {
            "status": "CRITICAL",
            "database_ready": False,
            "decision_data_ready": False,
            "blockers": ["DATABASE_MISSING"],
            "warnings": [],
            "db_path": str(database),
        }

    blockers: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int | None] = {}
    latest_price_date = None
    coverage: dict[str, Any] = {
        "daily_price_symbols": 0,
        "daily_price_symbols_with_security_master": 0,
        "security_master_price_coverage": 0.0,
        "daily_price_symbols_with_fresh_price": 0,
        "daily_price_fresh_coverage": 0.0,
        "portfolio_symbols": 0,
        "portfolio_symbols_with_security_master": 0,
        "portfolio_security_master_coverage": 0.0,
        "portfolio_symbols_with_fresh_price": 0,
        "portfolio_fresh_price_coverage": 0.0,
        "fresh_price_cutoff": (today - timedelta(days=7)).isoformat(),
    }
    try:
        # API 行情采集会短暂持有同进程的读写连接。DuckDB 不允许同一进程
        # 同时用不同配置（read_only=True/False）打开同一文件，因此这里使用
        # 相同连接配置，但本函数仍只执行 SELECT，不承担任何写入职责。
        conn = duckdb.connect(str(database))
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        }
        missing_tables = sorted(set(CORE_TABLES) - existing)
        if missing_tables:
            blockers.append("SCHEMA_INCOMPLETE")
        for table in CORE_TABLES:
            counts[table] = (
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if table in existing
                else None
            )
        # 宿主机盘中行情链路写 cn_quote_snapshot；market_quote_snapshot 是
        # Dashboard 旧 watchlist 口径。任一链路有数据都不应误报“实时行情为空”。
        counts["cn_quote_snapshot"] = (
            conn.execute("SELECT COUNT(*) FROM cn_quote_snapshot").fetchone()[0]
            if "cn_quote_snapshot" in existing
            else None
        )
        if "daily_price" in existing:
            latest_price_date = conn.execute(
                "SELECT MAX(date) FROM daily_price"
            ).fetchone()[0]
        if {"daily_price", "stock_basic"} <= existing:
            daily_symbols, mastered_symbols, fresh_daily_symbols = conn.execute(
                "SELECT COUNT(DISTINCT dp.symbol), "
                "COUNT(DISTINCT CASE WHEN sb.code IS NOT NULL THEN dp.symbol END), "
                "COUNT(DISTINCT CASE WHEN dp.date >= ? THEN dp.symbol END) "
                "FROM daily_price dp "
                "LEFT JOIN stock_basic sb ON sb.code = dp.symbol",
                [today - timedelta(days=7)],
            ).fetchone()
            coverage["daily_price_symbols"] = daily_symbols
            coverage["daily_price_symbols_with_security_master"] = mastered_symbols
            coverage["security_master_price_coverage"] = (
                round(mastered_symbols / daily_symbols, 6)
                if daily_symbols
                else 0.0
            )
            coverage["daily_price_symbols_with_fresh_price"] = fresh_daily_symbols
            coverage["daily_price_fresh_coverage"] = (
                round(fresh_daily_symbols / daily_symbols, 6)
                if daily_symbols
                else 0.0
            )
        if {"portfolio", "stock_basic", "daily_price"} <= existing:
            (
                portfolio_symbols,
                portfolio_mastered,
                portfolio_fresh,
            ) = conn.execute(
                "WITH latest AS ("
                "  SELECT symbol, MAX(date) AS latest_date "
                "  FROM daily_price GROUP BY symbol"
                ") "
                "SELECT COUNT(DISTINCT p.symbol), "
                "COUNT(DISTINCT CASE WHEN sb.code IS NOT NULL THEN p.symbol END), "
                "COUNT(DISTINCT CASE WHEN latest.latest_date >= ? "
                "                    THEN p.symbol END) "
                "FROM portfolio p "
                "LEFT JOIN stock_basic sb ON sb.code = p.symbol "
                "LEFT JOIN latest ON latest.symbol = p.symbol "
                "WHERE p.shares > 0",
                [today - timedelta(days=7)],
            ).fetchone()
            coverage["portfolio_symbols"] = portfolio_symbols
            coverage["portfolio_symbols_with_security_master"] = portfolio_mastered
            coverage["portfolio_security_master_coverage"] = (
                round(portfolio_mastered / portfolio_symbols, 6)
                if portfolio_symbols
                else 0.0
            )
            coverage["portfolio_symbols_with_fresh_price"] = portfolio_fresh
            coverage["portfolio_fresh_price_coverage"] = (
                round(portfolio_fresh / portfolio_symbols, 6)
                if portfolio_symbols
                else 0.0
            )
        event_paths = (
            conn.execute(
                "SELECT archive_relpath FROM investment_event"
            ).fetchall()
            if "investment_event" in existing
            else []
        )
        report_paths = (
            conn.execute(
                "SELECT archive_relpath FROM research_report"
            ).fetchall()
            if "research_report" in existing
            else []
        )
        broker_sim_paths = (
            conn.execute(
                "SELECT archive_relpath FROM broker_sim_export_archive"
            ).fetchall()
            if "broker_sim_export_archive" in existing
            else []
        )
        conn.close()
    except Exception as exc:
        return {
            "status": "CRITICAL",
            "database_ready": False,
            "decision_data_ready": False,
            "blockers": ["DATABASE_READ_FAILED"],
            "warnings": [],
            "error_type": type(exc).__name__,
            "db_path": str(database),
        }

    stale_days = (
        (today - latest_price_date).days if latest_price_date is not None else None
    )
    if latest_price_date is None:
        blockers.append("MARKET_DATA_MISSING")
    elif stale_days > 7:
        blockers.append("MARKET_DATA_STALE")
    elif stale_days > 3:
        warnings.append("MARKET_DATA_DELAYED")
    if (
        coverage["daily_price_symbols"] > 0
        and coverage["daily_price_fresh_coverage"] < 0.95
    ):
        blockers.append("MARKET_PRICE_COVERAGE_LOW")
    if not counts.get("portfolio"):
        blockers.append("PORTFOLIO_EMPTY")
    if not counts.get("stock_basic"):
        blockers.append("SECURITY_MASTER_EMPTY")
    elif (
        coverage["daily_price_symbols"] > 0
        and coverage["security_master_price_coverage"] < 0.95
    ):
        blockers.append("SECURITY_MASTER_PRICE_COVERAGE_LOW")
    if coverage["portfolio_symbols"] > 0:
        if coverage["portfolio_security_master_coverage"] < 1.0:
            blockers.append("PORTFOLIO_SECURITY_MASTER_COVERAGE_LOW")
        if coverage["portfolio_fresh_price_coverage"] < 0.95:
            blockers.append("PORTFOLIO_PRICE_COVERAGE_LOW")
    if not counts.get("financial_statement"):
        warnings.append("FINANCIAL_DATA_EMPTY")
    if not counts.get("macro_data"):
        warnings.append("MACRO_DATA_EMPTY")
    if not counts.get("investment_event"):
        warnings.append("EVENT_HISTORY_EMPTY")
    if not counts.get("research_report"):
        warnings.append("RESEARCH_HISTORY_EMPTY")
    if not (
        counts.get("market_quote_snapshot")
        or counts.get("cn_quote_snapshot")
    ):
        warnings.append("MARKET_WATCH_SNAPSHOT_EMPTY")
    if not counts.get("broker_sim_export_archive"):
        warnings.append("BROKER_SIM_EXPORT_EMPTY")

    missing_event_archives = sum(
        not (root / row[0]).exists() for row in event_paths if row[0]
    )
    missing_report_archives = sum(
        not (root / row[0]).exists() for row in report_paths if row[0]
    )
    missing_broker_sim_archives = sum(
        not (root / row[0]).exists() for row in broker_sim_paths if row[0]
    )
    if missing_event_archives:
        blockers.append("EVENT_ARCHIVE_MISSING")
    if missing_report_archives:
        blockers.append("RESEARCH_ARCHIVE_MISSING")
    if missing_broker_sim_archives:
        blockers.append("BROKER_SIM_ARCHIVE_MISSING")

    backup_root = root / "backups"
    backups = sorted(
        (
            path
            for path in backup_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ),
        reverse=True,
    ) if backup_root.exists() else []
    latest_backup = backups[0] if backups else None
    backup_manifest_ready = bool(
        latest_backup and (latest_backup / "manifest.json").exists()
    )
    latest_backup_at = None
    backup_verification: dict[str, Any] = {
        "status": "MISSING",
        "failures": [],
    }
    if backup_manifest_ready:
        try:
            latest_backup_at = json.loads(
                (latest_backup / "manifest.json").read_text(encoding="utf-8")
            ).get("created_at")
        except (OSError, ValueError):
            backup_manifest_ready = False
        if backup_manifest_ready:
            backup_verification = verify_backup(latest_backup)
            backup_manifest_ready = backup_verification["status"] == "VALID"
    if not backup_manifest_ready:
        warnings.append("VERIFIED_BACKUP_NOT_FOUND")
    restore_drill = _restore_drill_projection(root, latest_backup, today)
    if restore_drill["status"] != "PASSED":
        warnings.append("RESTORE_DRILL_NOT_PASSED")

    decision_ready = not any(
        item
        in {
            "DATABASE_MISSING",
            "DATABASE_READ_FAILED",
            "SCHEMA_INCOMPLETE",
            "MARKET_DATA_MISSING",
            "MARKET_DATA_STALE",
            "MARKET_PRICE_COVERAGE_LOW",
            "PORTFOLIO_EMPTY",
            "SECURITY_MASTER_EMPTY",
            "SECURITY_MASTER_PRICE_COVERAGE_LOW",
            "PORTFOLIO_SECURITY_MASTER_COVERAGE_LOW",
            "PORTFOLIO_PRICE_COVERAGE_LOW",
            "EVENT_ARCHIVE_MISSING",
            "RESEARCH_ARCHIVE_MISSING",
            "BROKER_SIM_ARCHIVE_MISSING",
        }
        for item in blockers
    )
    status = "READY" if decision_ready and not warnings else (
        "DEGRADED" if not blockers else "BLOCKED_DATA"
    )
    return {
        "status": status,
        "database_ready": True,
        "decision_data_ready": decision_ready,
        "as_of": today.isoformat(),
        "db_path": str(database),
        "latest_market_date": (
            latest_price_date.isoformat() if latest_price_date else None
        ),
        "market_stale_days": stale_days,
        "table_counts": counts,
        "coverage": coverage,
        "archive_integrity": {
            "missing_event_archives": missing_event_archives,
            "missing_research_archives": missing_report_archives,
            "missing_broker_sim_archives": missing_broker_sim_archives,
        },
        "backup": {
            "latest_path": str(latest_backup) if latest_backup else None,
            "created_at": latest_backup_at,
            "manifest_ready": backup_manifest_ready,
            "verification": backup_verification,
            "restore_drill": restore_drill,
        },
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }
