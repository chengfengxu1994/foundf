#!/usr/bin/env python3
"""策略净值 vs 基准对比图（每日更新）。

回测口径 = 生产 multifactor_v5_valve.1 真实执行全约束：
kernel 五桶打分 + no_trade_band 0.02 + 池级 EP 阀门(floor 0.3)
+ 涨跌停一字板约束(limit_guard) + ±2% 偏离闸门(exec_gate)。
2022-01 首个交易日起归一；叠加模拟盘实盘 NAV（sim_nav_daily，
2026-08-06 起，独立归一）。

数据源：DuckDB daily_price（策略回测 + sh.000300 沪深300）、
FRED 本地缓存（标普500 data/external/sp500_fred.csv、纳斯达克
data/external/nasdaq_fred.csv，缺失时需宿主机 curl 预下载）。

用法：.venv/bin/python deploy/plot_nav_compare.py [--db data/finance.duckdb]
      [--out reports/nav_compare]
产物：nav_curve.png + summary.json（最新净值/区间收益汇总）。
只读数据库（read_only），与容器并存安全。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adhoc_year_backtest as bt  # noqa: E402

BASE_DATE = date(2022, 1, 1)      # 归一基准日（首个 >= 此日的交易日）
WARMUP_START = date(2020, 1, 1)   # ≥252 交易日 warm-up


def run_strategy_nav(con, end: date):
    """生产口径回测 → [(date, nav)]（复用 adhoc_year_backtest 加载器）。"""
    universe = bt.load_universe(con)
    prices = bt.load_prices(con, universe, WARMUP_START, end)
    basic = bt.load_basic(con, list(prices), WARMUP_START, end)
    basic_raw = bt.load_basic_raw(con, list(prices), WARMUP_START, end)
    bench_rows = con.execute(
        "SELECT date, close FROM daily_price WHERE symbol=? "
        "AND date BETWEEN ? AND ? ORDER BY date",
        [bt.BENCH_LOCAL, WARMUP_START, end],
    ).fetchall()
    bench = (np.array([r[0].toordinal() for r in bench_rows]),
             np.array([float(r[1]) for r in bench_rows]))
    cal = sorted({int(d[0][i]) for d in prices.values()
                  for i in range(len(d[0]))})
    pool_series = bt._pool_ep_series(basic, cal)
    hl = bt.load_hl(con, list(prices), WARMUP_START, end)
    nav = bt.run_backtest(
        prices, basic, date(2021, 1, 1), end, mode="kernel",
        no_trade_band=0.02, basic_raw=basic_raw, bench=bench,
        regime_valve=0.3, valve_state="pool", pool_series=pool_series,
        limit_guard=True, exec_gate=0.02, hl=hl,
    )
    return nav


def load_fred_csv(path: Path, col: str) -> dict[date, float]:
    """FRED 缓存 CSV → {date: close}；缺失返回 {}（不阻塞出图）。"""
    if not path.is_file():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = row.get(col, "")
            if v and v != ".":
                out[date.fromisoformat(row["observation_date"])] = float(v)
    return out


def align_ffill(series: dict[date, float], days: list[date]) -> np.ndarray:
    """把稀疏/异历法序列按日对齐到交易日轴（前值填充，起始前 NaN）。"""
    if not series:
        return np.full(len(days), np.nan)
    ds = np.array([d.toordinal() for d in sorted(series)])
    vs = np.array([series[date.fromordinal(int(o))] for o in ds])
    out = np.full(len(days), np.nan)
    for i, d in enumerate(days):
        j = int(np.searchsorted(ds, d.toordinal(), side="right")) - 1
        if j >= 0:
            out[i] = vs[j]
    return out


def normalize(vals: np.ndarray, days: list[date],
              base: date = BASE_DATE) -> np.ndarray:
    """首个 >= base 的有效值归一到 1.0。"""
    idx = [i for i, d in enumerate(days)
           if d >= base and np.isfinite(vals[i])]
    if not idx:
        return vals
    return vals / vals[idx[0]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/finance.duckdb")
    ap.add_argument("--out", default="reports/nav_compare")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    end = date.today()

    con = duckdb.connect(args.db, read_only=True)
    nav = run_strategy_nav(con, end)
    csi300_rows = con.execute(
        "SELECT date, close FROM daily_price WHERE symbol='sh.000300' "
        "AND date >= ? ORDER BY date", [BASE_DATE],
    ).fetchall()
    sim_rows = con.execute(
        "SELECT date, total_assets FROM sim_nav_daily ORDER BY date"
    ).fetchall()
    con.close()

    days = [d for d, _ in nav if d >= BASE_DATE]
    strat = normalize(np.array([n for d, n in nav if d >= BASE_DATE]), days)

    csi300 = normalize(align_ffill(
        {r[0]: float(r[1]) for r in csi300_rows}, days), days)
    sp500 = normalize(align_ffill(
        load_fred_csv(Path("data/external/sp500_fred.csv"), "SP500"),
        days), days)
    nasdaq = normalize(align_ffill(
        load_fred_csv(Path("data/external/nasdaq_fred.csv"), "NASDAQCOM"),
        days), days)

    fig, ax = plt.subplots(figsize=(12, 6.5))
    ax.plot(days, strat, lw=2.0, color="#1f5fb4",
            label="Strategy v5_valve.1 (backtest, realistic exec)")
    ax.plot(days, csi300, lw=1.2, color="#888888", label="CSI 300")
    ax.plot(days, sp500, lw=1.2, color="#2ca02c", label="S&P 500")
    ax.plot(days, nasdaq, lw=1.2, color="#ff7f0e", label="NASDAQ")
    sim_summary = None
    if sim_rows:
        sd = [r[0] for r in sim_rows]
        sv = np.array([float(r[1]) for r in sim_rows])
        sv = sv / sv[0]
        # 与策略曲线在模拟盘起始日对接(视觉同尺度, 直接读跟踪偏差)
        anchor = next((i for i, d in enumerate(days) if d >= sd[0]), None)
        sv_plot = sv * strat[anchor] if anchor is not None else sv
        ax.plot(sd, sv_plot, lw=1.8,
                color="#d62728", marker="o", ms=3,
                label="Sim account (live, anchored to strategy 2026-08-06)")
        sim_summary = {
            "since": sd[0].isoformat(), "latest": sd[-1].isoformat(),
            "return": round(float(sv[-1] - 1), 4),
            "total_assets": float(sim_rows[-1][1]),
        }
    ax.axhline(1.0, lw=0.6, color="#bbbbbb")
    ax.set_title(f"Daily NAV vs benchmarks (base {days[0].isoformat()} = 1.0)"
                 f" — generated {end.isoformat()}")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    png = out_dir / "nav_curve.png"
    fig.savefig(png, dpi=140)

    summary = {
        "generated_at": end.isoformat(),
        "base_date": days[0].isoformat(),
        "latest": days[-1].isoformat(),
        "total_return_since_base": {
            "strategy_v5_valve": round(float(strat[-1] - 1), 4),
            "csi300": round(float(csi300[-1] - 1), 4),
            "sp500": round(float(sp500[-1] - 1), 4) if np.isfinite(sp500[-1]) else None,
            "nasdaq": round(float(nasdaq[-1] - 1), 4) if np.isfinite(nasdaq[-1]) else None,
        },
        "sim_live": sim_summary,
        "caliber": "kernel + band 0.02 + pool-EP valve(0.3) + limit_guard "
                   "+ exec_gate ±2%; 月末T+1; 17.5bps+印花税",
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"图已保存: {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
