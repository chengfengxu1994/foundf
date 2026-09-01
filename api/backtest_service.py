"""Read-only backtest comparison projection for the dashboard.

组装 repro bundle（策略回测逐年结果 + 逐笔交易账本 + 日净值序列）、
year_compare 基准对照与 nav_compare 生产口径摘要。任何输入缺失或损坏时
对应段 fail-closed 为 ``status: BACKTEST_DATA_MISSING``，绝不抛 500。

口径提示：trades.jsonl 的 shares/notional/fee 是 NAV=1 归一口径
（非真实股数），展示层必须标注，避免误读为实盘金额。

逐笔增强（best-effort，失败即缺省不阻塞）：
- ``display_name``：stock_registry.code_name（需只读 DuckDB）
- ``reason``：BUY 取 factor_panels 当期截面因子分（价值/动量/风险），
  SELL 为月末再平衡调出
- ``nav_daily.benchmark``：沪深300 同区间归一净值（需只读 DuckDB）
"""

from __future__ import annotations

import bisect
import csv
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "foundf.backtest_compare.v2"
MISSING = "BACKTEST_DATA_MISSING"
BENCH_SYMBOL = "sh.000300"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _load_manifest(bundle_dir: Path) -> dict[str, Any]:
    raw = _load_json(bundle_dir / "manifest.json")
    if raw is None:
        return {"status": MISSING}
    hashes = raw.get("data_snapshot_hash") or {}
    return {
        "status": "OK",
        "generated_at": raw.get("generated_at"),
        "git_commit": raw.get("git_commit"),
        "strategy_spec_id": raw.get("strategy_spec_id"),
        "universe_size": raw.get("universe_size"),
        "rebalance_periods": raw.get("rebalance_periods"),
        "trade_records": raw.get("trade_records"),
        "config": raw.get("config") or {},
        "results": raw.get("results") or {},
        "prices_hash_short": str(hashes.get("prices_hash") or "")[:12],
        "code_hash_short": str(raw.get("code_hash") or "")[:12],
    }


def _load_year_compare(reports_root: Path) -> dict[str, Any]:
    """读最新的 year_compare_2022_*.json（基准随评价区间延伸滚动）。"""
    directory = reports_root / "adhoc_backtest"
    candidates = sorted(directory.glob("year_compare_2022_*.json")) \
        if directory.is_dir() else []
    raw = _load_json(candidates[-1]) if candidates else None
    if raw is None:
        return {"status": MISSING}
    return {
        "status": "OK",
        "source_file": candidates[-1].name,
        "disclaimer": raw.get("disclaimer"),
        "universe_size": raw.get("universe_size"),
        "cost_bps_per_side": raw.get("cost_bps_per_side"),
        "rebalance": raw.get("rebalance"),
        "strategy": raw.get("strategy") or {},
        "csi300": raw.get("csi300") or {},
        "sp500": raw.get("sp500") or {},
        "nav_end": raw.get("nav_end") or {},
    }


