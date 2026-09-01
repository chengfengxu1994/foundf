#!/usr/bin/env python3
"""quote_daemon 看门（系统 cron 自治，不依赖 Kimi 会话）。

- 进程不在 → 拉起；
- 工作日采集窗口内（09:20-15:10 CST）快照停滞超过 STALE_MIN 分钟 → 重启；
- 窗口外/非工作日 → 只确认进程存活（空转属正常，节假日停滞不重启）。
- 重启前必须确认所有旧进程已退出；确认失败时不启动第二个写进程。

用法:
  python3 deploy/quote_watchdog.py --once
  python3 deploy/quote_watchdog.py --daemon --interval 60

09:17 单次调用只负责当时的一次检查，不能发现后续盘中停滞。系统 cron
应在 09:17 使用 ``--daemon``；进程锁会拒绝重复的看门实例。
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))
LOG = ROOT / "data/runtime/quote_daemon.log"
LOCK = ROOT / "data/runtime/quote_watchdog.lock"
STALE_MIN = 10
MONITOR_START = dtime(9, 17)
MONITOR_END = dtime(15, 10)
STOP_TIMEOUT_SECONDS = 10.0


def running_pids() -> list[int]:
    completed = subprocess.run(
        ["pgrep", "-fa", "quote_daemon.py"], capture_output=True, text=True,
        check=False,
    )
    pids = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        # Require a Python executable plus an exact quote_daemon.py argument.
        # This excludes the pgrep command and quote_watchdog.py itself.
        if len(parts) < 3 or not Path(parts[1]).name.startswith("python"):
            continue
        if not any(Path(arg).name == "quote_daemon.py" for arg in parts[2:]):
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            continue
    return pids


def start_daemon() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        process = subprocess.Popen(
            [sys.executable, "deploy/quote_daemon.py", "--interval", "60"],
            cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    return process.pid


def in_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 20) <= t <= dtime(15, 10)


def last_snapshot_ts():
    import duckdb  # 局部导入, 看门其余路径不依赖 DB
    con = duckdb.connect(str(ROOT / "data/finance.duckdb"), read_only=True)
    try:
        return con.execute("SELECT max(ts) FROM cn_quote_snapshot").fetchone()[0]
    finally:
        con.close()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_and_confirm(pids: list[int], timeout: float = STOP_TIMEOUT_SECONDS) -> list[int]:
    """SIGTERM old writers and return any PIDs still alive at the deadline."""

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    remaining = [pid for pid in pids if _pid_exists(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        remaining = [pid for pid in remaining if _pid_exists(pid)]
    return remaining


def _normalise_snapshot_ts(ts: datetime) -> datetime:
    # TIMESTAMPTZ is expected. Be explicit and conservative for legacy/fixture
    # databases that return a naive value: snapshot_once writes UTC timestamps.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(CST)


def check_once(*, now: datetime | None = None, allow_restart: bool = True) -> int:
    now = now or datetime.now(CST)
    pids = running_pids()
    if not pids:
        pid = start_daemon()
        print(f"[{now.isoformat()}] quote_daemon 未运行, 已拉起(pid {pid})")
        return 0
    if len(pids) > 1:
        if not allow_restart:
            print(f"[{now.isoformat()}] 检测到多个写进程{pids}, 冷却期内不重启")
            return 2
        remaining = stop_and_confirm(pids)
        if remaining:
            print(f"[{now.isoformat()}] 多写进程停止失败, 仍存活{remaining}; 拒绝拉起新进程")
            return 2
        pid = start_daemon()
        print(f"[{now.isoformat()}] 已停止重复写进程{pids}, 单实例拉起(pid {pid})")
        return 0
    if not in_window(now):
        print(f"[{now.isoformat()}] 进程存活(pid {pids[0]}), 窗口外空转正常")
        return 0
    try:
        ts = last_snapshot_ts()
    except Exception as exc:
        # An observer DB error is not evidence that the writer is stale. Do not
        # risk creating another writer when the existing process is alive.
        print(f"[{now.isoformat()}] 无法读取快照时间({type(exc).__name__}: {exc}), 保留现有进程")
        return 3
    stale = ts is None or (now - _normalise_snapshot_ts(ts)) > timedelta(minutes=STALE_MIN)
    if not stale:
        print(f"[{now.isoformat()}] 进程存活, 快照新鲜(max {ts})")
        return 0
    if not allow_restart:
        print(f"[{now.isoformat()}] 快照停滞(max {ts}), 重启冷却中")
        return 1
    remaining = stop_and_confirm(pids)
    if remaining:
        print(f"[{now.isoformat()}] 快照停滞但旧进程仍存活{remaining}; 拒绝拉起新进程")
        return 2
    pid = start_daemon()
    print(f"[{now.isoformat()}] 快照停滞(max {ts}), 已确认旧进程退出并重启(pid {pid})")
    return 0


def _monitoring_period(now: datetime) -> bool:
    return now.weekday() < 5 and MONITOR_START <= now.time() <= MONITOR_END


def monitor(interval: int) -> int:
    """Monitor throughout the trading window, with a restart cooldown."""

    last_restart: datetime | None = None
    worst_exit = 0
    while True:
        now = datetime.now(CST)
        if not _monitoring_period(now):
            return worst_exit
        allow_restart = last_restart is None or now - last_restart >= timedelta(minutes=STALE_MIN)
        before = set(running_pids())
        code = check_once(now=now, allow_restart=allow_restart)
        after = set(running_pids())
        if code == 0 and before != after:
            last_restart = now
        worst_exit = max(worst_exit, code)
        time.sleep(interval)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="执行一次检查后退出（默认）")
    mode.add_argument("--daemon", action="store_true", help="从09:17持续监控至15:10")
    parser.add_argument("--interval", type=int, default=60, help="daemon 检查间隔秒")
    args = parser.parse_args(argv)
    if args.interval < 15:
        parser.error("--interval must be at least 15 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("quote_watchdog 已有实例运行, 本次拒绝重复启动")
            return 4
        if args.daemon:
            return monitor(args.interval)
        return check_once()


if __name__ == "__main__":
    sys.exit(main())
