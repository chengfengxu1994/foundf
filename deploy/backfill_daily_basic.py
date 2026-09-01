"""daily_basic 全历史回填 — baostock 每日估值指标（换手率/PE_TTM/PB）。

为 research_engine 的真实基本面因子（EP/BP/低换手）提供历史序列。
只写 daily_basic 表，绝不触碰 daily_price 价格主线。

baostock 日线接口自带 turn/peTTM/pbMRQ 字段，免费无限频、历史覆盖至
2007 年（2026-08-06 实测），因此替代 tushare daily_basic（需 2000 积分
且限频 1 次/分钟）成为主源；tushare 仅留 provider 兜底。

运行（容器内，baostock 装在镜像里）:
    docker compose exec -T collector python3 - < deploy/backfill_daily_basic.py
    docker compose exec -T collector python3 - --symbols 601138 --dry-run \
        < deploy/backfill_daily_basic.py

断点续跑：标的 daily_basic 已覆盖到近 4 天内则跳过；INSERT OR REPLACE
幂等。连接按需开合（短锁），不阻塞 nightly collect。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    # 经 `docker compose exec -T collector python3 - < 本文件` 运行时无 __file__
    _PROJECT_ROOT = Path("/app")
sys.path.insert(0, str(_PROJECT_ROOT))

from data_provider.providers.baostock_provider import BaoStockProvider
from foundf_db import Warehouse

CALL_SLEEP_S = 0.2
DB_LOCK_RETRY = 5
DB_LOCK_SLEEP_S = 10
BASIC_HISTORY_FLOOR = "2007-01-01"
SUCCESS_STATUSES = {
    "LOADED", "DRY_RUN", "SKIP_ALREADY", "SKIP_NO_EXPECTED_PRICE"
}


def _missing_expected_dates(
    expected_dates: set[str], rows: list[dict]
) -> set[str]:
    """Return price dates absent from a provider batch."""
    returned_dates = {str(row["date"]) for row in rows}
    return expected_dates - returned_dates


def _has_failures(results: list[dict]) -> bool:
    return any(row.get("status") not in SUCCESS_STATUSES for row in results)


def _open_warehouse(db_path: str) -> Warehouse:
    """建立连接（DuckDB 单写锁冲突时重试）。"""
    for attempt in range(DB_LOCK_RETRY):
        try:
            wh = Warehouse(db_path)
            wh.init()
            return wh
        except Exception as exc:
            if "lock" not in str(exc).lower() or attempt == DB_LOCK_RETRY - 1:
                raise
            print(f"    ⏳ DuckDB 连接锁冲突，{DB_LOCK_SLEEP_S}s 后重试 "
                  f"({attempt + 1}/{DB_LOCK_RETRY})")
            time.sleep(DB_LOCK_SLEEP_S)
    raise RuntimeError("unreachable")


def _insert_with_retry(warehouse: Warehouse, rows: list[dict]) -> None:
    for attempt in range(DB_LOCK_RETRY):
        try:
            warehouse.insert("daily_basic", rows, conflict_strategy="replace")
            return
        except Exception as exc:
            if "lock" not in str(exc).lower() or attempt == DB_LOCK_RETRY - 1:
                raise
            print(f"    ⏳ DuckDB 写锁冲突，{DB_LOCK_SLEEP_S}s 后重试 "
                  f"({attempt + 1}/{DB_LOCK_RETRY})")
            time.sleep(DB_LOCK_SLEEP_S)


def main() -> int:
    parser = argparse.ArgumentParser(description="daily_basic 全历史回填")
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="只处理这些 6 位代码；默认处理库内全部")
    parser.add_argument("--start", default=None,
                        help="起始日期 YYYY-MM-DD；默认取该标的 daily_price 最早日")
    parser.add_argument("--end", default=None,
                        help="结束日期 YYYY-MM-DD；默认北京时间当天")
    parser.add_argument("--force", action="store_true",
                        help="忽略近 4 天断点跳过；用于指定窗口缺口修复")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉取统计行数，不写库")
    parser.add_argument("--db", default="data/finance.duckdb")
    args = parser.parse_args()

    end = args.end or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    if args.start and args.start > end:
        parser.error("--start must not be later than --end")

    warehouse = _open_warehouse(args.db)
    if args.symbols:
        symbols = sorted(args.symbols)
    else:
        symbols = [
            str(r["symbol"])
            for r in warehouse.query(
                "SELECT DISTINCT symbol FROM daily_price "
                "WHERE regexp_matches(symbol, '^[0-9]{6}$') ORDER BY symbol"
            )
        ]
    warehouse.close()
    print(f"[daily_basic] {len(symbols)} 只标的, end={end}, "
          f"dry_run={args.dry_run} (baostock)")

    provider = BaoStockProvider()
    results = []
    t0 = time.time()
    resume_before = (
        datetime.now(timezone.utc) - timedelta(days=4)
    ).strftime("%Y-%m-%d")
    with provider.session():
        for i, symbol in enumerate(symbols, 1):
            wh = _open_warehouse(args.db)
            try:
                # 断点续跑：该标的 daily_basic 已覆盖到近 4 天内 → 跳过
                latest_basic = wh.query(
                    "SELECT MAX(date)::VARCHAR AS latest FROM daily_basic "
                    "WHERE symbol = ?",
                    [symbol],
                )[0]["latest"]
                first_missing = wh.query(
                    "SELECT MIN(p.date)::VARCHAR AS first_missing "
                    "FROM daily_price p LEFT JOIN daily_basic b "
                    "ON b.symbol = p.symbol AND b.date = p.date "
                    "WHERE p.symbol = ? AND p.date >= ?::DATE "
                    "AND p.date <= ?::DATE AND b.symbol IS NULL",
                    [symbol, BASIC_HISTORY_FLOOR, end],
                )[0]["first_missing"]
                if (
                    not args.force
                    and latest_basic
                    and latest_basic >= resume_before
                    and not first_missing
                ):
                    results.append({"symbol": symbol, "status": "SKIP_ALREADY",
                                    "latest": latest_basic})
                    print(f"  [{i}/{len(symbols)}] {results[-1]}")
                    wh.close()
                    continue
                start = args.start or first_missing
                if not start:
                    row = wh.query(
                        "SELECT GREATEST(MIN(date), ?::DATE)::VARCHAR AS first "
                        "FROM daily_price WHERE symbol = ?",
                        [BASIC_HISTORY_FLOOR, symbol],
                    )[0]
                    start = row["first"]
                if not start:
                    wh.close()
                    results.append({"symbol": symbol, "status": "SKIP_NO_PRICE"})
                    continue
                expected_dates = {
                    str(row["date"])
                    for row in wh.query(
                        "SELECT date::VARCHAR AS date FROM daily_price "
                        "WHERE symbol = ? AND date BETWEEN ? AND ?",
                        [symbol, start, end],
                    )
                }
                wh.close()
                rows = provider.get_daily_basic(symbol, start, end)
                missing_dates = _missing_expected_dates(expected_dates, rows)
                if missing_dates:
                    results.append({
                        "symbol": symbol,
                        "status": "INCOMPLETE",
                        "missing": len(missing_dates),
                        "first_missing": min(missing_dates),
                    })
                elif not rows and expected_dates:
                    results.append({"symbol": symbol, "status": "EMPTY"})
                elif not rows:
                    results.append({
                        "symbol": symbol, "status": "SKIP_NO_EXPECTED_PRICE"
                    })
                elif args.dry_run:
                    results.append({"symbol": symbol, "status": "DRY_RUN",
                                    "rows": len(rows),
                                    "first": rows[0]["date"],
                                    "last": rows[-1]["date"]})
                else:
                    for r in rows:
                        r["source"] = "baostock"
                    wh = _open_warehouse(args.db)
                    _insert_with_retry(wh, rows)
                    wh.close()
                    results.append({"symbol": symbol, "status": "LOADED",
                                    "rows": len(rows)})
            except Exception as exc:
                results.append({"symbol": symbol, "status": "ERROR",
                                "reason": f"{type(exc).__name__}: {exc}"})
                try:
                    wh.close()
                except Exception:
                    pass
            print(f"  [{i}/{len(symbols)}] {results[-1]}")
            if i < len(symbols):
                time.sleep(CALL_SLEEP_S)

    ok = sum(1 for r in results if r["status"] in SUCCESS_STATUSES)
    bad = [r for r in results if r["status"] not in SUCCESS_STATUSES]
    total_rows = sum(int(r.get("rows", 0)) for r in results)
    print(f"[daily_basic] 完成 {ok}/{len(symbols)}，共 {total_rows} 行，"
          f"耗时 {time.time() - t0:.0f}s，异常 {len(bad)} 只")
    for r in bad:
        print(f"  ⚠ {r}")
    return int(_has_failures(results))


if __name__ == "__main__":
    raise SystemExit(main())
