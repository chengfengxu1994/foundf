"""DuckDB 与长期事件归档的一致性 NAS 备份和校验。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

import duckdb

from .event_store import resolve_data_root


IMMUTABLE_ARCHIVE_DIRS = ("event_archive", "research_archive", "raw")
DURABLE_STATE_DIRS = ("governance", "source_registry")
PRIVATE_DATA_DIRS = ("portfolio_inputs",)
PRIVATE_DATA_FILES = ("daily_position_update.json", "portfolio_nav_history.json")
BACKUP_SCHEMA = "foundf.backup.v1"
RESTORE_DRILL_SCHEMA = "foundf.restore_drill.v1"
KEY_TABLES = (
    "stock_basic",
    "daily_price",
    "portfolio",
    "investment_event",
    "research_report",
    "market_quote_snapshot",
    "broker_sim_export_archive",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# api/collector 等进程会短暂持有 finance.duckdb 的文件锁；CHECKPOINT 需要
# 独占的读写连接，遇锁冲突（duckdb.IOException）按指数退避重试，穷尽后报错。
CHECKPOINT_RETRY_ATTEMPTS = 5
CHECKPOINT_RETRY_BACKOFF_SECONDS = 1.0
MIN_DATABASE_BYTES = 256 * 1024
MIN_FREE_SPACE_RESERVE_BYTES = 64 * 1024 * 1024
MIN_PREVIOUS_DATABASE_SIZE_RATIO = 0.5
MIN_PREVIOUS_KEY_ROW_RATIO = 0.9
BUILDING_STALE_SECONDS = 6 * 60 * 60


def _checkpoint_database(db_path: Path) -> None:
    delay = CHECKPOINT_RETRY_BACKOFF_SECONDS
    for attempt in range(1, CHECKPOINT_RETRY_ATTEMPTS + 1):
        try:
            conn = duckdb.connect(str(db_path))
            try:
                conn.execute("CHECKPOINT")
            finally:
                conn.close()
            return
        except duckdb.IOException:
            if attempt == CHECKPOINT_RETRY_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_manifest(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a complete, traversal-safe v1 manifest."""

    if not root.is_dir() or root.is_symlink():
        raise ValueError("backup root must be a real directory")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("manifest.json is missing or unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA:
        raise ValueError("unsupported backup manifest schema")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("backup manifest files must be a non-empty list")

    root_resolved = root.resolve()
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("backup manifest file entry must be an object")
        raw_path = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or raw_path == "manifest.json"
        ):
            raise ValueError("backup manifest contains an invalid path")
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("backup manifest contains an unsafe path")
        canonical = relative.as_posix()
        if canonical != raw_path or canonical in seen:
            raise ValueError("backup manifest contains duplicate or non-canonical paths")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("backup manifest contains an invalid size")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError("backup manifest contains an invalid SHA-256")
        target = root.joinpath(*relative.parts)
        try:
            target.resolve(strict=False).relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError("backup manifest path escapes backup root") from exc
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("backup manifest path contains a symlink")
        seen.add(canonical)
        normalized.append({"path": canonical, "size": size, "sha256": digest})
    if "finance.duckdb" not in seen:
        raise ValueError("backup manifest does not contain finance.duckdb")

    all_paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in all_paths):
        raise ValueError("backup contains an untrusted symlink")
    actual = {
        path.relative_to(root).as_posix()
        for path in all_paths
        if path.is_file() and path != manifest_path
    }
    if actual != seen:
        raise ValueError("backup file set does not match manifest")
    return manifest, normalized


def _database_inventory(path: Path) -> dict[str, Any]:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            ).fetchall()
        ]
        key_counts = {
            name: conn.execute(
                f'SELECT COUNT(*) FROM "{name}"'
            ).fetchone()[0]
            for name in KEY_TABLES
            if name in table_names
        }
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {
        "database_size_bytes": path.stat().st_size,
        "table_count": len(table_names),
        "table_names_sha256": hashlib.sha256(
            "\n".join(table_names).encode("utf-8")
        ).hexdigest(),
        "key_table_counts": key_counts,
    }


