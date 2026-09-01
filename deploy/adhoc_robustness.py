"""过拟合压力测试: 分时期回测(regime) + 参数扰动 + 宇宙 bootstrap。

回答「2022-2024 对照回测是否过拟合」三组证据:

1. **分时期回测**: 同一套参数(整数先验)在多个历史 regime 的表现——
   2015-2019(去杠杆/贸易战)、2020-2021(核心资产泡沫与破裂)、
   2025-2026YTD。**注意: 这不是真正样本外**——这些历史在策略设计与
   解释时全部可见, 只是参数未在任何窗口拟合。真正样本外必须预注册
   冻结策略+使用冻结日后新发生数据(模拟盘 forward 观察承担此角色)。
2. **参数扰动**: top_n {4,10}、季频调仓、单桶独立——若只有精确配置
   才赚钱即过拟合; 若邻域配置同方向赚钱, 说明赚的是因子暴露而非参数运气。
3. **宇宙 bootstrap**: 20 次随机 70% 子宇宙重跑 2022-2024, 看三年累计
   收益分布——只能衡量对特定股票的依赖度, 不能消除幸存者偏差
   (子样本抽自同一幸存者池)。

用法: python3 deploy/adhoc_robustness.py [--db data/finance.duckdb]
产物: reports/adhoc_backtest/robustness.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import adhoc_year_backtest as bt


def period_stats(nav, y0: int, y1: int) -> dict:
    pts = [(d, n) for d, n in nav if y0 <= d.year <= y1]
    prev = [n for d, n in nav if d < pts[0][0]]
    base = prev[-1] if prev else pts[0][1]
    navs = np.array([base] + [n for _d, n in pts])
    dr = np.diff(navs) / navs[:-1]
    vol = float(np.std(dr) * np.sqrt(252))
    years = (pts[-1][0] - pts[0][0]).days / 365.25
    cum = navs[-1] / base - 1
    ann = (navs[-1] / base) ** (1 / years) - 1 if years > 0 else 0.0
    peak, mdd = base, 0.0
    for _d, n in pts:
        peak = max(peak, n)
        mdd = min(mdd, n / peak - 1)
    return {
        "cum_return": round(cum, 4), "ann_return": round(ann, 4),
        "ann_vol": round(vol, 4),
        "sharpe_rf0": round(ann / vol, 2) if vol > 0 else None,
        "max_drawdown": round(mdd, 4),
    }


def bench_period(series: dict[date, float], y0: int, y1: int) -> dict:
    pts = sorted((d, v) for d, v in series.items() if y0 <= d.year <= y1)
    prev = sorted((d, v) for d, v in series.items() if d < pts[0][0])
    base = prev[-1][1] if prev else pts[0][1]
    navs = np.array([base] + [v for _d, v in pts])
    dr = np.diff(navs) / navs[:-1]
    vol = float(np.std(dr) * np.sqrt(252))
    years = (pts[-1][0] - pts[0][0]).days / 365.25
    ann = (navs[-1] / base) ** (1 / years) - 1 if years > 0 else 0.0
    peak, mdd = base, 0.0
    for _d, v in pts:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"cum_return": round(navs[-1] / base - 1, 4),
            "ann_return": round(ann, 4),
            "sharpe_rf0": round(ann / vol, 2) if vol > 0 else None,
            "max_drawdown": round(mdd, 4)}


def subset(prices, basic, symbols):
    p = {s: prices[s] for s in symbols if s in prices}
    b = {s: basic[s] for s in symbols if s in basic}
    return p, b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/finance.duckdb")
    ap.add_argument("--out", default="reports/adhoc_backtest/robustness.json")
    args = ap.parse_args()

    start, end = date(2014, 1, 1), date(2026, 8, 7)
    load_start = date(2012, 1, 1)  # 最早评估窗(2015)的 252 日 warm-up
    con = duckdb.connect(args.db, read_only=True)
    universe = bt.load_universe(con)
    prices = bt.load_prices(con, universe, load_start, end)
    basic = bt.load_basic(con, list(prices), load_start, end)
    bench_rows = con.execute(
        "SELECT date, close FROM daily_price WHERE symbol=? AND date BETWEEN ? AND ? "
        "ORDER BY date", [bt.BENCH_LOCAL, start, end]).fetchall()
    con.close()
    csi300 = {d: float(c) for d, c in bench_rows}
    # 库内 sh.000300 仅 2020 年起; 2014-2019 由 baostock 补拉的外部缓存合并
    ext = Path("data/external/csi300_2014_2019.csv")
    if ext.exists():
        for line in ext.read_text().splitlines()[1:]:
            d, c = line.split(",")
            csi300.setdefault(date.fromisoformat(d), float(c))
    sp500 = bt.fetch_sp500()
    print(f"宇宙 {len(prices)} 只, 区间 {start}~{end}")

    result: dict = {"schema": "foundf.adhoc_robustness.v2",
                    "note": "参数为整数先验未拟合; 分时期回测≠真正样本外"
                            "(历史在策略设计时可见); bootstrap 不消除幸存者偏差"}

    # ── 1. 盲数据 regime 窗口 ─────────────────────────────
    windows = {"2015-2019": (2015, 2019), "2020-2021": (2020, 2021),
               "2022-2024": (2022, 2024), "2025-2026YTD": (2025, 2026)}
    regimes = {}
    for name, (y0, y1) in windows.items():
        nav = bt.run_backtest(prices, basic, start, date(y1, 12, 31)
                              if y1 < 2026 else end)
        regimes[name] = {
            "strategy": period_stats(nav, y0, y1),
            "csi300": bench_period(csi300, y0, y1),
            "sp500": bench_period(sp500, y0, y1),
        }
        print(f"regime {name}: strat {regimes[name]['strategy']['ann_return']:+.1%} "
              f"vs csi300 {regimes[name]['csi300']['ann_return']:+.1%}")
    result["regime_windows"] = regimes

    # ── 2. 参数扰动(2022-2024) ────────────────────────────
    s22, e24 = date(2021, 1, 1), date(2024, 12, 31)
    perturb = {}
    variants = {
        "base_top6_monthly": {},
        "top4": {"top_n": 4},
        "top10": {"top_n": 10},
        "quarterly": {"freq": "Q"},
        "value_only": {"weights": (1.0, 0.0, 0.0)},
        "momentum_only": {"weights": (0.0, 1.0, 0.0)},
        "lowrisk_only": {"weights": (0.0, 0.0, 1.0)},
        "no_value": {"weights": (0.0, 0.5, 0.5)},
        "double_cost": {"cost": 35 / 1e4},
    }
    for name, kw in variants.items():
        nav = bt.run_backtest(prices, basic, s22, e24, **kw)
        perturb[name] = period_stats(nav, 2022, 2024)
        print(f"perturb {name}: cum {perturb[name]['cum_return']:+.1%} "
              f"sharpe {perturb[name]['sharpe_rf0']}")
    result["perturbations_2022_2024"] = perturb

    # ── 3. 宇宙 bootstrap(2022-2024, 20 次 70% 抽样) ─────
    rng = random.Random(42)
    cums, sharpes = [], []
    syms = sorted(prices)
    for i in range(20):
        sample = rng.sample(syms, int(len(syms) * 0.7))
        p, b = subset(prices, basic, sample)
        nav = bt.run_backtest(p, b, s22, e24)
        st = period_stats(nav, 2022, 2024)
        cums.append(st["cum_return"])
        sharpes.append(st["sharpe_rf0"])
    result["universe_bootstrap_2022_2024"] = {
        "trials": 20, "sample_ratio": 0.7,
        "cum_return_min": round(min(cums), 4),
        "cum_return_median": round(float(np.median(cums)), 4),
        "cum_return_max": round(max(cums), 4),
        "sharpe_min": round(min(sharpes), 2),
        "sharpe_median": round(float(np.median(sharpes)), 2),
    }
    print(f"bootstrap: cum median {result['universe_bootstrap_2022_2024']['cum_return_median']:+.1%} "
          f"[{min(cums):+.1%}, {max(cums):+.1%}]")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