def _load_factor_panels(bundle_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """factor_panels.jsonl → {panel_date: {symbol: record}}；缺失返回空。"""

    path = bundle_dir / "factor_panels.jsonl"
    panels: dict[str, dict[str, dict[str, Any]]] = {}
    if not path.is_file():
        return panels
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                panel = json.loads(line)
                date = str(panel.get("date") or "")
                if not date:
                    continue
                panels[date] = {
                    str(r.get("symbol")): r
                    for r in (panel.get("records") or [])
                    if r.get("symbol")
                }
    except (json.JSONDecodeError, OSError):
        return {}
    return panels


def _trade_reason(
    trade: dict[str, Any],
    panel_dates: list[str],
    panels: dict[str, dict[str, dict[str, Any]]],
) -> str | None:
    """生成单笔交易原因；BUY 取当期截面因子分，SELL 为调出。"""

    if not panel_dates:
        return None
    exec_date = str(trade.get("exec_date") or "")
    # 调仓执行日 = 打分日 T+1：取不晚于执行日的最近一期面板
    idx = bisect.bisect_right(panel_dates, exec_date) - 1
    if idx < 0:
        return None
    panel = panels.get(panel_dates[idx]) or {}
    if trade.get("side") == "SELL":
        return f"月末再平衡调出（{panel_dates[idx]} 期跌出持仓）"
    record = panel.get(str(trade.get("symbol")))
    if not record:
        return None
    def fmt(key: str) -> str:
        value = record.get(key)
        return f"{float(value):.2f}" if isinstance(value, (int, float)) else "—"
    return (f"综合 {fmt('composite')} · 价值 {fmt('value_score')}"
            f" · 动量分位 {fmt('mom_rank')} · 风险 {fmt('risk_score')}")


def _load_names(db_path: Path | None, symbols: set[str]) -> dict[str, str]:
    """stock_registry 查股票名称；DB 不可用返回空（best-effort）。"""

    if db_path is None or not symbols:
        return {}
    try:
        import duckdb
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT symbol, code_name FROM stock_registry "
                "WHERE symbol IN (SELECT unnest(?::VARCHAR[]))",
                [sorted(symbols)],
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    return {str(s): str(n) for s, n in rows if n}


def _load_benchmark(db_path: Path | None,
                    dates: list[str]) -> dict[str, Any]:
    """沪深300 同区间归一净值，按策略交易日对齐（前向填充）。"""

    if db_path is None or not dates:
        return {"status": MISSING, "series": []}
    try:
        import duckdb
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT CAST(date AS VARCHAR) AS d, close FROM daily_price "
                "WHERE symbol = ? AND date BETWEEN ? AND ? ORDER BY date",
                [BENCH_SYMBOL, dates[0], dates[-1]],
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {"status": MISSING, "series": []}
    if not rows:
        return {"status": MISSING, "series": []}
    close_by_date = {str(d): float(c) for d, c in rows if c}
    bench_dates = sorted(close_by_date)
    if not bench_dates:
        return {"status": MISSING, "series": []}
    base = close_by_date[bench_dates[0]]
    series: list[dict[str, Any]] = []
    last: float | None = None
    for d in dates:
        idx = bisect.bisect_right(bench_dates, d) - 1
        if idx >= 0:
            last = close_by_date[bench_dates[idx]] / base
        series.append({"date": d, "nav": last})
    return {"status": "OK", "symbol": BENCH_SYMBOL, "series": series}


def _load_trades(bundle_dir: Path, db_path: Path | None) -> dict[str, Any]:
    path = bundle_dir / "trades.jsonl"
    if not path.is_file():
        return {"status": MISSING, "records": []}
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                records.append({
                    "exec_date": row.get("exec_date"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "shares": row.get("shares"),
                    "price": row.get("price"),
                    "notional": row.get("notional"),
                    "fee": row.get("fee"),
                    "price_source": row.get("price_source"),
                })
    except (json.JSONDecodeError, OSError):
        return {"status": MISSING, "records": []}

    panels = _load_factor_panels(bundle_dir)
    panel_dates = sorted(panels)
    names = _load_names(db_path, {str(r["symbol"]) for r in records if r["symbol"]})
    for record in records:
        record["display_name"] = names.get(str(record["symbol"]))
        record["reason"] = _trade_reason(record, panel_dates, panels)
    return {
        "status": "OK",
        "caliber": "NAV=1 归一口径（shares/notional/fee 非真实股数金额）",
        "count": len(records),
        "records": records,
    }


def _load_nav_daily(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "nav_daily.csv"
    if not path.is_file():
        return {"status": MISSING, "series": []}
    series: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    series.append({"date": row["date"], "nav": float(row["nav"])})
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return {"status": MISSING, "series": []}
    if not series:
        return {"status": MISSING, "series": []}
    return {"status": "OK", "count": len(series), "series": series}


def _load_nav_compare(reports_root: Path) -> dict[str, Any]:
    raw = _load_json(reports_root / "nav_compare" / "summary.json")
    if raw is None:
        return {"status": MISSING}
    return {
        "status": "OK",
        "generated_at": raw.get("generated_at"),
        "base_date": raw.get("base_date"),
        "latest": raw.get("latest"),
        "total_return_since_base": raw.get("total_return_since_base") or {},
        "sim_live": raw.get("sim_live") or {},
        "caliber": raw.get("caliber"),
    }


def build_backtest_compare(
    reports_root: Path | str = Path("reports"),
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """组装回测对比投影；manifest 与 trades 任一缺失则 decision_ready=false。

    db_path 仅用于 best-effort 的名称与沪深300 基准序列补全（只读连接），
    缺席或故障不影响主体数据。
    """

    reports_root = Path(reports_root)
    db = Path(db_path) if db_path else None
    bundle_dir = reports_root / "adhoc_backtest" / "repro_bundle"
    manifest = _load_manifest(bundle_dir)
    trades = _load_trades(bundle_dir, db)
    nav_daily = _load_nav_daily(bundle_dir)
    if nav_daily["status"] == "OK":
        dates = [row["date"] for row in nav_daily["series"]]
        nav_daily["benchmark"] = _load_benchmark(db, dates)
    else:
        nav_daily["benchmark"] = {"status": MISSING, "series": []}
    year_compare = _load_year_compare(reports_root)
    nav_compare = _load_nav_compare(reports_root)
    ready = manifest["status"] == "OK" and trades["status"] == "OK"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK" if ready else MISSING,
        "decision_ready": ready,
        "manifest": manifest,
        "year_compare": year_compare,
        "nav_daily": nav_daily,
        "trades": trades,
        "nav_compare": nav_compare,
    }
