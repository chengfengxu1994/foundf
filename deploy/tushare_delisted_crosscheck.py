#!/usr/bin/env python3
"""tushare 退市清单 × stock_registry 交叉校验补跑（一次性/补漏）。

背景：2026-08-13 build_stock_registry 运行时 tushare stock_basic(list_status=D)
限频（1 次/分钟）重试 3 次耗尽，registry_<date>.md 第 2 节降级为「仅 baostock
单源」。本脚本用更宽松的重试节奏补跑交叉校验，并就地更新报告第 2 节。

- 只读 stock_registry；只调 tushare 一次成功即走；绝不改任何业务数据
- token 从项目根 .env 解析（复用 build_stock_registry._load_tushare_token）
- 用法：python3 deploy/tushare_delisted_crosscheck.py [--report reports/pit_universe/registry_2026-08-13.md]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import duckdb  # noqa: E402

import deploy.build_stock_registry as reg  # noqa: E402

# tushare stock_basic 限频 1 次/小时（list_status=D 实测）：失败重试本身
# 也消耗配额并可能重置计时窗——2026-08-14 三次 75s 节奏重试批次全部
# 自锁。本脚本单次尝试，重试交给外层 cron 按 >1 小时间隔调度。
reg.TUSHARE_RETRY = 0


def load_registry_rows(db_path: str) -> list[dict]:
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT symbol, code_name, list_status, out_date FROM stock_registry"
        ).fetchall()
    finally:
        con.close()
    return [
        {"symbol": r[0], "name": r[1], "list_status": r[2], "out_date": r[3]}
        for r in rows
    ]


def delist_date_mismatches(
    registry_rows: list[dict], tushare_delisted: dict[str, dict]
) -> list[str]:
    """两边都标退市但退市日不一致的 symbol 清单。"""
    out = []
    for r in registry_rows:
        if r["list_status"] != "DELISTED":
            continue
        ts = tushare_delisted.get(r["symbol"])
        if ts is None:
            continue
        bs_date = r["out_date"]
        if isinstance(bs_date, str):
            bs_date = date.fromisoformat(bs_date)
        ts_date = ts.get("delist_date")
        if bs_date and ts_date and bs_date != ts_date:
            out.append(f"{r['symbol']}(baostock={bs_date}, tushare={ts_date})")
    return sorted(out)


def render_section2(cross: dict, mismatches: list[str]) -> list[str]:
    return [
        "## 2. tushare 退市清单交叉校验（只报差异，未自动改数）",
        "",
        f"- 状态：成功（{date.today().isoformat()} 补跑，重试间隔 75s）",
        f"- baostock 退市数：{cross['baostock_delisted_count']}；"
        f"tushare 退市数：{cross['tushare_delisted_count']}",
        f"- tushare 退市但 baostock 仍标 LISTED（{len(cross['tushare_only_bs_listed'])}）："
        f"{', '.join(cross['tushare_only_bs_listed']) or '无'}",
        f"- tushare 退市但 baostock 全量无此票（{len(cross['tushare_only_bs_absent'])}）："
        f"{', '.join(cross['tushare_only_bs_absent']) or '无'}",
        f"- baostock 退市但 tushare D 清单没有（{len(cross['baostock_only'])}）："
        f"{', '.join(cross['baostock_only']) or '无'}",
        f"- 退市日不一致（{len(mismatches)}）："
        f"{', '.join(mismatches) or '无'}",
    ]


def replace_section2(report_path: Path, new_section: list[str]) -> None:
    text = report_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("## 2. "))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    lines[start:end] = new_section + [""]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument(
        "--report",
        default="reports/pit_universe/registry_2026-08-13.md",
    )
    args = parser.parse_args()

    rows = load_registry_rows(args.db)
    print(f"stock_registry 行数：{len(rows)}")

    token = reg._load_tushare_token()
    tushare_delisted = reg.fetch_tushare_delisted(token)
    print(f"tushare 退市清单：{len(tushare_delisted)}")

    cross = reg.cross_check_delisted(rows, tushare_delisted)
    mismatches = delist_date_mismatches(rows, tushare_delisted)
    for line in render_section2(cross, mismatches):
        print(line)

    report_path = Path(args.report)
    if report_path.exists():
        replace_section2(report_path, render_section2(cross, mismatches))
        print(f"已更新报告第 2 节：{report_path}")
    else:
        print(f"⚠ 报告不存在，仅打印不落盘：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
