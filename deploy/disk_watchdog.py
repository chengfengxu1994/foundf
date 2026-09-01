#!/usr/bin/env python3
"""Disk-capacity watchdog and reusable fail-closed write preflight.

The check is read-only: it never removes or rotates files. Every invocation
prints JSON and atomically replaces a machine-readable status file. Exit codes:
0=OK, 1=WARNING, 2=CRITICAL, 3=check/status-file error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATUS_FILE = ROOT / "data/runtime/disk_status.json"

CHECKS = {
    "/srv": "项目库/备份/原始数据所在盘",
    "/": "系统盘（日志/临时文件）",
}

WARN_FREE_GB = 30
CRIT_FREE_GB = 15
EXIT_CODES = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "ERROR": 3}
_SEVERITY = {name: code for name, code in EXIT_CODES.items()}


class DiskSpacePreflightError(RuntimeError):
    """Raised when a write preflight reaches its configured blocking level."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(f"disk write preflight blocked: {result['status']}")


def inspect_filesystems(
    checks: Mapping[str, str] = CHECKS,
    *,
    disk_usage: Callable = shutil.disk_usage,
    now: datetime | None = None,
) -> dict:
    """Return a serialisable capacity snapshot without changing disk state."""

    timestamp = now or datetime.now(timezone.utc)
    worst = "OK"
    filesystems = []
    for path, note in checks.items():
        try:
            usage = disk_usage(path)
            free_gb = usage.free / 1e9
            used_pct = usage.used / usage.total * 100 if usage.total else 100.0
            if free_gb < CRIT_FREE_GB:
                level = "CRITICAL"
            elif free_gb < WARN_FREE_GB:
                level = "WARNING"
            else:
                level = "OK"
            row = {
                "path": path,
                "note": note,
                "free_gb": round(free_gb, 1),
                "used_pct": round(used_pct, 1),
                "level": level,
            }
        except Exception as exc:
            level = "ERROR"
            row = {
                "path": path,
                "note": note,
                "free_gb": None,
                "used_pct": None,
                "level": level,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if _SEVERITY[level] > _SEVERITY[worst]:
            worst = level
        filesystems.append(row)
    return {
        "schema_version": 1,
        "ts": timestamp.astimezone(timezone.utc).isoformat(),
        "status": worst,
        "exit_code": EXIT_CODES[worst],
        "thresholds_gb": {"warning": WARN_FREE_GB, "critical": CRIT_FREE_GB},
        "filesystems": filesystems,
    }


def write_status_atomic(result: dict, status_file: Path = DEFAULT_STATUS_FILE) -> None:
    """Durably replace ``status_file``; readers never observe partial JSON."""

    status_file = Path(status_file)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=status_file.parent,
            prefix=f".{status_file.name}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(result, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, status_file)
        tmp_name = None
        try:
            directory_fd = os.open(status_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def run_check(
    *,
    checks: Mapping[str, str] = CHECKS,
    status_file: Path = DEFAULT_STATUS_FILE,
    disk_usage: Callable = shutil.disk_usage,
    now: datetime | None = None,
) -> dict:
    result = inspect_filesystems(checks, disk_usage=disk_usage, now=now)
    write_status_atomic(result, status_file)
    return result


def require_disk_capacity(
    *,
    checks: Mapping[str, str] = CHECKS,
    status_file: Path = DEFAULT_STATUS_FILE,
    block_on_warning: bool = True,
    disk_usage: Callable = shutil.disk_usage,
) -> dict:
    """Write-job preflight; record status, then fail closed at the chosen level.

    Essential jobs may use ``block_on_warning=False`` to continue at WARNING;
    CRITICAL and ERROR always block. The exception exposes the full ``result``.
    """

    result = run_check(checks=checks, status_file=status_file, disk_usage=disk_usage)
    blocking = {"WARNING", "CRITICAL", "ERROR"} if block_on_warning else {"CRITICAL", "ERROR"}
    if result["status"] in blocking:
        raise DiskSpacePreflightError(result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_check(status_file=args.status_file)
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "ERROR",
            "exit_code": EXIT_CODES["ERROR"],
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False), file=sys.stderr, flush=True)
        return EXIT_CODES["ERROR"]
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
