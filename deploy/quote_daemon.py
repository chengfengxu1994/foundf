#!/usr/bin/env python3
"""A 股盘中实时快照常驻采集（东方财富 → cn_quote_snapshot）。

交易日 09:15–15:10（Asia/Shanghai）每 ``--interval`` 秒批量抓取一次全池
快照写入 ``cn_quote_snapshot``；非交易时段空转休眠。设计要点：

- 股票池 = ``daily_price`` 里全部 6 位数字代码（与候选生成同一 universe）。
- 幂等：``UNIQUE(symbol,ts)`` + INSERT OR IGNORE，重启重跑不产生重复行。
- 写库窗口与 CollectorScheduler(17:15) 不重叠；冲突时本进程退避重试，
  绝不阻塞主采集链路。
- 快照是未复权原始价，仅服务盘中闸门与复盘，不回写 ``daily_price``。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_provider.providers.eastmoney_provider import fetch_quotes  # noqa: E402
from foundf_db.runtime_scheduler import runtime_write_lock  # noqa: E402
from foundf_db.warehouse import Warehouse  # noqa: E402

CST = timezone(timedelta(hours=8))
SESSION_START = (9, 15)
SESSION_END = (15, 10)


def in_session(now: datetime) -> bool:
    """是否处于采集窗口（工作日 09:15–15:10 北京时间）。"""
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=SESSION_START[0], minute=SESSION_START[1],
                        second=0, microsecond=0)
    end = now.replace(hour=SESSION_END[0], minute=SESSION_END[1],
                      second=0, microsecond=0)
    return start <= now <= end


def universe(db_path: str | Path) -> list[str]:
    with Warehouse(db_path) as warehouse:
        rows = warehouse.query(
            "SELECT DISTINCT symbol FROM daily_price "
            "WHERE length(symbol) = 6 "
            "AND regexp_matches(symbol, '^[0-9]{6}$') ORDER BY symbol"
        )
    return [str(r["symbol"]) for r in rows]


def escape_snapshot(db_path: str | Path, now: datetime,
                    original: Exception) -> None:
    """直连退避耗尽且疑似封禁时，经 proxy_guard 换出口重试一轮快照。

    proxy_guard 未启用/非封禁类错误/逃生故障时直接返回（本轮放弃），
    不改变原有"等下一间隔"的语义。
    """
    try:
        from proxy_guard import (EscapeSession, ProxyExhaustedError,
                                 is_ban_like, load_config)
    except ImportError:
        return
    cfg = load_config()
    if not cfg.enabled or not is_ban_like(original):
        return
    try:
        with EscapeSession(cfg, reason="quote-snapshot") as escape:
            for _ in escape.attempts():
                try:
                    escape.next_egress()
                except ProxyExhaustedError:
                    return
                try:
                    codes = universe(db_path)
                    quotes = fetch_quotes(codes, proxy=escape.proxy_url)
                    if not quotes:
                        raise RuntimeError("代理路径返回空快照")
                    n = snapshot_once_with_quotes(db_path, quotes, now)
                    escape.mark_success()
                    print(f"[{now.isoformat()}] escape snapshot rows={n} "
                          f"via {escape.current_node}", flush=True)
                    return
                except Exception as exc:
                    print(f"[{now.isoformat()}] escape attempt failed: {exc}",
                          flush=True)
                    escape.mark_failure(exc)
    except Exception as exc:
        print(f"[{now.isoformat()}] escape unavailable: {exc}", flush=True)



def snapshot_once(db_path: str | Path, *, now: datetime | None = None) -> int:
    """抓一次全池快照入库，返回写入行数。"""
    now = now or datetime.now(timezone.utc)
    codes = universe(db_path)
    if not codes:
        return 0
    quotes = fetch_quotes(codes)
    return snapshot_once_with_quotes(db_path, quotes, now)


def snapshot_once_with_quotes(db_path: str | Path, quotes: list[dict],
                              now: datetime) -> int:
    """把已抓到的快照写库（逃生路径复用，不重复请求网络）。"""
    ts = now.isoformat()
    rows = [{
        "ts": ts,
        "symbol": q["symbol"],
        "last": q["last"],
        "pct_chg": q["pct_chg"],
        "open": q["open"],
        "high": q["high"],
        "low": q["low"],
        "prev_close": q["prev_close"],
        "volume_hand": q["volume_hand"],
        "amount": q["amount"],
        "source": "eastmoney",
        "fetched_at": ts,
    } for q in quotes]
    if not rows:
        return 0
    data_root = Path(db_path).resolve().parent
    with runtime_write_lock(data_root) as acquired:
        if not acquired:
            raise RuntimeError("FoundF 共享写锁忙，跳过本轮快照")
        with Warehouse(db_path) as warehouse:
            before = warehouse.query(
                "SELECT COUNT(*) AS n FROM cn_quote_snapshot")[0]["n"]
            warehouse.insert("cn_quote_snapshot", rows,
                             conflict_strategy="ignore")
            after = warehouse.query(
                "SELECT COUNT(*) AS n FROM cn_quote_snapshot")[0]["n"]
    # warehouse.insert 返回的是尝试行数，幂等去重后以实际增量为准
    return int(after - before)


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股盘中快照常驻采集")
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--interval", type=int, default=60, help="抓取间隔秒")
    parser.add_argument("--once", action="store_true", help="只抓一次后退出")
    args = parser.parse_args()

    while True:
        now = datetime.now(CST)
        if in_session(now):
            # 网络/锁冲突指数退避重试（5s/15s/45s），三次失败才等下一间隔
            last_exc: Exception | None = None
            for attempt, backoff in enumerate((5, 15, 45), 1):
                try:
                    n = snapshot_once(args.db)
                    print(f"[{now.isoformat()}] snapshot rows={n}", flush=True)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    print(f"[{now.isoformat()}] snapshot failed "
                          f"(attempt {attempt}/3): {exc}", flush=True)
                    if attempt == 3:
                        break
                    time.sleep(backoff)
            if last_exc is not None:
                escape_snapshot(args.db, now, last_exc)
            time.sleep(args.interval)
        else:
            if args.once:
                print(f"[{now.isoformat()}] 非采集窗口", flush=True)
                return
            time.sleep(30)
        if args.once:
            return


if __name__ == "__main__":
    main()
