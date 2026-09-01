#!/usr/bin/env python3
"""执行择时影子对照（只读测量，不影响任何交易链路）。

问题背景：模拟盘买入时机 = 10:12 调仓 + ±2% 偏离闸门 + 盘中回落补单。
「14:55 尾盘直接下单是不是更好」不拍脑袋——每日收盘后记录候选标的的
四个基准价，积累样本后由回归层回答：

- decision : T-1 收盘（sim_targets 快照，偏离闸门基准）
- p_1012   : 当日 10:12 之后第一条快照（现行首试时点的可成交价代理）
- p_1455   : 最接近 14:55 的快照（尾盘方案代理）
- p_close  : 当日最后一条快照（≈收盘价）

产物：reports/exec_shadow/<date>.json（每日一份，幂等覆盖）。
数据源：cn_quote_snapshot（东财 1 分钟快照）+ reports/sim_targets 最新快照。
只读 DuckDB（read_only=True），与 quote_daemon 写入不冲突。
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb

DB = Path("data/finance.duckdb")
TARGETS_DIR = Path("reports/sim_targets")
OUT_DIR = Path("reports/exec_shadow")
CST = timezone(timedelta(hours=8))


def latest_targets() -> dict | None:
    files = sorted(TARGETS_DIR.glob("*.json"), reverse=True)
    if not files:
        return None
    return json.loads(files[0].read_text(encoding="utf-8"))


def _snap_at(con, symbol: str, day: date, op: str, t: time) -> float | None:
    """op='>=' 取 t 之后第一条; op='<=' 取 t 之前最后一条(均为 CST 当日)。"""
    start = datetime.combine(day, time(0, 0), tzinfo=CST)
    end = start + timedelta(days=1)
    bound = datetime.combine(day, t, tzinfo=CST)
    order = "ASC" if op == ">=" else "DESC"
    row = con.execute(
        f"""SELECT last FROM cn_quote_snapshot
            WHERE symbol=? AND ts>=? AND ts<? AND ts {op} ?
            ORDER BY ts {order} LIMIT 1""",
        [symbol, start, end, bound],
    ).fetchone()
    return row[0] if row else None


def main() -> int:
    today = datetime.now(CST).date()
    targets = latest_targets()
    if not targets:
        print(json.dumps({"status": "NO_TARGETS"}))
        return 0
    symbols = [s for s in targets.get("weights", {}) if s != "CASH"]
    prices = targets.get("prices", {})
    if not symbols:
        print(json.dumps({"status": "EMPTY_TARGETS"}))
        return 0

    con = duckdb.connect(str(DB), read_only=True)
    try:
        day_end = datetime.combine(today, time(23, 59), tzinfo=CST)
        rows = {}
        for sym in symbols:
            hi_lo = con.execute(
                """SELECT max(high), min(low), count(*) FROM cn_quote_snapshot
                   WHERE symbol=? AND ts>=? AND ts<?""",
                [sym, datetime.combine(today, time(0, 0), tzinfo=CST), day_end],
            ).fetchone()
            rows[sym] = {
                "decision": prices.get(sym),
                "p_1012": _snap_at(con, sym, today, ">=", time(10, 12)),
                "p_1455": _snap_at(con, sym, today, "<=", time(14, 55)),
                "p_close": _snap_at(con, sym, today, "<=", time(23, 59)),
                "day_high": hi_lo[0],
                "day_low": hi_lo[1],
                "snapshots": hi_lo[2],
            }
    finally:
        con.close()

    record = {
        "date": today.isoformat(),
        "strategy_version": targets.get("strategy_version"),
        "data_as_of": targets.get("data_as_of"),
        "symbols": rows,
        "note": "影子对照: 只读测量, 不影响执行; p_1012/p_1455/p_close 为可成交价代理",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{today.isoformat()}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"status": "OK", "out": str(out), "symbols": len(rows)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
