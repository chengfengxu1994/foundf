"""退市股行情补采 — 真 PIT 宇宙重建 Phase 2（档 C：2020 年来退市股）。

从 ``stock_registry`` 选出 ``ipo_date <= 今天 且 out_date >= '2020-01-01'
且 symbol 不在 daily_price`` 的退市股（约 225 只，probe_2026-08-14.md 实测
口径），baostock 单会话逐票拉全历史 qfq 日线（adjustflag=2，字段含
tradestatus/isST，复用 ``BaoStockProvider`` 的会话与 ``_archive_raw``
归档——raw 响应照常落 ``data/raw/cn_stock/baostock/``）。

分流口径：

- ``tradestatus=1``（正常交易）行 → ``INSERT OR REPLACE`` 进 ``daily_price``
  （列集合与现有行对齐：date/symbol/open/high/low/close/volume/amount/
  adj_factor=NULL/source='baostock'/quality_score=100；停牌行不进
  daily_price，与现役池 nightly 口径一致）。
- 全部行（含停牌）→ ``stock_status_daily``（trade_status=tradestatus，
  is_st=isST 原值，source='baostock'）。
- 两张表均 UNIQUE(symbol,date) + INSERT OR REPLACE，幂等重跑安全；
  **绝不 UPDATE/DELETE daily_price 现有行**。

对账：逐票「baostock 返回行数 vs 入库行数」、首末行情日，末日与
``stock_registry.out_date`` 比对（±3 天容差，超差进报告 WARNING 清单）；
报告落 ``reports/pit_universe/backfill_<date>.md``。

运行（宿主机 .venv，baostock 只装在 .venv）：
    .venv/bin/python deploy/backfill_delisted_daily.py --dry-run   # 只拉数不写库
    .venv/bin/python deploy/backfill_delisted_daily.py             # 实跑写库
    .venv/bin/python deploy/backfill_delisted_daily.py --limit 5   # 抽样试跑

注意：baostock 单账号单会话，**避开 17:15 前后 nightly collect 窗口**
（16:30–18:30 只跑 --dry-run 也有踢会话风险，尽量整个避开）；DuckDB
单写者，写库前确认无其它写进程。
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    _PROJECT_ROOT = Path("/app")
sys.path.insert(0, str(_PROJECT_ROOT))

from foundf_db import Warehouse  # noqa: E402

REPORT_DIR = Path("reports/pit_universe")
DELISTED_SINCE = date(2020, 1, 1)          # 档 C：2020 年来退市股
LAST_DATE_TOLERANCE_DAYS = 3               # 末日 vs out_date 容差
SYMBOL_SLEEP_S = 0.3                       # 逐票节奏，保护 baostock 单会话
DB_LOCK_RETRY = 5
DB_LOCK_SLEEP_S = 10


def _insert_replace_fast(wh: "Warehouse", table: str,
                         rows: list[dict[str, Any]]) -> int:
    """pandas DataFrame 单语句 INSERT OR REPLACE。

    ``Warehouse.insert`` 的 executemany 在 UNIQUE 约束下约 350 行/秒，
    数十万行要等几十分钟；注册 DataFrame 单语句插入快两个数量级
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

# baostock 历史起点兜底（ipo_date 缺失时）；A 股 1990-12-19 开市
DEFAULT_START = "1990-12-19"


def _now_cn() -> datetime:
    """北京时间（报告文件名与抬头与 probe 报告同口径）。"""
    return datetime.now(timezone(timedelta(hours=8)))


# ── 纯函数：选股 / 分流 / 对账（可单测，不依赖网络）────────────────

def select_delisted_targets(
    registry_rows: list[dict[str, Any]],
    daily_price_symbols: set[str],
    *,
    today: date,
    delisted_since: date = DELISTED_SINCE,
) -> list[dict[str, Any]]:
    """档 C 选股：退市、out_date >= delisted_since、已上市且 daily_price 无行情。

    registry_rows 元素须含 symbol/ipo_date/out_date/list_status。
    返回按 symbol 排序的目标列表。
    """
    out: list[dict[str, Any]] = []
    for row in registry_rows:
        if row.get("list_status") != "DELISTED":
            continue
        out_date = row.get("out_date")
        if out_date is None or out_date < delisted_since:
            continue  # 2020 前退市不在档 C 范围
        ipo_date = row.get("ipo_date")
        if ipo_date is not None and ipo_date > today:
            continue  # 上市日晚于今天：口径异常，跳过
        symbol = str(row["symbol"])
        if symbol in daily_price_symbols:
            continue  # 已有行情（理论上不该命中，防御）
        out.append({
            "symbol": symbol,
            "code_name": row.get("code_name"),
            "ipo_date": ipo_date,
            "out_date": out_date,
        })
    return sorted(out, key=lambda r: r["symbol"])


