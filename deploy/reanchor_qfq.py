"""全历史前复权重锚 — 修复 baostock 增量采集造成的复权锚点接缝。

背景:
    scheduler 每日增量拉取 adjustflag=2（前复权），每次拉取以自身窗口末端
    为锚。个股除息后，新锚与旧锚之间的历史价格差一个分红因子，daily_price
    在除息日出现假跌幅（例: 601138 2026-08-03 除息 0.65 元，DB 跨缝收益
    -1.41%，真实前复权收益 -0.27%），污染动量信号与历史 IC。

做法:
    对 daily_price 中每只 6 位个股/ETF，从库内最早日期到今日一次性全历史
    前复权重拉（单一锚 = 拉取日），INSERT OR REPLACE 覆盖。原始响应仍经
    BaoStockProvider._archive_raw 落 data/raw 审计链。

运行（容器内，周末或无写入任务时段）:
    docker compose exec -T collector python3 deploy/reanchor_qfq.py
    docker compose exec -T collector python3 deploy/reanchor_qfq.py \
        --symbols 601138 601398 --dry-run

注意: 会改写全部历史收盘价，运行前先保留 IC 基线报告，运行后重跑
research_engine 对比。DuckDB 单写者，写冲突自动重试。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    # 经 `docker compose exec -T collector python3 - < 本文件` 运行时无 __file__，
    # 容器内代码位于 /app（PYTHONPATH 已含），此处兜底即可。
    _PROJECT_ROOT = Path("/app")
sys.path.insert(0, str(_PROJECT_ROOT))

from data_provider.providers.baostock_provider import BaoStockProvider
from foundf_db import Warehouse

LOCK_RETRY = 5
LOCK_RETRY_SLEEP_S = 10
MIN_ROWS_RATIO = 0.9  # 新行数低于旧行数该比例则拒绝覆盖


def _insert_with_retry(warehouse: Warehouse, rows: list[dict]) -> None:
    for attempt in range(LOCK_RETRY):
        try:
            warehouse.insert("daily_price", rows, conflict_strategy="replace")
            return
        except Exception as exc:
            if "lock" not in str(exc).lower() or attempt == LOCK_RETRY - 1:
                raise
            print(f"    ⏳ DuckDB 写锁冲突，{LOCK_RETRY_SLEEP_S}s 后重试 "
                  f"({attempt + 1}/{LOCK_RETRY})")
            time.sleep(LOCK_RETRY_SLEEP_S)


def reanchor_symbol(
    provider: BaoStockProvider,
    warehouse: Warehouse,
    symbol: str,
    end: str,
    dry_run: bool,
) -> dict:
    row = warehouse.query(
        "SELECT MIN(date)::VARCHAR AS first, COUNT(*) AS n "
        "FROM daily_price WHERE symbol = ?",
        [symbol],
    )[0]
    first, old_n = row["first"], int(row["n"])
    if not first:
        return {"symbol": symbol, "status": "SKIP_NO_DATA"}

    prices = provider.get_daily_price(symbol, first, end)
    if not prices:
        return {"symbol": symbol, "status": "FAIL_EMPTY", "old_rows": old_n}
    if len(prices) < old_n * MIN_ROWS_RATIO:
        return {
            "symbol": symbol,
            "status": "REJECT_TOO_FEW_ROWS",
            "old_rows": old_n,
            "new_rows": len(prices),
        }

    if dry_run:
        # 只报告除息缝变化最大的日期，不写库
        old = {
            r["date"]: float(r["close"])
            for r in warehouse.query(
                "SELECT date::VARCHAR AS date, close FROM daily_price "
                "WHERE symbol = ?",
                [symbol],
            )
        }
        diffs = [
            (p.date, old[p.date], p.close)
            for p in prices
            if p.date in old and abs(old[p.date] - p.close) > 1e-6
        ]
        return {
            "symbol": symbol,
            "status": "DRY_RUN",
            "old_rows": old_n,
            "new_rows": len(prices),
            "changed_dates": len(diffs),
            "sample": diffs[:3],
        }

    # 沿用该标的现有 quality_score（众数），避免重锚把 source_registry
    # 实时评分重置为默认值
    score_row = warehouse.query(
        "SELECT quality_score, COUNT(*) AS n FROM daily_price "
        "WHERE symbol = ? GROUP BY quality_score ORDER BY n DESC LIMIT 1",
        [symbol],
    )
    quality = float(score_row[0]["quality_score"]) if score_row else 100.0
    rows = [
        {
            "symbol": p.symbol, "date": p.date,
            "open": p.open, "high": p.high, "low": p.low,
            "close": p.close, "volume": p.volume, "amount": p.amount,
            "source": "baostock", "quality_score": quality,
        }
        for p in prices
    ]
    _insert_with_retry(warehouse, rows)
    return {
        "symbol": symbol,
        "status": "REANCHORED",
        "old_rows": old_n,
        "new_rows": len(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="全历史前复权重锚")
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="只处理这些 6 位代码；默认处理库内全部")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告将发生的变化，不写库")
    parser.add_argument("--db", default="data/finance.duckdb")
    args = parser.parse_args()

    warehouse = Warehouse(args.db)
    warehouse.init()
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
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[reanchor] {len(symbols)} 只标的, end={end}, dry_run={args.dry_run}")

    provider = BaoStockProvider()
    results = []
    t0 = time.time()
    with provider.session():
        for i, symbol in enumerate(symbols, 1):
            try:
                result = reanchor_symbol(
                    provider, warehouse, symbol, end, args.dry_run
                )
            except Exception as exc:
                result = {
                    "symbol": symbol,
                    "status": "ERROR",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            print(f"  [{i}/{len(symbols)}] {result}")

    ok = sum(1 for r in results if r["status"] in {"REANCHORED", "DRY_RUN"})
    bad = [r for r in results if r["status"] not in {"REANCHORED", "DRY_RUN"}]
    print(f"[reanchor] 完成 {ok}/{len(symbols)}，"
          f"耗时 {time.time() - t0:.0f}s，异常 {len(bad)} 只")
    for r in bad:
        print(f"  ⚠ {r}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