def _validate_database_inventory(
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> None:
    """Reject uninitialised or catastrophically smaller database snapshots."""

    size = int(current.get("database_size_bytes", 0))
    table_count = int(current.get("table_count", 0))
    if size < MIN_DATABASE_BYTES:
        raise RuntimeError("database is below the minimum safe backup size")
    if table_count <= 0:
        raise RuntimeError("database contains no base tables")
    current_counts = current.get("key_table_counts", {})
    missing_key_tables = sorted(set(KEY_TABLES) - set(current_counts))
    if missing_key_tables:
        raise RuntimeError(
            "database is missing key tables: " + ", ".join(missing_key_tables)
        )
    if previous is None:
        return

    previous_size = int(previous.get("database_size_bytes", 0))
    if previous_size and size < previous_size * MIN_PREVIOUS_DATABASE_SIZE_RATIO:
        raise RuntimeError("database size fell below the previous backup baseline")
    previous_counts = previous.get("key_table_counts", {})
    for table, previous_count in previous_counts.items():
        if table not in current_counts:
            raise RuntimeError(f'key table "{table}" is missing')
        if not isinstance(previous_count, int) or previous_count <= 0:
            continue
        current_count = current_counts.get(table)
        minimum = max(1, math.ceil(previous_count * MIN_PREVIOUS_KEY_ROW_RATIO))
        if not isinstance(current_count, int) or current_count < minimum:
            raise RuntimeError(
                f'key table "{table}" fell below the previous backup baseline'
            )


def _source_size_bytes(root: Path, report_root: str | Path | None) -> int:
    total = 0
    sources = [
        *(root / name for name in IMMUTABLE_ARCHIVE_DIRS),
        *(root / name for name in DURABLE_STATE_DIRS + PRIVATE_DATA_DIRS),
        *(root / name for name in PRIVATE_DATA_FILES),
    ]
    if report_root is not None:
        sources.append(Path(report_root))
    for source in sources:
        if not source.exists():
            continue
        if source.is_symlink():
            raise ValueError(f"backup source contains a symlink: {source}")
        files = [source] if source.is_file() else _regular_files(source)
        total += sum(path.stat().st_size for path in files)
    return total


def _write_restore_evidence(
    evidence_root: Path,
    backup_name: str,
    payload: dict[str, Any],
) -> Path:
    evidence_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = evidence_root / f"{backup_name}_{stamp}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    path.chmod(0o640)
    return path


def _regular_files(root: Path) -> list[Path]:
    paths = sorted(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError(f"backup source contains a symlink: {root}")
    return [path for path in paths if path.is_file()]


def create_backup(
    *,
    data_root: str | Path | None = None,
    backup_root: str | Path | None = None,
    report_root: str | Path | None = None,
) -> dict[str, Any]:
    root = resolve_data_root(data_root)
    db_path = Path(os.getenv("DUCKDB_PATH", "") or root / "finance.duckdb")
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    destination_root = Path(backup_root or root / "backups")
    destination_root.mkdir(parents=True, mode=0o750, exist_ok=True)
    cutoff = time.time() - BUILDING_STALE_SECONDS
    for building in destination_root.glob(".*.building"):
        if (
            building.is_dir()
            and not building.is_symlink()
            and building.stat().st_mtime < cutoff
        ):
            shutil.rmtree(building)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_dir = destination_root / stamp
    temp_dir = destination_root / f".{stamp}.building"
    if final_dir.exists() or temp_dir.exists():
        raise FileExistsError(final_dir)

    previous_dir = None
    previous_inventory = None
    for path in sorted(destination_root.iterdir(), reverse=True):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if verify_backup(path)["status"] != "VALID":
            continue
        previous_dir = path
        previous_inventory = _database_inventory(path / "finance.duckdb")
        break
    previous_items: dict[str, dict[str, Any]] = {}
    if previous_dir is not None:
        try:
            previous_manifest = json.loads(
                (previous_dir / "manifest.json").read_text(encoding="utf-8")
            )
            previous_items = {
                item["path"]: item
                for item in previous_manifest.get("files", [])
                if isinstance(item, dict) and item.get("path")
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            previous_dir = None
            previous_inventory = None
            previous_items = {}

    required_bytes = (
        db_path.stat().st_size
        + _source_size_bytes(root, report_root)
        + MIN_FREE_SPACE_RESERVE_BYTES
    )
    free_bytes = shutil.disk_usage(destination_root).free
    if free_bytes < required_bytes:
        raise OSError(
            f"insufficient backup space: required={required_bytes}, free={free_bytes}"
        )
    sources: list[tuple[Path, str, bool]] = []
    for name in IMMUTABLE_ARCHIVE_DIRS:
        sources.append((root / name, name, True))
    for name in DURABLE_STATE_DIRS + PRIVATE_DATA_DIRS:
        sources.append((root / name, name, False))
    for name in PRIVATE_DATA_FILES:
        source = root / name
        if source.exists():
            sources.append((source, name, False))
    if report_root is not None:
        sources.append((Path(report_root), "reports", False))

    temp_dir.mkdir(parents=True, mode=0o750)
    try:
        # 先 checkpoint 并关闭连接，再复制单文件数据库，避免复制未合并的 WAL 状态。
        _checkpoint_database(db_path)
        current_inventory = _database_inventory(db_path)
        _validate_database_inventory(current_inventory, previous_inventory)
        shutil.copy2(db_path, temp_dir / "finance.duckdb")
        deduplicated_files = 0
        copied_bytes = (temp_dir / "finance.duckdb").stat().st_size
        for source, namespace, allow_deduplication in sources:
            if not source.exists():
                continue
            if source.is_symlink():
                raise ValueError(f"backup source contains a symlink: {source}")
            source_files = [source] if source.is_file() else _regular_files(source)
            for source_path in source_files:
                relpath = (
                    namespace
                    if source.is_file()
                    else f"{namespace}/{source_path.relative_to(source).as_posix()}"
                )
                destination = temp_dir / relpath
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                size = source_path.stat().st_size
                digest = _sha256(source_path)
                previous_item = previous_items.get(relpath)
                previous_path = previous_dir / relpath if previous_dir else None
                if (
                    allow_deduplication
                    and previous_item
                    and previous_item.get("size") == size
                    and previous_item.get("sha256") == digest
                    and previous_path is not None
                    and previous_path.is_file()
                ):
                    try:
                        os.link(previous_path, destination)
                        deduplicated_files += 1
                        continue
                    except OSError:
                        pass
                shutil.copy2(source_path, destination)
                copied_bytes += size

        files = []
        for path in sorted(p for p in temp_dir.rglob("*") if p.is_file()):
            files.append(
                {
                    "path": path.relative_to(temp_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        manifest = {
            "schema_version": BACKUP_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_data_root": "FOUNDF_DATA_ROOT",
            "database_inventory": current_inventory,
            "layers": {
                "database": ["finance.duckdb"],
                "immutable_archives": [f"{name}/" for name in IMMUTABLE_ARCHIVE_DIRS],
                "durable_governance": [f"{name}/" for name in DURABLE_STATE_DIRS],
                "private_local_inputs": [
                    *PRIVATE_DATA_FILES,
                    *(f"{name}/" for name in PRIVATE_DATA_DIRS),
                ],
                "reports": ["reports/"] if report_root is not None else [],
                "excluded": [".secrets/", "backups/", "runtime/"],
            },
            "files": files,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for path in temp_dir.rglob("*"):
            path.chmod(0o750 if path.is_dir() else 0o640)
        temp_dir.chmod(0o750)
        os.replace(temp_dir, final_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return {
        "status": "CREATED",
        "backup_path": str(final_dir),
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "copied_bytes": copied_bytes,
        "deduplicated_files": deduplicated_files,
    }


def verify_backup(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    try:
        manifest, files = _safe_manifest(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "INVALID",
            "failures": [
                {"path": "manifest.json", "reason": "MANIFEST_INVALID"}
            ],
            "table_count": None,
            "file_count": 0,
            "manifest_sha256": (
                _sha256(root / "manifest.json")
                if (root / "manifest.json").is_file()
                else None
            ),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    failures = []
    for item in files:
        target = root / item["path"]
        if not target.is_file():
            failures.append({"path": item["path"], "reason": "MISSING"})
        elif target.stat().st_size != item["size"]:
            failures.append({"path": item["path"], "reason": "SIZE_MISMATCH"})
        elif _sha256(target) != item["sha256"]:
            failures.append({"path": item["path"], "reason": "HASH_MISMATCH"})
    db_path = root / "finance.duckdb"
    table_count = None
    if not failures and db_path.exists():
        try:
            inventory = _database_inventory(db_path)
            table_count = inventory["table_count"]
            _validate_database_inventory(inventory)
            expected_inventory = manifest.get("database_inventory")
            if expected_inventory is not None and inventory != expected_inventory:
                failures.append(
                    {"path": "finance.duckdb", "reason": "INVENTORY_MISMATCH"}
                )
        except (duckdb.Error, OSError, RuntimeError):
            failures.append(
                {"path": "finance.duckdb", "reason": "DATABASE_READ_FAILED"}
            )
    return {
        "status": "VALID" if not failures else "INVALID",
        "failures": failures,
        "table_count": table_count,
        "file_count": len(files),
        "manifest_sha256": _sha256(root / "manifest.json"),
    }


def run_restore_drill(
    backup_path: str | Path,
    *,
    data_root: str | Path | None = None,
    drill_root: str | Path | None = None,
    evidence_root: str | Path | None = None,
) -> dict[str, Any]:
    """Copy a backup into an isolated directory and prove it is readable.

    The production data root and backup are never modified. Restored material
    is removed after the drill; only a credential-free evidence record remains.
    """

    backup = Path(backup_path).resolve()
    if data_root is not None:
        root = resolve_data_root(data_root)
    elif backup.parent.name == "backups":
        root = backup.parent.parent
    else:
        raise ValueError("data_root is required outside a standard backups directory")
    evidence_dir = Path(evidence_root or root / "governance" / "restore_drills")
    base_dir = Path(drill_root) if drill_root is not None else None
    if base_dir is not None:
        base_dir.mkdir(parents=True, exist_ok=True)

    verification = verify_backup(backup)
    completed_at = datetime.now(timezone.utc).isoformat()
    base_payload: dict[str, Any] = {
        "schema_version": RESTORE_DRILL_SCHEMA,
        "completed_at": completed_at,
        "backup_path": (
            backup.relative_to(root.resolve()).as_posix()
            if backup.is_relative_to(root.resolve())
            else backup.name
        ),
        "backup_manifest_sha256": verification.get("manifest_sha256"),
        "backup_verification": verification,
        "production_data_modified": False,
        "restored_material_retained": False,
    }
    if verification["status"] != "VALID":
        payload = {
            **base_payload,
            "status": "FAILED",
            "failure_code": "BACKUP_INVALID",
        }
        evidence = _write_restore_evidence(evidence_dir, backup.name, payload)
        return {**payload, "evidence_path": str(evidence)}

    manifest, files = _safe_manifest(backup)
    temporary = Path(
        tempfile.mkdtemp(prefix="foundf-restore-drill-", dir=base_dir)
    )
    try:
        restored = temporary / "data"
        restored.mkdir()
        copied_bytes = 0
        for item in files:
            source = backup / item["path"]
            destination = restored / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_bytes += item["size"]
        shutil.copy2(backup / "manifest.json", restored / "manifest.json")

        restored_verification = verify_backup(restored)
        if restored_verification["status"] != "VALID":
            raise RuntimeError("restored file verification failed")
        source_inventory = _database_inventory(backup / "finance.duckdb")
        restored_inventory = _database_inventory(restored / "finance.duckdb")
        if source_inventory != restored_inventory:
            raise RuntimeError("restored database inventory differs from backup")
        payload = {
            **base_payload,
            "status": "PASSED",
            "backup_created_at": manifest.get("created_at"),
            "file_count": len(files),
            "copied_bytes": copied_bytes,
            "restored_verification": restored_verification,
            "database_inventory": restored_inventory,
        }
    except (OSError, ValueError, RuntimeError, duckdb.Error) as exc:
        payload = {
            **base_payload,
            "status": "FAILED",
            "failure_code": "RESTORE_VALIDATION_FAILED",
            "error_type": type(exc).__name__,
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)

    evidence = _write_restore_evidence(evidence_dir, backup.name, payload)
    return {**payload, "evidence_path": str(evidence)}


def main() -> None:
    parser = argparse.ArgumentParser(description="FoundF NAS 数据备份")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--data-root")
    create.add_argument("--backup-root")
    create.add_argument("--report-root")
    verify = sub.add_parser("verify")
    verify.add_argument("path")
    drill = sub.add_parser("restore-drill")
    drill.add_argument("path")
    drill.add_argument("--data-root")
    drill.add_argument("--drill-root")
    drill.add_argument("--evidence-root")
    args = parser.parse_args()
    if args.command == "create":
        result = create_backup(
            data_root=args.data_root,
            backup_root=args.backup_root,
            report_root=args.report_root,
        )
    elif args.command == "verify":
        result = verify_backup(args.path)
    else:
        result = run_restore_drill(
            args.path,
            data_root=args.data_root,
            drill_root=args.drill_root,
            evidence_root=args.evidence_root,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
