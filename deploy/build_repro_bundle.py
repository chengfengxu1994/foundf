"""回测可复现包(repro bundle)导出器。

背景: 外部审查(Codex P2)指出策略复现包不完整。本脚本**复用**
`adhoc_year_backtest` 的可 import 函数重跑同一回测(同参数、同口径),
导出完整复现证据, 补齐 docs/guides/MULTIFACTOR_STRATEGY_SPEC.md §9 第 8 条
承认的缺口: 精确宇宙清单、原始数据快照哈希、逐月因子截面/最终排名/入选
标的、逐笔调仓账本(成交价/费用)、完整日度 NAV。

只读数据库(read_only=True), 不修改任何生产产物。

用法:
  python3 deploy/build_repro_bundle.py [--db data/finance.duckdb]
      [--out reports/adhoc_backtest/repro_bundle] [--dry-run]
产物(--dry-run 时不写盘, 只打印 manifest 与摘要):
  manifest.json       schema/生成时间/git commit/全部配置/数据快照哈希/
                      代码哈希/逐年结果摘要
  universe.json       每只标的 {symbol, first_date, last_date, rows, source}
                      + 每期实际参与打分的标的数
  factor_panels.jsonl 每行一个调仓打分日的完整截面 + selected 前 N 标的
  trades.jsonl        逐笔调仓账本(BUY/SELL/CARRY, 成交价/股数/费用)
  nav_daily.csv       date,nav 全序列(warm-up 后全部评价期)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adhoc_year_backtest as bt  # noqa: E402

# 与 adhoc_year_backtest.main 完全一致的回测参数(改动即失配, 禁止分叉)
LOAD_START = date(2020, 1, 1)   # warm-up 起点(≥252 交易日 12m 动量)
EVAL_START = date(2021, 1, 1)   # 交易/净值起点
EVAL_YEAR_START = 2022          # 逐年评价起始年(2021 为基期)
SPEC_ID = "adhoc_year_compare.v1"
# 一致性基准: 目录下最新的 year_compare_2022_*.json(v3 口径),
# 由 adhoc_year_backtest --end-year <今年> 生成, 周末 cron 先于本脚本刷新
YEAR_COMPARE_GLOB = "year_compare_2022_*.json"
YEAR_COMPARE_DIR = Path("reports/adhoc_backtest")


def _latest_eval_end(con: duckdb.DuckDBPyConnection) -> date:
    """评价终点 = 库内最新交易日(截止数据可得的"今天")。"""
    row = con.execute("SELECT MAX(date) FROM daily_price").fetchone()
    latest = row[0]
    if latest is None:
        raise RuntimeError("daily_price 为空, 无法确定评价终点")
    return min(latest, date.today())


def _year_compare_path() -> Path | None:
    """取最新的 year_compare_2022_*.json 基准文件。"""
    candidates = sorted(YEAR_COMPARE_DIR.glob(YEAR_COMPARE_GLOB))
    return candidates[-1] if candidates else None


def _json_default(o):
    """numpy 标量一律落为 Python float, 保证 JSON 可序列化。"""
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


def _fmt_num(v) -> str:
    """快照哈希行字段格式: None → "null", 数值 → repr(float) 全精度。"""
    return "null" if v is None else repr(float(v))


def hash_lines(lines: list[str]) -> str:
    """行集合排序拼接后的 sha256(与输入顺序无关, 对内容敏感)。"""
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def snapshot_hashes(price_lines: list[str], basic_lines: list[str],
                    bench_lines: list[str]) -> dict:
    """三部分各自哈希再合哈希, 字段名 prices/basic/bench/snapshot_hash。"""
    prices_hash = hash_lines(price_lines)
    basic_hash = hash_lines(basic_lines)
    bench_hash = hash_lines(bench_lines)
    combined = hashlib.sha256(
        (prices_hash + basic_hash + bench_hash).encode("utf-8")
    ).hexdigest()
    return {
        "prices_hash": prices_hash, "basic_hash": basic_hash,
        "bench_hash": bench_hash, "snapshot_hash": combined,
        "price_rows": len(price_lines), "basic_rows": len(basic_lines),
        "bench_rows": len(bench_lines),
    }


def collect_snapshot(con: duckdb.DuckDBPyConnection,
                     price_symbols: list[str], basic_symbols: list[str],
                     eval_end: date) -> dict:
    """从库中取出**实际参与回测**的数据行并计算快照哈希(只读)。"""
    price_lines = [
        f"{s}|{d}|{_fmt_num(o)}|{_fmt_num(c)}"
        for s, d, o, c in con.execute(
            "SELECT symbol, date, open, close FROM daily_price "
            "WHERE symbol IN (SELECT * FROM UNNEST(?::VARCHAR[])) "
            "AND date BETWEEN ? AND ?",
            [price_symbols, LOAD_START, eval_end],
        ).fetchall()
    ]
    basic_lines = [
        f"{s}|{d}|{_fmt_num(pe)}|{_fmt_num(pb)}"
        for s, d, pe, pb in con.execute(
            "SELECT symbol, date, pe_ttm, pb FROM daily_basic "
            "WHERE symbol IN (SELECT * FROM UNNEST(?::VARCHAR[])) "
            "AND date BETWEEN ? AND ?",
            [basic_symbols, LOAD_START, eval_end],
        ).fetchall()
    ]
    bench_lines = [
        f"{s}|{d}|{_fmt_num(o)}|{_fmt_num(c)}"
        for s, d, o, c in con.execute(
            "SELECT symbol, date, open, close FROM daily_price "
            "WHERE symbol=? AND date BETWEEN ? AND ?",
            [bt.BENCH_LOCAL, EVAL_START, eval_end],
        ).fetchall()
    ]
    return snapshot_hashes(price_lines, basic_lines, bench_lines)


def group_panels(panel: list[dict], top_n: int) -> list[dict]:
    """把逐标的 panel 记录按打分日归组, 并给出每期 selected 前 top_n 标的。

    panel 记录与 run_backtest 内部 scores 字典同序追加, 稳定排序下
    selected 与回测实际调仓名单一致(含并列处理)。
    """
    groups: list[dict] = []
    for rec in panel:
        if not groups or groups[-1]["date"] != rec["date"]:
            groups.append({"date": rec["date"], "records": [], "selected": []})
        groups[-1]["records"].append(rec)
    for g in groups:
        ranked = sorted(g["records"], key=lambda r: r["composite"], reverse=True)
        g["selected"] = [r["symbol"] for r in ranked[:top_n]]
    return groups


def _git_commit() -> str | None:
    """取当前 git 短哈希; 失败(非仓库/无 git)记 null。"""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def _code_hash() -> dict:
    """回测脚本与本导出器两文件内容的 sha256(改动即失配)。"""
    here = Path(__file__).resolve().parent
    h1 = hashlib.sha256((here / "adhoc_year_backtest.py").read_bytes()).hexdigest()
    h2 = hashlib.sha256((here / "build_repro_bundle.py").read_bytes()).hexdigest()
    combined = hashlib.sha256((h1 + h2).encode("utf-8")).hexdigest()
    return {"adhoc_year_backtest": h1, "build_repro_bundle": h2,
            "code_hash": combined}


def build_manifest(snapshot: dict, strat_stats: dict, nav,
                   universe_size: int, n_periods: int, n_trades: int,
                   eval_end: date) -> dict:
    """组装 manifest(schema foundf.repro_bundle.v1)。"""
    from datetime import datetime, timezone
    years = [y for y in range(EVAL_YEAR_START, eval_end.year + 1)
             if [n for d, n in nav if d.year == y]]
    return {
        "schema": "foundf.repro_bundle.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "strategy_spec_id": SPEC_ID,
        "config": {
            "top_n": bt.TOP_N, "min_history": bt.MIN_HISTORY,
            "cost_bps_per_side": 17.5, "stamp_bps_sell": 5.0,
            "weights": {"value": bt.W_VALUE, "mom": bt.W_MOM, "risk": bt.W_RISK},
            "warmup_start": LOAD_START.isoformat(),
            "eval_start": EVAL_START.isoformat(),
            "eval_end": eval_end.isoformat(),
            "rebalance": "月末T打分,T+1开盘价调仓,满仓top_n等权",
            "bench_local": bt.BENCH_LOCAL,
        },
        "data_snapshot_hash": snapshot,
        "code_hash": _code_hash(),
        "universe_size": universe_size,
        "rebalance_periods": n_periods,
        "trade_records": n_trades,
        "results": {
            "strategy": strat_stats,
            "nav_end": {str(y): round([n for d, n in nav if d.year == y][-1], 4)
                        for y in years},
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/finance.duckdb")
    ap.add_argument("--out", default="reports/adhoc_backtest/repro_bundle")
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算 manifest 与摘要, 不写任何文件")
    args = ap.parse_args()

    # 1) 只读加载, 与 adhoc_year_backtest.main 同参数同口径
    con = duckdb.connect(args.db, read_only=True)
    eval_end = _latest_eval_end(con)
    years = tuple(range(EVAL_YEAR_START, eval_end.year + 1))
    print(f"评价区间: {EVAL_START} ~ {eval_end} (逐年 {years[0]}-{years[-1]})")
    universe = bt.load_universe(con)
    prices = bt.load_prices(con, universe, LOAD_START, eval_end)
    basic = bt.load_basic(con, list(prices), LOAD_START, eval_end)
    sources = dict(con.execute(
        "SELECT symbol, MIN(source) FROM daily_price "
        "WHERE symbol IN (SELECT * FROM UNNEST(?::VARCHAR[])) GROUP BY symbol",
        [list(prices)],
    ).fetchall())
    snapshot = collect_snapshot(con, list(prices), list(basic), eval_end)
    con.close()
    print(f"宇宙: {len(universe)} 只; 满足最小历史: {len(prices)} 只; "
          f"有估值: {len(basic)} 只")

    # 2) 重跑同一回测, 带出逐笔账本与因子截面
    #    显式 mode="legacy_v3": 可复现包锁定 v3 口径(回归校验能力不变),
    #    kernel 模式属对照实验, 不进复现包
    ledger: list[dict] = []
    panel: list[dict] = []
    nav = bt.run_backtest(prices, basic, EVAL_START, eval_end,
                          ledger=ledger, panel=panel, mode="legacy_v3")
    strat = bt.annual_stats(nav, years=years)
    groups = group_panels(panel, bt.TOP_N)

    # 3) 一致性校验: 与最新 year_compare_2022_*.json(v3 口径基准)逐年比对
    #    基准未覆盖的新年份只提示, 不判不一致(等周末 cron 先刷新基准)
    ref_path = _year_compare_path()
    consistency = "未找到基准文件, 跳过"
    if ref_path is not None:
        ref = json.loads(ref_path.read_text(encoding="utf-8"))["strategy"]
        common = sorted(set(ref) & {str(y) for y in strat})
        mismatched = [y for y in common
                      if ref[y] != {k: v for k, v in strat[int(y)].items()}]
        new_years = sorted({str(y) for y in strat} - set(ref))
        if mismatched:
            consistency = (f"不一致! 年份 {mismatched}; "
                           f"基准={ref} 本次={strat}")
        else:
            consistency = f"一致(比对 {common[0]}-{common[-1]} 共 {len(common)} 年)"
            if new_years:
                consistency += f"; 基准未覆盖新年份 {new_years}(待刷新)"

    # 4) 组装产物
    manifest = build_manifest(snapshot, strat, nav, len(prices),
                              len(groups), len(ledger), eval_end)
    universe_doc = {
        "total": len(prices),
        "symbols": [
            {
                "symbol": s,
                "first_date": date.fromordinal(int(d[0])).isoformat(),
                "last_date": date.fromordinal(int(d[-1])).isoformat(),
                "rows": len(d), "source": sources.get(s),
            }
            for s, (d, _o, _c) in sorted(prices.items())
        ],
        # 每个调仓打分日实际参与打分的标的数
        "scored_counts": [{"date": g["date"], "scored": len(g["records"])}
                          for g in groups],
    }
    total_fee = sum(e["fee"] for e in ledger)

    # 5) 写盘(--dry-run 跳过)
    if not args.dry_run:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1,
                       default=_json_default) + "\n", encoding="utf-8")
        (out / "universe.json").write_text(
            json.dumps(universe_doc, ensure_ascii=False, indent=1,
                       default=_json_default) + "\n", encoding="utf-8")
        with (out / "factor_panels.jsonl").open("w", encoding="utf-8") as f:
            for g in groups:
                f.write(json.dumps(g, ensure_ascii=False, default=_json_default)
                        + "\n")
        with (out / "trades.jsonl").open("w", encoding="utf-8") as f:
            for e in ledger:
                f.write(json.dumps(e, ensure_ascii=False, default=_json_default)
                        + "\n")
        with (out / "nav_daily.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "nav"])
            for d, n in nav:
                w.writerow([d.isoformat(), repr(n)])
        print(f"产物已写入 {out}/")

    # 6) 摘要
    print(json.dumps(manifest, ensure_ascii=False, indent=1,
                     default=_json_default))
    print("==== 摘要 ====")
    print(f"宇宙大小: {len(prices)} 只")
    print(f"调仓期数: {len(groups)}")
    print(f"总交易笔数: {len(ledger)} (含 CARRY "
          f"{sum(1 for e in ledger if e['side'] == 'CARRY')} 笔)")
    print(f"总费用占初始净值比: {total_fee / 1.0:.4%} (总费用 {total_fee:.6f})")
    for y, s in strat.items():
        print(f"  {y}: return {s['return']:+.2%} sharpe_rf0 {s['sharpe_rf0']}")
    print(f"与 v3 口径({ref_path or YEAR_COMPARE_GLOB})逐年收益一致性: {consistency}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
