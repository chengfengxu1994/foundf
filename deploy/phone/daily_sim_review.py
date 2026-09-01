#!/usr/bin/env python3
"""每日模拟盘复盘打分（收盘后运行，充分验证模式）。

输入：sim_targets 快照（含 prices 决策价表）、调仓日志、executions.jsonl、
盘中补单状态、当日收盘（tushare daily 单调用，失败回退 cn_quote_snapshot
最后一帧）、15:38 抓取解析的持仓状态。

产出 ``reports/sim_review/<date>.json`` 与同名 ``.md``：

- 执行分（0-100）：当日计划委托的成交率。拒单明细与偏离度一并列出，
  拒单率高说明决策价陈旧或闸门过严，是参数优化输入而非错误。
- 信号分（0-100）：上一批候选等权 T+1 收益的正票占比（hit rate）。
  收益口径 = 当日收盘 / 上批决策价 - 1（**累计口径**，跨多日持仓时为
  多日累计浮盈，非当日涨跌；明细另附 day_chg = 昨收→今收 的日涨跌）。
  基准对比待沪深300 入库后补。
- 滑点：模拟盘按委托价成交，执行滑点系统性为零（模拟口径缺陷，
  真实账户需另建冲击成本模型），此处只记录名义值。
- execution_quality 段（2026-08-14 新增）：调
  ``execution_quality.build_daily`` 拿当日 TCA 摘要（决策滑点 / vs VWAP
  胜率 / 拒单率 / 孤儿占比）；任何故障降级 ``EQ_UNAVAILABLE``，不阻塞复盘。
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim_state import get_state  # noqa: E402

CST = timezone(timedelta(hours=8))
TARGETS_DIR = Path("reports/sim_targets")
REBALANCE_LOG = Path("data/phone_sim_capture/rebalance")
INTRADAY_DIR = Path("data/phone_sim_capture/intraday")
EXEC_LOG = Path("data/phone_sim_capture/executions.jsonl")
REVIEW_DIR = Path("reports/sim_review")
DB_PATH = "data/finance.duckdb"


def _targets_files() -> list[Path]:
    return sorted(TARGETS_DIR.glob("*.json"))


def _today_closes(symbols: list[str], today: date) -> dict[str, float]:
    """当日官方收盘：tushare 单调用优先，失败回退东财最后一帧快照。"""
    try:
        from quant_strategy.daily_candidates import (
            _load_tushare_token, _tushare_daily_by_date)
        items = _tushare_daily_by_date(
            today.isoformat().replace("-", ""), _load_tushare_token())
        closes = {it[0].split(".")[0]: float(it[1])
                  for it in items if len(it) >= 2 and it[1]}
        if closes:
            return {s: closes[s] for s in symbols if s in closes}
    except Exception:
        pass
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    out = {}
    for sym in symbols:
        row = con.execute(
            "SELECT last FROM cn_quote_snapshot WHERE symbol=? "
            "AND ts::date=? ORDER BY ts DESC LIMIT 1",
            [sym, today.isoformat()]).fetchone()
        if row and row[0]:
            out[sym] = row[0]
    con.close()
    return out


def _prev_closes(symbols: list[str], today: date) -> dict[str, float]:
    """最近一个 <today 的 daily_price 收盘价（T-1 官方收盘，baostock T+1 时
    可能取到 T-2；仅用于日涨跌口径展示，不参与打分）。"""
    if not symbols:
        return {}
    try:
        import duckdb
        con = duckdb.connect(DB_PATH, read_only=True)
        out = {}
        for sym in symbols:
            row = con.execute(
                "SELECT close FROM daily_price WHERE symbol=? AND date<? "
                "ORDER BY date DESC LIMIT 1",
                [sym, today.isoformat()]).fetchone()
            if row and row[0]:
                out[sym] = float(row[0])
        con.close()
        return out
    except Exception:
        return {}


def _today_logs(today: date) -> list[dict]:
    logs = []
    if REBALANCE_LOG.exists():
        for path in sorted(REBALANCE_LOG.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            ts = datetime.fromisoformat(data["ts"])
            if ts.astimezone(CST).date() == today and data.get("execute"):
                logs.append(data)
    return logs


def _today_executions(today: date) -> list[dict]:
    recs = []
    if EXEC_LOG.exists():
        for line in EXEC_LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = datetime.fromisoformat(rec["ts"])
            if ts.astimezone(CST).date() == today:
                recs.append(rec)
    return recs


def _execution_quality(today: date) -> dict:
    """当日 TCA 摘要（execution_quality 模块）；故障降级 EQ_UNAVAILABLE。"""
    try:
        from execution_quality import build_daily
        daily = build_daily(today, DB_PATH)
        return {"status": "OK", **daily["summary"]}
    except Exception as exc:  # 只加不改：TCA 故障绝不阻塞复盘
        return {"status": "EQ_UNAVAILABLE", "error": str(exc)[:200]}


def build_review(today: date) -> dict:
    files = _targets_files()
    targets_now = json.loads(files[-1].read_text(encoding="utf-8")) if files else {}
    targets_prev = (json.loads(files[-2].read_text(encoding="utf-8"))
                    if len(files) >= 2 else {})

    # ── 执行质量 ──
    logs = _today_logs(today)
    executions = _today_executions(today)
    intraday_path = INTRADAY_DIR / f"{today.isoformat()}.json"
    intraday = (json.loads(intraday_path.read_text(encoding="utf-8"))
                if intraday_path.exists() else {"attempts": []})
    planned = sum(len(l.get("fills", [])) for l in logs)
    submitted = [e for e in executions if e.get("status") == "SUBMITTED"]
    rejected = [f for l in logs for f in l.get("fills", [])
                if f.get("status") == "REJECTED"]
    intraday_fills = [a for a in intraday.get("attempts", [])
                      if a.get("status") == "SUBMITTED"]
    fill_rate = (len(submitted) / planned) if planned else None
    exec_score = round(100 * fill_rate) if fill_rate is not None else None

    # ── 候选 T+1 表现（上一批 picks 用当日官方收盘计价）──
    prev_picks = {s: w for s, w in (targets_prev.get("weights") or {}).items()
                  if s != "CASH" and w > 0}
    prev_prices = targets_prev.get("prices") or {}
    closes = _today_closes(list(prev_picks), today)
    prev_closes = _prev_closes(list(prev_picks), today)
    per_pick = []
    for sym in prev_picks:
        ref = prev_prices.get(sym)
        close = closes.get(sym)
        if ref and close:
            pick = {"symbol": sym, "ref": ref, "close": close,
                    "ret": round(close / ref - 1, 4)}
            # 日涨跌口径（昨收→今收），与 ret（决策价→今收的累计口径）分开，
            # 防止把多日累计浮亏误读为"当日大跌"
            prev_close = prev_closes.get(sym)
            if prev_close:
                pick["prev_close"] = prev_close
                pick["day_chg"] = round(close / prev_close - 1, 4)
            per_pick.append(pick)
    scored = [p for p in per_pick]
    hit_rate = (sum(1 for p in scored if p["ret"] > 0) / len(scored)) if scored else None
    avg_ret = (sum(p["ret"] for p in scored) / len(scored)) if scored else None
    signal_score = round(100 * hit_rate) if hit_rate is not None else None

    # ── 持仓状态（读最新抓取，不碰手机）──
    state = get_state()
    holdings = state.get("holdings", []) if state.get("status") == "OK" else []

    # ── 每日账户快照落库（TWR/回撤的原始序列）──
    account = state.get("account") or {}
    if state.get("status") == "OK" and account.get("total_assets"):
        try:
            from foundf_db.warehouse import Warehouse
            from foundf_db.runtime_scheduler import runtime_write_lock
            with runtime_write_lock(Path(DB_PATH).resolve().parent) as acquired:
                if not acquired:
                    raise RuntimeError("FoundF 共享写锁忙")
                with Warehouse(DB_PATH) as warehouse:
                    warehouse.init()
                    total = float(account["total_assets"])
                    mkt = float(account.get("market_value") or 0)
                    warehouse.insert("sim_nav_daily", [{
                        "date": today.isoformat(),
                        "total_assets": total,
                        "cash": round(total - mkt, 2),
                        "market_value": mkt,
                        "holdings_json": json.dumps(holdings, ensure_ascii=False),
                        "source": "ths_phone_sim",
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    }], conflict_strategy="replace")
        except Exception:
            pass  # NAV 落库失败不阻塞复盘报告

    return {
        "date": today.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution": {
            "planned_orders": planned,
            "submitted": len(submitted),
            "intraday_fills": len(intraday_fills),
            "rejected": len(rejected),
            "reject_detail": [{"symbol": f["symbol"],
                               "reason": f.get("reason", "")[:60]}
                              for f in rejected],
            "fill_rate": round(fill_rate, 4) if fill_rate is not None else None,
            "exec_score": exec_score,
        },
        "signal": {
            "prev_data_as_of": targets_prev.get("data_as_of"),
            "scored_picks": scored,
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "avg_return": round(avg_ret, 4) if avg_ret is not None else None,
            "signal_score": signal_score,
            "note": "等权 T+1，基准对比待指数入库；模拟盘按委托价成交，滑点口径为零",
        },
        "holdings": holdings,
        "current_targets": {k: v for k, v in
                            (targets_now.get("weights") or {}).items()},
        "execution_quality": _execution_quality(today),
    }


def render_md(review: dict) -> str:
    exe = review["execution"]
    sig = review["signal"]
    lines = [
        f"# 模拟盘每日复盘 {review['date']}",
        "",
        f"- 执行分: **{exe['exec_score']}**（计划 {exe['planned_orders']} 笔，"
        f"成交 {exe['submitted']} + 盘中补 {exe['intraday_fills']}，"
        f"拒 {exe['rejected']}）",
        f"- 信号分: **{sig['signal_score']}**（上批 {sig['prev_data_as_of']} "
        f"候选 T+1 命中率 {sig['hit_rate']}，等权均收益 {sig['avg_return']}）",
        "",
        "## 候选 T+1 明细",
        "",
    ]
    for p in sig["scored_picks"]:
        day = (f"，当日 {p['prev_close']} → {p['close']} ({p['day_chg']:+.2%})"
               if p.get("day_chg") is not None else "")
        lines.append(f"- {p['symbol']}: 决策价 {p['ref']} → 今收 {p['close']} "
                     f"(累计 {p['ret']:+.2%}{day})")
    if exe["reject_detail"]:
        lines += ["", "## 拒单明细", ""]
        for r in exe["reject_detail"]:
            lines.append(f"- {r['symbol']}: {r['reason']}")
    eq = review.get("execution_quality") or {}
    lines += ["", "## 执行质量（TCA）", ""]
    if eq.get("status") == "OK":
        lines += [
            f"- 成交 {eq['fills_count']} 笔，平均决策滑点 "
            f"{eq['avg_slippage_decision_bps']} bps（正=劣于决策）",
            f"- vs VWAP 胜率 {eq['vwap_win_rate']}，拒单率 "
            f"{eq['reject']['reject_rate']}，孤儿占比 {eq['orphan_ratio']}",
            f"- 费用口径：{eq['fee_note']}",
        ]
    else:
        lines.append(f"- {eq.get('status', 'EQ_UNAVAILABLE')}: "
                     f"{eq.get('error', '')}")
    lines += ["", f"> {sig['note']}", ""]
    return "\n".join(lines)


def main() -> None:
    today = datetime.now(CST).date()
    review = build_review(today)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEW_DIR / f"{today.isoformat()}.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    (REVIEW_DIR / f"{today.isoformat()}.md").write_text(
        render_md(review), encoding="utf-8")
    print(json.dumps({"status": "OK", "date": today.isoformat(),
                      "exec_score": review["execution"]["exec_score"],
                      "signal_score": review["signal"]["signal_score"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
