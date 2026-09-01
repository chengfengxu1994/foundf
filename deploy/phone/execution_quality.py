#!/usr/bin/env python3
"""执行质量分析（TCA）：量化信号 → 手机执行 → 成交反馈 → 打分 闭环的量化层。

用法::

    python3 deploy/phone/execution_quality.py [--date YYYY-MM-DD] [--days 30] [--dry-run]

输入（全部只读，绝不写生产库）：

- ``broker_sim_normalized_fill``：规范化模拟成交（fee_amount 恒 NULL →
  成本分析标 ``MISSING_FEE``，绝不补零）。
- ``strategy_candidate.decision_price``：候选决策价（2026-08-06 起）。
- ``cn_quote_snapshot``：东方财富分钟快照（**2026-08-06 起才有数**，
  早于该日的成交无 VWAP/开盘价口径，标 ``VWAP_NA``）。
- ``reports/sim_targets/<date>.json``：targets 快照顶层 ``prices`` 决策价表，
  孤儿成交的第一兜底来源。
- ``data/phone_sim_capture/rebalance/*.json``：调仓日志（委托提交 ts、
  SUBMITTED/REJECTED 状态），用于委托→成交时延与拒单率。

指标口径（逐笔，单位 bps = 1e-4）：

- ``slippage_decision_bps = (fill_price - decision_price) / decision_price
  × 1e4``，SELL 取反。按此公式，**正值 = 执行劣于决策（成本），
  负值 = 执行优于决策**（BUY 买贵了为正、SELL 卖便宜了为正）。
- 决策价来源 ``decision_basis``：``CANDIDATE``（候选 decision_price）；
  孤儿成交（candidate_id 为 NULL，候选只产 BUY，SELL 必孤儿）标
  ``ORPHAN`` 但仍统计，决策价依次兜底：fill_time 之前最新 targets
  prices（``ORPHAN_TARGETS``）→ 当日快照 prev_close / daily_price 前收盘
  （``ORPHAN_PREV_CLOSE``）→ 都没有则 ``NO_REFERENCE`` 不计滑点。
- ``dev_open_bps`` / ``dev_prev_close_bps`` / ``dev_vwap_bps``：同一符号
  约定（正=劣）对当日开盘价 / 前收盘 / 日 VWAP 代理的偏差。
  日 VWAP 代理 = Σamount / Σvolume_hand / 100（快照 amount/volume_hand
  为当日累计值，Σ 口径为近似；仅取 volume_hand > 0 的行）。
- ``latency_sec``：fill_time − 调仓日志委托提交 ts（按 symbol+side+数量
  匹配当日 execute=true 日志，取不晚于成交时间的最近一笔委托；
  匹配不到标 ``NO_ORDER_MATCH``）。
- ``vs VWAP 胜率``：dev_vwap_bps < 0（执行优于 VWAP）的笔数占比。

聚合输出 ``reports/execution_quality/<date>.json`` + ``.md``：当日笔数 /
平均滑点（分 BUY/SELL）/ vs VWAP 胜率 / 拒单率 / 孤儿占比；
``--days N`` 额外给 [date-N+1, date] 窗口的滚动汇总（逐笔 pooled）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

CST = timezone(timedelta(hours=8))
DB_PATH = "data/finance.duckdb"
TARGETS_DIR = Path("reports/sim_targets")
REBALANCE_LOG = Path("data/phone_sim_capture/rebalance")
REPORT_DIR = Path("reports/execution_quality")
# cn_quote_snapshot 自 2026-08-06 起才有数据，早于该日一律 VWAP_NA
SNAPSHOT_START = date(2026, 8, 6)


def _signed_bps(fill_price: float, benchmark: float, side: str) -> float:
    """带方向符号的偏差 bps：BUY 取 (fill-bench)/bench，SELL 取反。

    统一口径：**正值 = 执行劣于基准（成本），负值 = 优于基准**。
    """
    raw = (fill_price - benchmark) / benchmark * 1e4
    return round(raw if side == "BUY" else -raw, 2)


def _day_fills(con, day: date) -> list[dict]:
    """当日（Asia/Shanghai 口径）全部规范化成交，按成交时间排序。"""
    rows = con.execute(
        "SELECT fill_id, symbol, side, fill_time, fill_price, quantity, "
        "       fee_amount, candidate_id "
        "FROM broker_sim_normalized_fill "
        "WHERE CAST(timezone('Asia/Shanghai', fill_time) AS DATE) = ? "
        "ORDER BY fill_time",
        [day.isoformat()]).fetchall()
    cols = ["fill_id", "symbol", "side", "fill_time", "fill_price",
            "quantity", "fee_amount", "candidate_id"]
    return [dict(zip(cols, r)) for r in rows]


def _targets_prices_before(fill_time: datetime,
                           targets_dir: Path) -> Path | None:
    """fill_time 之前生成的最新 targets 快照 prices（孤儿决策价第一兜底）。

    文件名是 data_as_of 日期；用 generated_at <= fill_time 判定"已生成"，
    避免用到成交当晚才生成的新一批（如 08-14 的成交不能用 08-14 19:26
    生成的快照）。
    """
    if not targets_dir.exists():
        return None
    best: tuple[str, Path] | None = None
    for path in sorted(targets_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            gen = datetime.fromisoformat(data["generated_at"])
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            continue
        if gen <= fill_time and (best is None or gen.isoformat() > best[0]):
            best = (gen.isoformat(), path)
    return None if best is None else best[1]


def _prev_close(con, symbol: str, day: date) -> float | None:
    """前收盘兜底：当日快照 prev_close 优先，其次 daily_price 最近收盘。"""
    row = con.execute(
        "SELECT prev_close FROM cn_quote_snapshot "
        "WHERE symbol=? AND CAST(timezone('Asia/Shanghai', ts) AS DATE)=? "
        "AND prev_close IS NOT NULL ORDER BY ts DESC LIMIT 1",
        [symbol, day.isoformat()]).fetchone()
    if row and row[0]:
        return float(row[0])
    row = con.execute(
        "SELECT close FROM daily_price WHERE symbol=? AND date<? "
        "ORDER BY date DESC LIMIT 1",
        [symbol, day.isoformat()]).fetchone()
    return float(row[0]) if row and row[0] else None


def _decision_price(con, fill: dict, targets_dir: Path,
                    fill_date: date) -> tuple[float | None, str]:
    """决策价与来源口径：候选优先，孤儿按 targets → 前收盘 兜底。"""
    cid = fill.get("candidate_id")
    if cid:
        row = con.execute(
            "SELECT decision_price FROM strategy_candidate "
            "WHERE candidate_id=?", [cid]).fetchone()
        if row and row[0]:
            return float(row[0]), "CANDIDATE"
    # 孤儿成交（含候选缺 decision_price 的前冻结时代记录）：仍统计但标 ORPHAN
    targets_path = _targets_prices_before(fill["fill_time"], targets_dir)
    if targets_path is not None:
        prices = json.loads(
            targets_path.read_text(encoding="utf-8")).get("prices") or {}
        if prices.get(fill["symbol"]):
            return float(prices[fill["symbol"]]), "ORPHAN_TARGETS"
    prev = _prev_close(con, fill["symbol"], fill_date)
    if prev:
        return prev, "ORPHAN_PREV_CLOSE"
    return None, "NO_REFERENCE"


def _intraday_stats(con, symbol: str, day: date) -> dict:
    """当日快照聚合：open / prev_close / VWAP 代理；无快照返回 None。"""
    if day < SNAPSHOT_START:
        return {"open": None, "prev_close": None, "vwap": None}
    row = con.execute(
        "SELECT max(open), max(prev_close), "
        "       sum(amount) / nullif(sum(volume_hand), 0) / 100 "
        "FROM cn_quote_snapshot "
        "WHERE symbol=? AND CAST(timezone('Asia/Shanghai', ts) AS DATE)=? "
        "AND volume_hand > 0",
        [symbol, day.isoformat()]).fetchone()
    if not row or row[2] is None:
        return {"open": None, "prev_close": None, "vwap": None}
    return {"open": row[0], "prev_close": row[1], "vwap": round(row[2], 4)}


def _order_submit_ts(symbol: str, side: str, qty: float, day: date,
                     fill_time: datetime,
                     rebalance_dir: Path) -> datetime | None:
    """当日调仓日志里匹配的委托提交 ts（symbol+side+数量，不晚于成交）。"""
    if not rebalance_dir.exists():
        return None
    best: datetime | None = None
    for path in sorted(rebalance_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not data.get("execute"):
            continue
        for f in data.get("fills", []):
            if (f.get("symbol") == symbol and f.get("side") == side
                    and f.get("status") == "SUBMITTED"
                    and int(f.get("qty") or 0) == int(qty)
                    and f.get("ts")):
                ts = datetime.fromisoformat(f["ts"])
                if (ts.astimezone(CST).date() == day and ts <= fill_time
                        and (best is None or ts > best)):
                    best = ts
    return best


def _reject_stats(day: date, rebalance_dir: Path) -> dict:
    """当日拒单率：execute=true 日志 fills 数组里 REJECTED 占比。"""
    submitted = rejected = 0
    if rebalance_dir.exists():
        for path in sorted(rebalance_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not data.get("execute"):
                continue
            try:
                ts = datetime.fromisoformat(data["ts"])
            except (KeyError, ValueError):
                continue
            if ts.astimezone(CST).date() != day:
                continue
            for f in data.get("fills", []):
                if f.get("status") == "SUBMITTED":
                    submitted += 1
                elif f.get("status") == "REJECTED":
                    rejected += 1
    total = submitted + rejected
    return {"submitted": submitted, "rejected": rejected,
            "reject_rate": round(rejected / total, 4) if total else None}


def analyze_fill(con, fill: dict, *, targets_dir: Path = TARGETS_DIR,
                 rebalance_dir: Path = REBALANCE_LOG) -> dict:
    """单笔成交的 TCA 指标（纯计算，con 须只读连接）。"""
    fill_time = fill["fill_time"]
    if fill_time.tzinfo is None:
        fill_time = fill_time.replace(tzinfo=timezone.utc)
    fill_date = fill_time.astimezone(CST).date()
    symbol, side = fill["symbol"], fill["side"]
    price = float(fill["fill_price"])

    decision, basis = _decision_price(con, fill, targets_dir, fill_date)
    stats = _intraday_stats(con, symbol, fill_date)
    order_ts = _order_submit_ts(symbol, side, float(fill["quantity"]),
                                fill_date, fill_time, rebalance_dir)

    rec = {
        "fill_id": fill["fill_id"], "symbol": symbol, "side": side,
        "fill_time": fill_time.isoformat(),
        "fill_price": price, "quantity": fill["quantity"],
        "candidate_id": fill.get("candidate_id"),
        # 候选只产 BUY，SELL 必然孤儿；孤儿仍统计，决策价走兜底口径
        "orphan": fill.get("candidate_id") is None,
        "decision_price": decision, "decision_basis": basis,
        # 费用恒 NULL：成本分析标 MISSING_FEE，绝不补零
        "fee_status": "MISSING_FEE" if fill.get("fee_amount") is None else "OK",
        "slippage_decision_bps": (
            _signed_bps(price, decision, side) if decision else None),
        "dev_open_bps": (
            _signed_bps(price, stats["open"], side)
            if stats["open"] else None),
        "dev_prev_close_bps": (
            _signed_bps(price, stats["prev_close"], side)
            if stats["prev_close"] else None),
        "dev_vwap_bps": (
            _signed_bps(price, stats["vwap"], side)
            if stats["vwap"] else None),
        # cn_quote_snapshot 2026-08-06 起才有数，早于该日无 VWAP 口径
        "vwap_status": "OK" if stats["vwap"] else "VWAP_NA",
        "day_vwap": stats["vwap"], "day_open": stats["open"],
        "day_prev_close": stats["prev_close"],
        "order_ts": order_ts.isoformat() if order_ts else None,
        "latency_sec": (
            round((fill_time - order_ts).total_seconds(), 1)
            if order_ts else None),
        "latency_status": "OK" if order_ts else "NO_ORDER_MATCH",
    }
    return rec


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def summarize(records: list[dict], reject: dict) -> dict:
    """逐笔记录 → 聚合摘要（单日与滚动窗口共用，滚动即 pooled 逐笔）。"""
    slip = [r["slippage_decision_bps"] for r in records
            if r["slippage_decision_bps"] is not None]
    vwap_ok = [r for r in records if r["dev_vwap_bps"] is not None]
    lat = [r["latency_sec"] for r in records if r["latency_sec"] is not None]
    by_side = {}
    for side in ("BUY", "SELL"):
        vals = [r["slippage_decision_bps"] for r in records
                if r["side"] == side
                and r["slippage_decision_bps"] is not None]
        by_side[side] = {
            "count": sum(1 for r in records if r["side"] == side),
            "avg_slippage_decision_bps": _avg(vals),
        }
    return {
        "fills_count": len(records),
        "avg_slippage_decision_bps": _avg(slip),
        "by_side": by_side,
        "vwap_scored": len(vwap_ok),
        # 胜率 = 执行优于 VWAP（dev_vwap_bps < 0）的笔数占比
        "vwap_win_rate": (
            round(sum(1 for r in vwap_ok if r["dev_vwap_bps"] < 0)
                  / len(vwap_ok), 4) if vwap_ok else None),
        "avg_latency_sec": _avg(lat),
        "reject": reject,
        "orphan_ratio": (
            round(sum(1 for r in records if r["orphan"]) / len(records), 4)
            if records else None),
        "fee_note": "fee_amount 恒 NULL → MISSING_FEE，不补零",
    }


def build_daily(day: date, db_path: str | Path = DB_PATH, *,
                targets_dir: Path = TARGETS_DIR,
                rebalance_dir: Path = REBALANCE_LOG) -> dict:
    """单日执行质量报告（只读库连接）。"""
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        fills = _day_fills(con, day)
        records = [analyze_fill(con, f, targets_dir=targets_dir,
                                rebalance_dir=rebalance_dir) for f in fills]
    finally:
        con.close()
    reject = _reject_stats(day, rebalance_dir)
    return {
        "date": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sign_convention": "正值=执行劣于基准(成本)，负值=优于基准；SELL 已取反",
        "summary": summarize(records, reject),
        "fills": records,
    }


def build_rolling(end: date, days: int, db_path: str | Path = DB_PATH, *,
                  targets_dir: Path = TARGETS_DIR,
                  rebalance_dir: Path = REBALANCE_LOG) -> dict:
    """[end-days+1, end] 窗口滚动汇总（逐笔 pooled，非日均）。"""
    dailies = [build_daily(end - timedelta(days=i), db_path,
                           targets_dir=targets_dir,
                           rebalance_dir=rebalance_dir)
               for i in range(days)]
    dailies.sort(key=lambda d: d["date"])
    records = [r for d in dailies for r in d["fills"]]
    rej = {"submitted": sum(d["summary"]["reject"]["submitted"] for d in dailies),
           "rejected": sum(d["summary"]["reject"]["rejected"] for d in dailies)}
    total = rej["submitted"] + rej["rejected"]
    rej["reject_rate"] = round(rej["rejected"] / total, 4) if total else None
    return {
        "window": {"start": dailies[0]["date"], "end": end.isoformat(),
                   "days": days},
        "summary": summarize(records, rej),
        "per_day": [{"date": d["date"], **d["summary"]} for d in dailies],
    }


def render_md(report: dict) -> str:
    """单日报告 → Markdown（rolling 段可选）。"""
    s = report["summary"]
    lines = [
        f"# 执行质量（TCA） {report['date']}",
        "",
        f"- 口径：{report['sign_convention']}；bps = 万分之一",
        f"- 成交 **{s['fills_count']}** 笔（BUY {s['by_side']['BUY']['count']} / "
        f"SELL {s['by_side']['SELL']['count']}），孤儿占比 {s['orphan_ratio']}",
        f"- 平均决策滑点 **{s['avg_slippage_decision_bps']} bps**"
        f"（BUY {s['by_side']['BUY']['avg_slippage_decision_bps']} / "
        f"SELL {s['by_side']['SELL']['avg_slippage_decision_bps']}）",
        f"- vs VWAP 胜率 **{s['vwap_win_rate']}**（{s['vwap_scored']} 笔有 VWAP 口径）",
        f"- 拒单率 **{s['reject']['reject_rate']}**"
        f"（提交 {s['reject']['submitted']} / 拒 {s['reject']['rejected']}）",
        f"- 平均委托→成交时延 {s['avg_latency_sec']} s；费用口径：{s['fee_note']}",
        "",
        "## 逐笔明细",
        "",
        "| 时间 | 代码 | 方向 | 成交价 | 决策价 | 来源 | 滑点bps | vs VWAP | 时延s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report["fills"]:
        hhmmss = r["fill_time"][11:19]
        lines.append(
            f"| {hhmmss} | {r['symbol']} | {r['side']} | {r['fill_price']} "
            f"| {r['decision_price']} | {r['decision_basis']} "
            f"| {r['slippage_decision_bps']} "
            f"| {r['dev_vwap_bps'] if r['vwap_status'] == 'OK' else 'VWAP_NA'} "
            f"| {r['latency_sec'] if r['latency_status'] == 'OK' else 'NO_MATCH'} |")
    rolling = report.get("rolling")
    if rolling:
        rs = rolling["summary"]
        w = rolling["window"]
        lines += [
            "",
            f"## 滚动汇总（{w['start']} ~ {w['end']}，{w['days']} 个自然日）",
            "",
            f"- 成交 {rs['fills_count']} 笔，平均决策滑点 "
            f"{rs['avg_slippage_decision_bps']} bps，vs VWAP 胜率 "
            f"{rs['vwap_win_rate']}，拒单率 {rs['reject']['reject_rate']}，"
            f"孤儿占比 {rs['orphan_ratio']}",
        ]
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟盘执行质量分析（TCA）")
    parser.add_argument("--date", default=None,
                        help="分析日期 YYYY-MM-DD，默认今天（Asia/Shanghai）")
    parser.add_argument("--days", type=int, default=1,
                        help="滚动窗口自然日数（>1 时附滚动汇总）")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印，不写 reports/execution_quality/")
    args = parser.parse_args()
    day = (date.fromisoformat(args.date) if args.date
           else datetime.now(CST).date())

    report = build_daily(day, args.db)
    if args.days > 1:
        report["rolling"] = build_rolling(day, args.days, args.db)
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        return
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{day.isoformat()}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    (REPORT_DIR / f"{day.isoformat()}.md").write_text(
        render_md(report), encoding="utf-8")
    s = report["summary"]
    print(json.dumps({"status": "OK", "date": day.isoformat(),
                      "fills": s["fills_count"],
                      "avg_slippage_decision_bps": s["avg_slippage_decision_bps"],
                      "vwap_win_rate": s["vwap_win_rate"],
                      "reject_rate": s["reject"]["reject_rate"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
