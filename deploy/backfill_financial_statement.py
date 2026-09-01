#!/usr/bin/env python3
"""financial_statement 全历史回填（baostock 季度财务接口）。

背景：financial_statement 表近乎为空，导致质量/成长因子只能用价格代理
（research_engine 已判 quality_proxy/growth_proxy CULL）。baostock 提供
query_profit_data / query_growth_data / query_cash_flow_data 三个季度接口，
带 pubDate（真实披露日，PIT 安全）与 statDate。

口径：
- 宇宙：stock_registry 中有 daily_price 行情的标的（约 331 只，含退市股）
- 区间：2007Q1 起（与 daily_basic 估值序列同起点）
- symbol 落 6 位纯数字，与 stock_registry / 研究宇宙同口径
- roe=roeAvg, net_margin=npMargin, gross_margin=gpMargin, net_profit=netProfit,
  eps=epsTTM, profit_growth=YOYNI, filed_at=pubDate, report_date=statDate
- 新增列 cfo_to_np（经营现金流/净利润，应计质量的反向代理；源为
  query_cash_flow_data.CFOToNP）
- INSERT OR REPLACE 幂等；已存在的 (symbol, report_date) 跳过（断点续跑）
- 原始三接口返回逐票落 data/raw/cn_stock/baostock_fundamental/（审计追溯）

注意：baostock 单账号单会话，避开 17:15 collect 窗口（会被踢）。
.venv/bin/python deploy/backfill_financial_statement.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb

DB = Path("data/finance.duckdb")
RAW_DIR = Path("data/raw/cn_stock/baostock_fundamental")
START_YEAR = 2007

PROFIT_MAP = {  # baostock profit 字段 → financial_statement 列
    "roeAvg": "roe",
    "npMargin": "net_margin",
    "gpMargin": "gross_margin",
    "netProfit": "net_profit",
    "epsTTM": "eps",
}
GROWTH_MAP = {"YOYNI": "profit_growth"}
CASHFLOW_MAP = {"CFOToNP": "cfo_to_np"}


def _f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quarters(today=None):
    import datetime as _dt

    now = today or _dt.date.today()
    for year in range(START_YEAR, now.year + 1):
        for quarter in (1, 2, 3, 4):
            # 未到披露季的季度跳过（季报披露滞后约 1-4 个月，粗放宽到季末+150 天）
            q_end = _dt.date(year, quarter * 3, 28)
            if (now - q_end).days < 150:
                continue
            yield year, quarter


def _universe(db: duckdb.DuckDBPyConnection) -> list[str]:
    rows = db.execute(
        "SELECT r.symbol FROM stock_registry r "
        "JOIN (SELECT DISTINCT symbol FROM daily_price) p "
        "ON r.symbol = p.symbol ORDER BY r.symbol"
    ).fetchall()
    return [r[0] for r in rows]


def _existing(db: duckdb.DuckDBPyConnection) -> set[tuple[str, str]]:
    rows = db.execute(
        "SELECT symbol, report_date::VARCHAR FROM financial_statement "
        "WHERE source = 'baostock'"
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def _ensure_cfo_column(db: duckdb.DuckDBPyConnection) -> None:
    cols = {c[0] for c in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='financial_statement' AND table_schema='main'"
    ).fetchall()}
    if "cfo_to_np" not in cols:
        db.execute("ALTER TABLE financial_statement ADD COLUMN cfo_to_np DOUBLE")


def _query_all(bs, code: str, year: int, quarter: int) -> tuple[dict, dict, list[str]]:
    """三接口合并查询一票一季度，返回 (merged_fields, raw_payload, errors)。

    errors 非空表示接口返回 error_code != '0'（限流/拉黑/会话失效），
    调用方必须将其与"真无数据"区分——静默吞掉会把接口故障误判为空数据。
    """
    merged: dict = {}
    raw: dict = {}
    errors: list[str] = []
    for name, query in (
        ("profit", bs.query_profit_data),
        ("growth", bs.query_growth_data),
        ("cashflow", bs.query_cash_flow_data),
    ):
        rs = query(code=code, year=year, quarter=quarter)
        if rs.error_code != "0":
            errors.append(f"{name}:{rs.error_code}:{rs.error_msg}")
            raw[name] = []
            continue
        rows = []
        while rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        raw[name] = rows
        if rows:
            merged.update(rows[0])
    return merged, raw, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（测速/试跑）")
    args = parser.parse_args()

    db = duckdb.connect(str(DB), read_only=args.dry_run)
    symbols = _universe(db)
    existing = _existing(db)
    if not args.dry_run:
        _ensure_cfo_column(db)
    print(f"宇宙 {len(symbols)} 只，已有 baostock 行 {len(existing)} 组", flush=True)
    if args.limit:
        symbols = symbols[: args.limit]

    quarters = list(_quarters())
    print(f"季度窗口 {quarters[0]} .. {quarters[-1]} 共 {len(quarters)} 季", flush=True)
    if args.dry_run:
        db.close()
        return 0

    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        print(f"baostock login 失败: {lg.error_msg}", flush=True)
        db.close()
        return 1

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"rows": 0, "skipped_existing": 0, "empty": 0, "symbols_done": 0,
             "query_errors": 0}
    t0 = time.time()
    abort = False
    try:
        for sym in symbols:
            if abort:
                break
            code = f"{'sh' if sym.startswith('6') else 'sz'}.{sym}"
            sym_raw: dict = {}
            pending = []
            consecutive_error_quarters = 0
            for year, quarter in quarters:
                q_end_month = quarter * 3
                stat_date = f"{year}-{q_end_month:02d}-"
                stat_date += {3: "31", 6: "30", 9: "30", 12: "31"}[q_end_month]
                if (sym, stat_date) in existing:
                    stats["skipped_existing"] += 1
                    continue
                merged, raw, errors = _query_all(bs, code, year, quarter)
                sym_raw[f"{year}Q{quarter}"] = raw
                if errors:
                    stats["query_errors"] += 1
                    consecutive_error_quarters += 1
                    print(f"⚠️  {sym} {year}Q{quarter} 接口错误: "
                          f"{'; '.join(errors)}", flush=True)
                    if consecutive_error_quarters >= 3:
                        # 连续三季全错 = 会话级故障（限流/拉黑），
                        # fail-closed 中止，防止把故障整批误判为空数据
                        print(f"❌ 连续 {consecutive_error_quarters} 季接口错误，"
                              f"判定 baostock 会话故障，中止于 {sym}"
                              f"（已写 {stats['symbols_done']} 只，幂等可续跑）",
                              flush=True)
                        abort = True
                        break
                    continue
                consecutive_error_quarters = 0
                if not merged.get("statDate"):
                    stats["empty"] += 1
                    continue
                pending.append((
                    sym,
                    merged["statDate"],
                    "Q",
                    merged.get("pubDate") or None,
                    None,  # revenue（profit 接口 MBRevenue 对银行等常为空，不硬填）
                    _f(merged.get("netProfit")),
                    None,  # operating_cf（接口只给比率，不给总额）
                    None, None, None, None,  # fcf/assets/liab/equity
                    _f(merged.get("roeAvg")),
                    None,  # roa
                    None,  # debt_ratio
                    _f(merged.get("gpMargin")),
                    _f(merged.get("npMargin")),
                    _f(merged.get("epsTTM")),
                    None,  # bvps
                    None,  # revenue_growth（growth 接口无营收同比列）
                    _f(merged.get("YOYNI")),
                    None,  # r_and_d_expense
                    "baostock",
                    None, None,  # pe/pb（估值序列在 daily_basic）
                    _f(merged.get("CFOToNP")),
                ))
            if pending:
                db.executemany(
                    "INSERT OR REPLACE INTO financial_statement VALUES ("
                    + ", ".join(["?"] * 25) + ")",
                    pending,
                )
                stats["rows"] += len(pending)
            (RAW_DIR / f"{sym}.json").write_text(
                json.dumps(sym_raw, ensure_ascii=False), encoding="utf-8"
            )
            stats["symbols_done"] += 1
            if stats["symbols_done"] % 20 == 0:
                rate = stats["symbols_done"] / max(time.time() - t0, 1)
                eta = (len(symbols) - stats["symbols_done"]) / max(rate, 0.01) / 60
                print(
                    f"[{stats['symbols_done']}/{len(symbols)}] rows={stats['rows']} "
                    f"empty={stats['empty']} {rate:.2f} 只/s ETA {eta:.0f} 分钟",
                    flush=True,
                )
    finally:
        bs.logout()
        db.close()
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    return 2 if abort else 0


if __name__ == "__main__":
    sys.exit(main())