def split_bars(
    symbol: str, bars: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """逐票 baostock 全行 → (daily_price 行, stock_status_daily 行)。

    - tradestatus=1 且 close 可解析 → daily_price（adj_factor 恒 NULL，
      与现役池 baostock 行口径一致）；
    - 全部行 → stock_status_daily（含停牌行）。
    """
    price_rows: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []
    for bar in bars:
        status_rows.append({
            "date": bar["date"],
            "symbol": symbol,
            "trade_status": bar["trade_status"],
            "is_st": bar["is_st"],
            "source": "baostock",
        })
        if bar["trade_status"] != 1:
            continue  # 停牌行不进 daily_price（与 nightly 口径一致）
        if bar.get("close") is None:
            continue  # 坏行防御：close 缺失不落行情
        price_rows.append({
            "date": bar["date"],
            "symbol": symbol,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar["close"],
            "volume": bar.get("volume") or 0.0,
            "amount": bar.get("amount") or 0.0,
            "adj_factor": None,
            "source": "baostock",
            "quality_score": 100,
        })
    return price_rows, status_rows


def reconcile_symbol(
    target: dict[str, Any],
    bars: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """逐票对账：返回行数、首末日、末日与 out_date 偏差天数（自然日）。"""
    dates = [b["date"] for b in bars]
    first = min(dates) if dates else None
    last = max(dates) if dates else None
    out_date = target.get("out_date")
    lag: int | None = None
    if last is not None and out_date is not None:
        lag = (out_date - date.fromisoformat(last)).days
    return {
        "symbol": target["symbol"],
        "code_name": target.get("code_name"),
        "out_date": str(out_date) if out_date else None,
        "fetched_rows": len(bars),
        "price_rows": len(price_rows),
        "suspended_rows": len(bars) - len(price_rows),
        "first_date": first,
        "last_date": last,
        "last_vs_out_lag_days": lag,
        "last_date_ok": (
            lag is not None and abs(lag) <= LAST_DATE_TOLERANCE_DAYS
        ),
    }


# ── 报告与主流程 ─────────────────────────────────────────────────

def render_report(
    targets: list[dict[str, Any]],
    per_symbol: list[dict[str, Any]],
    errors: list[dict[str, str]],
    *,
    dry_run: bool,
    db_path: str,
    elapsed_s: float,
) -> str:
    """补采对账报告 Markdown。"""
    total_fetched = sum(r["fetched_rows"] for r in per_symbol)
    total_price = sum(r["price_rows"] for r in per_symbol)
    total_susp = sum(r["suspended_rows"] for r in per_symbol)
    bad_last = [r for r in per_symbol if not r["last_date_ok"]]
    empty = [r for r in per_symbol if r["fetched_rows"] == 0]
    lines = [
        f"# 退市股行情补采对账 — {_now_cn():%Y-%m-%d %H:%M} 北京时间",
        "",
        f"> 库：`{db_path}`；模式：{'DRY-RUN（未写库）' if dry_run else '已写库（INSERT OR REPLACE 幂等）'}；"
        f"耗时 {elapsed_s:.0f}s",
        "",
        "## 1. 总量",
        "",
        f"- 档 C 目标（2020 后来退市、daily_price 无行情）：{len(targets)} 只",
        f"- 成功拉取：{len(per_symbol) - len(empty)} 只；空响应：{len(empty)} 只；"
        f"失败：{len(errors)} 只",
        f"- baostock 返回总行数：{total_fetched}（→ daily_price {total_price} / "
        f"停牌行进 status 表 {total_susp}）",
        "",
        "## 2. 末日 vs out_date 核对（±%d 天容差）" % LAST_DATE_TOLERANCE_DAYS,
        "",
        f"- 通过：{len(per_symbol) - len(bad_last) - len(empty)} 只；"
        f"超差 WARNING：{len(bad_last)} 只；空响应无法核对：{len(empty)} 只",
        "",
    ]
    if bad_last:
        lines.append("### 末日超差清单（symbol / 名称 / 末日 / out_date / 偏差天）")
        lines.append("")
        for r in bad_last:
            lines.append(
                f"- {r['symbol']} {r.get('code_name') or ''} "
                f"last={r['last_date']} out={r['out_date']} "
                f"lag={r['last_vs_out_lag_days']}d")
        lines.append("")
    if errors:
        lines.append("### 拉取失败清单（symbol / 错误）")
        lines.append("")
        for e in errors:
            lines.append(f"- {e['symbol']}: {e['error']}")
        lines.append("")
    lines += [
        "## 3. 抽样明细（前 5 只逐票对账）",
        "",
        "| symbol | 名称 | 返回行 | 入 daily_price | 停牌行 | 首日 | 末日 | out_date | 偏差天 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in per_symbol[:5]:
        lines.append(
            f"| {r['symbol']} | {r.get('code_name') or ''} | {r['fetched_rows']} "
            f"| {r['price_rows']} | {r['suspended_rows']} | {r['first_date']} "
            f"| {r['last_date']} | {r['out_date']} | {r['last_vs_out_lag_days']} |")
    lines.append("")
    return "\n".join(lines)


def _open_warehouse(db_path: str, *, init: bool = True) -> Warehouse:
    """建立连接（DuckDB 单写锁冲突时重试）。init=False 严格只读（dry-run）。"""
    for attempt in range(DB_LOCK_RETRY):
        try:
            wh = Warehouse(db_path)
            if init:
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
    limit: int | None = None,
    fetch_bars=None,      # 注入假拉取函数（测试用）：(symbol, start, end) -> bars
    sleep_s: float = SYMBOL_SLEEP_S,
    report_dir: Path = REPORT_DIR,
) -> dict[str, Any]:
    """主流程。fetch_bars 可注入（测试不碰网络）；默认走 BaoStockProvider
    单会话 get_daily_bars_with_status（raw 照常归档）。"""
    started = time.monotonic()
    today = _now_cn().date()

    # dry-run 严格只读（连 DDL 也不执行）；实跑先 init 确保 status 表存在
    wh = _open_warehouse(db_path, init=not dry_run)
    try:
        registry_rows = wh.query(
            "SELECT symbol, code_name, ipo_date, out_date, list_status "
            "FROM stock_registry")
        dp_symbols = {
            str(r["symbol"]) for r in wh.query(
                "SELECT DISTINCT symbol FROM daily_price "
                "WHERE regexp_matches(symbol, '^[0-9]{6}$')")
        }
    finally:
        wh.close()
    targets = select_delisted_targets(registry_rows, dp_symbols, today=today)
    if limit is not None:
        targets = targets[:limit]
    print(f"[delisted_backfill] 档 C 目标 {len(targets)} 只"
          f"（{'抽样' if limit else '全量'}，{today}）")

    if fetch_bars is None:
        # 默认：BaoStockProvider 单会话逐票拉取（raw 归档照常落盘）
        from data_provider.providers.baostock_provider import BaoStockProvider

        provider = BaoStockProvider()

        def _fetch(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
            return provider.get_daily_bars_with_status(symbol, start, end)

        fetch_bars = _fetch
        session_ctx = provider.session
    else:
        @contextmanager
        def session_ctx():
            yield

    per_symbol: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    all_price: list[dict[str, Any]] = []
    all_status: list[dict[str, Any]] = []
    end_str = today.isoformat()
    with session_ctx():
        for i, target in enumerate(targets, 1):
            symbol = target["symbol"]
            start = (target["ipo_date"].isoformat()
                     if target.get("ipo_date") else DEFAULT_START)
            try:
                bars = fetch_bars(symbol, start, end_str)
            except Exception as exc:
                errors.append({"symbol": symbol,
                               "error": f"{type(exc).__name__}: {exc}"})
                print(f"    ✗ [{i}/{len(targets)}] {symbol} 拉取失败: {exc}")
                continue
            price_rows, status_rows = split_bars(symbol, bars)
            rec = reconcile_symbol(target, bars, price_rows)
            per_symbol.append(rec)
            all_price.extend(price_rows)
            all_status.extend(status_rows)
            flag = "" if rec["last_date_ok"] else "  ⚠末日超差"
            print(f"    ✓ [{i}/{len(targets)}] {symbol} "
                  f"{target.get('code_name') or ''} 返回 {rec['fetched_rows']} 行 "
                  f"(行情 {rec['price_rows']} / 停牌 {rec['suspended_rows']}) "
                  f"{rec['first_date']}~{rec['last_date']}{flag}")
            if i < len(targets):
                time.sleep(sleep_s)

    written = {"daily_price": 0, "stock_status_daily": 0}
    if dry_run:
        print(f"[delisted_backfill] DRY-RUN：不写库"
              f"（待写 daily_price {len(all_price)} / status {len(all_status)} 行）")
    else:
        wh = _open_warehouse(db_path)
        try:
            written["daily_price"] = _insert_replace_fast(
                wh, "daily_price", all_price)
            written["stock_status_daily"] = _insert_replace_fast(
                wh, "stock_status_daily", all_status)
            print(f"[delisted_backfill] 已写库 daily_price "
                  f"{written['daily_price']} 行 / stock_status_daily "
                  f"{written['stock_status_daily']} 行（幂等）")
        finally:
            wh.close()

    elapsed = time.monotonic() - started
    report = render_report(targets, per_symbol, errors,
                           dry_run=dry_run, db_path=db_path, elapsed_s=elapsed)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"backfill_{_now_cn():%Y-%m-%d}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[delisted_backfill] 报告：{report_path}（耗时 {elapsed:.0f}s）")
    return {"targets": len(targets), "per_symbol": per_symbol,
            "errors": errors, "written": written,
            "report_path": str(report_path), "elapsed_s": elapsed}


def main() -> int:
    parser = argparse.ArgumentParser(description="退市股行情补采（档 C）")
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉数对账，不写库（仍写报告）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只处理前 N 只（抽样试跑）")
    args = parser.parse_args()
    run(args.db, dry_run=args.dry_run, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
