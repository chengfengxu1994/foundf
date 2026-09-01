"""Fail-closed NAS runtime scheduler for FoundF maintenance tasks."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .event_store import resolve_data_root


TASKS = {"collect", "daily", "review", "backup"}
STATE_SCHEMA = "foundf.runtime_scheduler.v1"
UNHEALTHY_TASK_STATUSES = {"FAILED", "PARTIAL", "LOCK_BUSY"}
RETRYABLE_TASK_STATUSES = {"FAILED", "PARTIAL", "LOCK_BUSY"}
MAX_DAILY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 30 * 60


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def runtime_write_lock(
    data_root: str | Path | None = None,
) -> Iterator[bool]:
    """Acquire the shared FoundF writer lock without blocking.

    Host cron writers can reuse this context manager and must stop when the
    yielded value is false. The stable lock path remains under ``data/runtime``.
    """

    root = resolve_data_root(data_root)
    lock_path = root / "runtime" / "foundf-runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Backward-compatible private name for existing imports.
_exclusive_runtime_lock = runtime_write_lock


def _summarize_collection(result: dict[str, Any]) -> dict[str, Any]:
    providers = result.get("providers", {})
    failed = []
    failure_reasons: dict[str, dict[str, int]] = {}
    failed_symbol_samples: dict[str, list[str]] = {}
    for name, item in providers.items():
        if not isinstance(item, dict):
            failed.append(name)
        elif (
            item.get("error")
            or item.get("failed_symbols")
            or item.get("basic_failures")
            or item.get("index_error")
        ):
            failed.append(name)
            reasons: dict[str, int] = {}
            samples: list[str] = []
            for failure in (
                list(item.get("failed_symbols", []))
                + list(item.get("basic_failures", []))
            ):
                if not isinstance(failure, dict):
                    continue
                reason = str(failure.get("reason") or "UNKNOWN")
                reasons[reason] = reasons.get(reason, 0) + 1
                symbol = str(failure.get("symbol") or "")
                if symbol and len(samples) < 5:
                    samples.append(symbol)
            if item.get("error"):
                reasons[str(item["error"])] = reasons.get(
                    str(item["error"]), 0
                ) + 1
            if item.get("index_error"):
                reason = f"INDEX_{item['index_error']}"
                reasons[reason] = reasons.get(reason, 0) + 1
            if reasons:
                failure_reasons[name] = reasons
            if samples:
                failed_symbol_samples[name] = samples
    return {
        "date": result.get("date"),
        "total_prices": result.get("total_prices", 0),
        "providers": sorted(providers),
        "failed_providers": sorted(failed),
        "failure_reasons": failure_reasons,
        "failed_symbol_samples": failed_symbol_samples,
    }


def execute_task(task: str, *, data_root: Path) -> dict[str, Any]:
    """Execute one task and return a credential-free operational summary."""

    if task == "collect":
        from data_provider.scheduler import CollectorScheduler

        scheduler = CollectorScheduler(
            duckdb_path=data_root / "finance.duckdb",
            raw_base=data_root / "raw",
        )
        try:
            result = scheduler.run_daily()
        finally:
            scheduler.close()
        summary = _summarize_collection(result)
        summary["status"] = (
            "PARTIAL" if summary["failed_providers"] else "COMPLETED"
        )
        return summary

    if task == "daily":
        from portfolio_manager.daily_run import run

        run()
        log_path = data_root / "daily_run_log.json"
        rows = json.loads(log_path.read_text(encoding="utf-8"))
        latest = rows[-1]
        failed_steps = sorted(
            name
            for name, status in latest.get("steps", {}).items()
            if status != "ok"
        )
        from .health import inspect_data_assets

        health = inspect_data_assets(data_root=data_root)
        return {
            "status": "PARTIAL" if failed_steps else "COMPLETED",
            "failed_steps": failed_steps,
            "data_asset_status": health["status"],
            "decision_data_ready": health["decision_data_ready"],
            "blockers": health["blockers"],
        }

    if task == "backup":
        from .backup import create_backup, verify_backup

        report_root = os.getenv("FOUNDF_REPORT_ROOT")
        created = create_backup(
            data_root=data_root,
            report_root=report_root or None,
        )
        verification = verify_backup(created["backup_path"])
        if verification["status"] != "VALID":
            raise RuntimeError("new backup failed verification")
        return {
            **created,
            "creation_status": created["status"],
            "status": "COMPLETED",
            "verification": verification["status"],
        }

    if task == "review":
        from portfolio_ai.daily import DailyPortfolioIntelligence

        intelligence = DailyPortfolioIntelligence(
            duckdb_path=data_root / "finance.duckdb",
            report_dir=Path("reports"),
            data_root=data_root,
            config_root=Path("config"),
            investor_profile_path=Path(".secrets/investor_profile.json"),
        )
        report = intelligence.generate()
        output = intelligence.save(report)
        return {
            "status": "COMPLETED",
            "review_status": report["status"],
            "report_path": str(output),
            "blockers": report["review_gate"]["blockers"],
            "llm_called": False,
            "automatic_trade_allowed": False,
        }

    raise ValueError(f"unsupported task: {task}")


def run_once(task: str, *, data_root: str | Path | None = None) -> dict[str, Any]:
    root = resolve_data_root(data_root)
    state_path = root / "runtime" / f"{task}.json"
    started = datetime.now(timezone.utc)
    with _exclusive_runtime_lock(root) as acquired:
        if not acquired:
            state = {
                "schema_version": STATE_SCHEMA,
                "task": task,
                "status": "LOCK_BUSY",
                "heartbeat_at": started.isoformat(),
            }
            _atomic_json(state_path, state)
            return state
        try:
            result = execute_task(task, data_root=root)
            status = result.get("status", "COMPLETED")
            state = {
                "schema_version": STATE_SCHEMA,
                "task": task,
                "status": status,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            }
        except Exception as exc:
            state = {
                "schema_version": STATE_SCHEMA,
                "task": task,
                "status": "FAILED",
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
            }
        _atomic_json(state_path, state)
        return state


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("schedule time must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def _next_run(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def run_loop(
    task: str,
    *,
    schedule_time: str,
    timezone_name: str,
    poll_seconds: int,
    data_root: str | Path | None = None,
    automation_enabled: bool = False,
) -> None:
    root = resolve_data_root(data_root)
    state_path = root / "runtime" / f"{task}.json"
    hour, minute = _parse_time(schedule_time)
    zone = ZoneInfo(timezone_name)
    last_run_date: str | None = None
    retry_attempts = 0
    retry_not_before: datetime | None = None
    retry_date: str | None = None
    # 从持久化状态恢复 last_run_date：容器/进程重启发生在当日计划时刻之后时，
    # 不得因内存态丢失而立即补跑一轮（2026-08-14 事故：collector recreate
    # 后连跑两轮冗余 collect，第二轮 baostock 会话挂起占住 DuckDB 写锁，
    # 差点撞 19:26 候选生成窗口）。
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            finished = prev.get("finished_at")
            if finished:
                finished_dt = datetime.fromisoformat(str(finished))
                if finished_dt.tzinfo is None:
                    finished_dt = finished_dt.replace(tzinfo=timezone.utc)
                finished_local = finished_dt.astimezone(zone)
                if prev.get("status") in RETRYABLE_TASK_STATUSES:
                    if finished_local.date() == datetime.now(zone).date():
                        retry_date = finished_local.date().isoformat()
                        retry_attempts = max(1, int(prev.get("retry_attempts", 1)))
                        raw_retry_at = prev.get("next_retry_at")
                        if raw_retry_at:
                            retry_not_before = datetime.fromisoformat(str(raw_retry_at))
                            if retry_not_before.tzinfo is None:
                                retry_not_before = retry_not_before.replace(tzinfo=zone)
                            retry_not_before = retry_not_before.astimezone(zone)
                        else:
                            retry_not_before = finished_local + timedelta(
                                seconds=RETRY_BACKOFF_SECONDS
                            )
                        if retry_attempts >= MAX_DAILY_ATTEMPTS:
                            last_run_date = finished_local.date().isoformat()
                            retry_not_before = None
                    else:
                        last_run_date = finished_local.date().isoformat()
                else:
                    last_run_date = finished_local.date().isoformat()
        except (OSError, json.JSONDecodeError, ValueError):
            last_run_date = None
    while True:
        now = datetime.now(zone)
        today = now.date().isoformat()
        if retry_date is not None and retry_date != today:
            retry_attempts = 0
            retry_not_before = None
            retry_date = None
        due = now.hour > hour or (now.hour == hour and now.minute >= minute)
        retry_due = retry_not_before is None or now >= retry_not_before
        if (
            automation_enabled
            and due
            and retry_due
            and last_run_date != now.date().isoformat()
        ):
            state = run_once(task, data_root=root)
            if state["status"] in RETRYABLE_TASK_STATUSES:
                retry_date = today
                retry_attempts += 1
                retry_not_before = now + timedelta(
                    seconds=RETRY_BACKOFF_SECONDS * retry_attempts
                )
                retry_exhausted = retry_attempts >= MAX_DAILY_ATTEMPTS
                if retry_exhausted:
                    retry_not_before = None
                    last_run_date = today
                state.update(
                    {
                        "automation_enabled": automation_enabled,
                        "retry_attempts": retry_attempts,
                        "next_retry_at": (
                            retry_not_before.isoformat()
                            if retry_not_before is not None else None
                        ),
                        "next_run_at": (
                            retry_not_before.isoformat()
                            if retry_not_before is not None
                            else _next_run(now, hour, minute).isoformat()
                        ),
                    }
                )
                _atomic_json(state_path, state)
            elif state["status"] != "LOCK_BUSY":
                last_run_date = now.date().isoformat()
                retry_attempts = 0
                retry_not_before = None
        else:
            current = {}
            if state_path.exists():
                try:
                    current = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = {}
            previous_status = current.get("status", "WAITING")
            if not automation_enabled:
                status = "DISABLED"
            elif previous_status == "DISABLED":
                # 由禁用切换到启用后，不沿用旧的 DISABLED 状态。
                status = "WAITING"
            else:
                status = previous_status
            if (
                retry_attempts >= MAX_DAILY_ATTEMPTS
                and last_run_date == today
            ):
                # Migrate stale states written by older schedulers: once the
                # daily retry budget is exhausted there is no pending retry.
                current["next_retry_at"] = None
            current.update(
                {
                    "schema_version": STATE_SCHEMA,
                    "task": task,
                    "status": status,
                    "automation_enabled": automation_enabled,
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "schedule_time": schedule_time,
                    "timezone": timezone_name,
                    "next_run_at": (
                        retry_not_before.isoformat()
                        if automation_enabled
                        and last_run_date != now.date().isoformat()
                        and retry_not_before is not None
                        else _next_run(now, hour, minute).isoformat()
                    ),
                }
            )
            _atomic_json(state_path, current)
        time.sleep(poll_seconds)


def health(
    task: str,
    *,
    data_root: str | Path | None = None,
    max_age_seconds: int = 180,
) -> dict[str, Any]:
    root = resolve_data_root(data_root)
    path = root / "runtime" / f"{task}.json"
    if not path.exists():
        return {"status": "UNHEALTHY", "reason": "STATE_MISSING"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(state["heartbeat_at"])
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return {"status": "UNHEALTHY", "reason": "STATE_INVALID"}
    scheduler_status = state.get("status")
    status_is_healthy = scheduler_status not in UNHEALTHY_TASK_STATUSES
    return {
        "status": (
            "HEALTHY"
            if age <= max_age_seconds and status_is_healthy
            else "UNHEALTHY"
        ),
        "reason": (
            "TASK_NOT_SUCCESSFUL"
            if age <= max_age_seconds and not status_is_healthy
            else ("HEARTBEAT_STALE" if age > max_age_seconds else None)
        ),
        "task": task,
        "scheduler_status": scheduler_status,
        "heartbeat_age_seconds": round(max(0.0, age), 1),
    }


def load_runtime_status(
    *,
    data_root: str | Path | None = None,
    max_age_seconds: int = 180,
) -> dict[str, Any]:
    """Return a read-only, credential-free projection for API/Dashboard."""

    root = resolve_data_root(data_root)
    tasks = []
    for task in sorted(TASKS):
        state_path = root / "runtime" / f"{task}.json"
        task_health = health(
            task, data_root=root, max_age_seconds=max_age_seconds
        )
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        tasks.append(
            {
                "task": task,
                "health": task_health["status"],
                "scheduler_status": state.get(
                    "status", task_health.get("reason", "STATE_MISSING")
                ),
                "automation_enabled": state.get("automation_enabled") is True,
                "schedule_time": state.get("schedule_time"),
                "timezone": state.get("timezone"),
                "next_run_at": state.get("next_run_at"),
                "last_started_at": state.get("started_at"),
                "last_finished_at": state.get("finished_at"),
                "heartbeat_at": state.get("heartbeat_at"),
            }
        )
    if all(item["scheduler_status"] == "STATE_MISSING" for item in tasks):
        overall = "UNAVAILABLE"
    elif any(item["health"] != "HEALTHY" for item in tasks):
        overall = "DEGRADED"
    elif all(not item["automation_enabled"] for item in tasks):
        overall = "DISABLED"
    elif any(item["scheduler_status"] == "FAILED" for item in tasks):
        overall = "ATTENTION"
    else:
        overall = "ACTIVE"
    return {
        "schema_version": "foundf.runtime_automation_projection.v1",
        "status": overall,
        "tasks": tasks,
        "shared_write_lock": True,
        "automatic_trade_allowed": False,
        "trusted_review_scheduled": any(
            item["task"] == "review" and item["automation_enabled"]
            for item in tasks
        ),
        "ai_review_scheduled": False,
        "disclaimer": (
            "自动化仅负责数据采集、门禁日报、可信复盘上下文和一致性备份；"
            "可信复盘不调用外部 LLM。"
            "不包含券商登录、下单或生产策略自动变更。"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="FoundF NAS runtime scheduler")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("once", "loop", "health"):
        item = sub.add_parser(command)
        item.add_argument("--task", choices=sorted(TASKS), required=True)
        item.add_argument("--data-root")
        if command == "loop":
            item.add_argument("--at", required=True)
            item.add_argument("--timezone", default="Asia/Shanghai")
            item.add_argument("--poll-seconds", type=int, default=30)
        if command == "health":
            item.add_argument("--max-age-seconds", type=int, default=180)
    args = parser.parse_args()
    if args.command == "once":
        result = run_once(args.task, data_root=args.data_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["status"] not in {"FAILED", "LOCK_BUSY"} else 1)
    if args.command == "health":
        result = health(
            args.task,
            data_root=args.data_root,
            max_age_seconds=args.max_age_seconds,
        )
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["status"] == "HEALTHY" else 1)
    if args.poll_seconds < 5:
        parser.error("--poll-seconds must be >= 5")
    run_loop(
        args.task,
        schedule_time=args.at,
        timezone_name=args.timezone,
        poll_seconds=args.poll_seconds,
        data_root=args.data_root,
        automation_enabled=os.getenv(
            "FOUNDF_AUTOMATION_ENABLED", "false"
        ).lower() == "true",
    )


if __name__ == "__main__":
    main()
