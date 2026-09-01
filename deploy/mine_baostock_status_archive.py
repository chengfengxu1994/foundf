"""baostock raw 归档 tradestatus 挖掘 — 真 PIT 宇宙重建 Phase 2（现役池）。

扫描 ``data/raw/cn_stock/baostock/**/*.json`` 日线归档
（``schema_version=foundf.raw.baostock.v1``，由
``data_provider/providers/baostock_provider.py`` 的 ``_archive_raw`` 写入），
从 ``daily_price`` / ``daily_basic`` 操作的原始响应行提取
``tradestatus``，回填现役池 ``stock_status_daily`` 表。

口径：

- symbol 取自归档 ``request.code``（``sh.601166`` → ``601166``，6 位纯数字；
  非 6 位如指数 ``sh.000300`` 跳过，且指数查询本就不带 tradestatus）。
- ``trade_status`` = baostock tradestatus（1 正常 / 0 停牌），原样转 int；
  缺失或无法解析的行跳过并计数。
- ``is_st``：2026-08-14 前的历史归档查询字段不含 isST，落 **-1（未知）**；
  provider 改动后的新归档（字段含 isST）按原值落 0/1。
  同一 (symbol, date) 多次出现时，**is_st 已知（>=0）的记录优先**。
- 写入 ``source='baostock-archive'``，INSERT OR REPLACE（UNIQUE(symbol,date)）
  幂等，重跑安全。
- 只写 stock_status_daily 一张表，不碰 daily_price 等任何其它表。

运行（宿主机即可，纯本地文件扫描，不登 baostock、无采集窗口限制）：
    python3 deploy/mine_baostock_status_archive.py --dry-run   # 只统计不写库
    python3 deploy/mine_baostock_status_archive.py             # 实跑写库
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    _PROJECT_ROOT = Path("/app")
sys.path.insert(0, str(_PROJECT_ROOT))

from foundf_db import Warehouse  # noqa: E402

ARCHIVE_ROOT = Path("data/raw/cn_stock/baostock")
REPORT_DIR = Path("reports/pit_universe")
# 只从这两类操作里挖 tradestatus（stock_basic 等无日线行）
MINED_OPERATIONS = ("daily_price", "daily_basic", "daily_price_with_status")

DB_LOCK_RETRY = 5
DB_LOCK_SLEEP_S = 10


def _insert_replace_fast(wh: "Warehouse", table: str,
                         rows: list[dict[str, Any]]) -> int:
    """pandas DataFrame 单语句 INSERT OR REPLACE。

    ``Warehouse.insert`` 的 executemany 在 UNIQUE 约束下约 350 行/秒，
    35 万行要十几分钟；注册 DataFrame 单语句插入快两个数量级
    （与 migration.py 的 parquet 迁移同模式）。date 列显式 ::DATE 转换。
    """
    if not rows:
        return 0
    import pandas as pd
    df = pd.DataFrame(rows)
    selects = [f"{c}::DATE AS {c}" if c == "date" else c for c in df.columns]
    wh.conn.register("_fast_batch", df)
    try:
        wh.conn.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(df.columns)}) "
            f"SELECT {', '.join(selects)} FROM _fast_batch")
    finally:
        wh.conn.unregister("_fast_batch")
    return len(df)


def _now_cn() -> datetime:
    """北京时间（报告抬头与 probe/backfill 报告同口径）。"""
    return datetime.now(timezone(timedelta(hours=8)))


# ── 纯函数：归档解析（可单测，不依赖文件系统/网络）────────────────

def iter_archive_files(root: Path) -> Iterator[Path]:
    """按路径排序遍历归档 JSON（确定性顺序，便于对账复现）。"""
    yield from sorted(root.glob("**/*.json"))


def parse_archive_payload(payload: dict[str, Any]) -> tuple[str | None, list[dict[str, int]]]:
    """单个归档 payload → (symbol, [{date, trade_status, is_st}, ...])。

    symbol 无法归一为 6 位数字、操作类型不在挖掘范围、或行内缺
    date/tradestatus 时跳过（返回 symbol=None 或空列表由调用方计数）。
    """
    operation = str(payload.get("operation") or "")
    if operation not in MINED_OPERATIONS:
        return None, []
    request = payload.get("request") or {}
    code = str(request.get("code") or "")
    prefix, _, digits = code.partition(".")
    if len(digits) != 6 or not digits.isdigit():
        return None, []
    # 只挖个股：sh 5/6/9 开头、sz 0/1/2/3 开头（与 BaoStockProvider
    # _provider_code 映射同口径）；sh.000300 等指数 code 跳过。
    if not ((prefix == "sh" and digits[0] in "569")
            or (prefix == "sz" and digits[0] in "0123")):
        return None, []
    fields: list[str] = list(payload.get("fields") or [])
    idx = {name: i for i, name in enumerate(fields)}
    if "date" not in idx or "tradestatus" not in idx:
        return digits, []
    out: list[dict[str, int]] = []
    for row in payload.get("rows") or []:
        try:
            day = str(row[idx["date"]])
            date.fromisoformat(day)  # 校验日期格式，坏行跳过
            trade_status = int(str(row[idx["tradestatus"]]))
            is_st = -1  # 历史归档无 isST 字段 → -1（未知）
            if "isST" in idx:
                raw_st = str(row[idx["isST"]]).strip()
                if raw_st != "":
                    is_st = int(raw_st)
        except (IndexError, TypeError, ValueError):
            continue
        out.append({"date": day, "trade_status": trade_status, "is_st": is_st})
    return digits, out


def merge_status_rows(
    records: list[tuple[str, dict[str, int]]],
) -> list[dict[str, Any]]:
    """(symbol, {date, trade_status, is_st}) 流 → 去重后的 stock_status_daily 行。

    同一 (symbol, date) 多条时 is_st 已知（>=0）优先；其余字段取先见值。
    """
    best: dict[tuple[str, str], dict[str, int]] = {}
    for symbol, rec in records:
        key = (symbol, rec["date"])
        cur = best.get(key)
        if cur is None or (cur["is_st"] < 0 <= rec["is_st"]):
            best[key] = rec
    return [
        {
            "date": day,
            "symbol": symbol,
            "trade_status": rec["trade_status"],
            "is_st": rec["is_st"],
            "source": "baostock-archive",
        }
        for (symbol, day), rec in sorted(best.items())
    ]


def reconcile_stats(rows: list[dict[str, Any]]) -> dict[str, int]:
    """对账计数：覆盖 symbol 数 / 总行数 / 停牌行数 / ST 行数 / is_st 未知行数。"""
    return {
        "symbols": len({r["symbol"] for r in rows}),
        "rows": len(rows),
        "suspended": sum(1 for r in rows if r["trade_status"] == 0),
        "st": sum(1 for r in rows if r["is_st"] == 1),
        "st_unknown": sum(1 for r in rows if r["is_st"] < 0),
    }


# ── 报告与主流程 ─────────────────────────────────────────────────

def render_report(
    stats: dict[str, int],
    scan: dict[str, int],
    *,
    dry_run: bool,
    db_path: str,
    root: Path,
) -> str:
    """对账报告 Markdown。"""
    return "\n".join([
        f"# stock_status_daily 现役池归档挖掘对账 — {_now_cn():%Y-%m-%d %H:%M} 北京时间",
        "",
        f"> 归档根：`{root}`；库：`{db_path}`；"
        f"模式：{'DRY-RUN（未写库）' if dry_run else '已写库（INSERT OR REPLACE 幂等）'}",
        "",
        "## 1. 归档扫描",
        "",
        f"- JSON 文件总数：{scan['files']}",
        f"- 日线操作归档（daily_price/daily_basic）：{scan['mined_files']}",
        f"- 跳过（非日线操作/非 6 位 symbol/坏 payload）：{scan['skipped_files']}",
        f"- 行内坏行（缺 date/tradestatus 或格式错）：{scan['bad_rows']}",
        "",
        "## 2. 回填对账（source='baostock-archive'）",
        "",
        f"- 覆盖 symbol 数：{stats['symbols']}",
        f"- 总行数（去重后 (symbol,date)）：{stats['rows']}",
        f"- 停牌行（trade_status=0）：{stats['suspended']}",
        f"- ST 行（is_st=1）：{stats['st']}",
        f"- is_st 未知（-1，历史归档无 isST 字段）：{stats['st_unknown']}",
        "",
    ])


def _open_warehouse(db_path: str) -> Warehouse:
    """建立连接并执行 DDL（DuckDB 单写锁冲突时重试）。"""
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


def run(
    db_path: str,
    *,
    dry_run: bool = False,
    root: Path = ARCHIVE_ROOT,
    report_dir: Path = REPORT_DIR,
) -> dict[str, Any]:
    """主流程。返回对账结果字典；报告落 reports/pit_universe/status_archive_<date>.md。"""
    records: list[tuple[str, dict[str, int]]] = []
    scan = {"files": 0, "mined_files": 0, "skipped_files": 0, "bad_rows": 0}
    for path in iter_archive_files(root):
        scan["files"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            scan["skipped_files"] += 1
            continue
        symbol, rows = parse_archive_payload(payload)
        if symbol is None:
            scan["skipped_files"] += 1
            continue
        scan["mined_files"] += 1
        raw_row_count = len(payload.get("rows") or [])
        scan["bad_rows"] += raw_row_count - len(rows) if raw_row_count >= len(rows) else 0
        records.extend((symbol, rec) for rec in rows)

    rows = merge_status_rows(records)
    stats = reconcile_stats(rows)
    print(f"[status_archive] 扫描 {scan['files']} 个归档文件"
          f"（日线 {scan['mined_files']} / 跳过 {scan['skipped_files']}）")
    print(f"[status_archive] 去重后 {stats['rows']} 行 / {stats['symbols']} 只 "
          f"（停牌 {stats['suspended']} / ST {stats['st']} / "
          f"is_st未知 {stats['st_unknown']}）")

    written = 0
    if dry_run:
        print("[status_archive] DRY-RUN：不写库")
    else:
        wh = _open_warehouse(db_path)
        try:
            written = _insert_replace_fast(wh, "stock_status_daily", rows)
            print(f"[status_archive] 已写库 {written} 行（INSERT OR REPLACE 幂等）")
        finally:
            wh.close()

    report = render_report(stats, scan, dry_run=dry_run,
                           db_path=db_path, root=root)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"status_archive_{_now_cn():%Y-%m-%d}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[status_archive] 报告：{report_path}")
    return {"stats": stats, "scan": scan, "written": written,
            "report_path": str(report_path)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="baostock raw 归档 tradestatus 挖掘 → stock_status_daily")
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--root", default=str(ARCHIVE_ROOT),
                        help="归档根目录（默认 data/raw/cn_stock/baostock）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描统计，不写库（仍写报告）")
    args = parser.parse_args()
    run(args.db, dry_run=args.dry_run, root=Path(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
