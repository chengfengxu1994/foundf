#!/usr/bin/env python3
"""盘中补单执行器（充分验证模式）。

早盘 sim_rebalance 被偏离闸门拒掉的委托，盘中价格回到闸门内时自动补单。
这是**执行战术**，不产生新信号、不改变目标权重：候选与决策价仍由
daily_candidates 收盘后生成，本模块只等价格收敛后完成既定计划。

纪律（不可突破）：

- 只处理当日 execute 调仓日志里 status=REJECTED 且因偏离被拒的委托；
  决策价一律取 sim_targets 快照的 prices 表，不用盘中价替代。
- 每轮补单前重新走 place_order 的全部闸门（模拟页校验、偏离、上限）；
  单日补单不超过 MAX_INTRADAY_ORDERS 笔。
- 已 SUBMITTED 的同方向同票委托绝不重复（executions.jsonl + 本模块状态双查）。
- 14:30 后不再补单；--dry-run 默认，--execute 才真实下单。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from place_sim_order import place_order  # noqa: E402
from sim_rebalance import MAX_DEVIATION, REBALANCE_LOG, latest_targets  # noqa: E402
from data_provider.providers.eastmoney_provider import fetch_quotes  # noqa: E402

CST = timezone(timedelta(hours=8))
EXEC_LOG = Path("data/phone_sim_capture/executions.jsonl")
STATE_DIR = Path("data/phone_sim_capture/intraday")
WINDOW_START = (10, 0)
WINDOW_END = (14, 30)
MAX_INTRADAY_ORDERS = 6


def in_window(now: datetime) -> bool:
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1],
                        second=0, microsecond=0)
    end = now.replace(hour=WINDOW_END[0], minute=WINDOW_END[1],
                      second=0, microsecond=0)
    return start <= now <= end


def _today_log(today: date) -> dict | None:
    """当日最新 execute 调仓日志。"""
    if not REBALANCE_LOG.exists():
        return None
    logs = sorted(REBALANCE_LOG.glob("*.json"), reverse=True)
    for path in logs:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ts = datetime.fromisoformat(data["ts"])
        if data.get("execute") and ts.astimezone(CST).date() == today:
            return data
    return None


def pending_orders(log: dict, targets: dict) -> list[dict]:
    """调仓日志中因偏离被拒的委托，决策价取自 targets 快照。"""
    prices = targets.get("prices") or {}
    pending = []
    for fill in log.get("fills", []):
        if fill.get("status") != "REJECTED":
            continue
        if "偏离" not in (fill.get("reason") or ""):
            continue
        decision = prices.get(fill["symbol"])
        if not decision:
            continue
        pending.append({
            "side": fill["side"],
            "symbol": fill["symbol"],
            "qty": fill["qty"],
            "decision_price": decision,
        })
    return pending


def _filled_today(today: date) -> set[tuple[str, str]]:
    """今日不可自动重试的委托。

    SUBMITTING/UNKNOWN_SUBMISSION 可能已被券商受理，必须先由当日委托或
    成交对账解除；同 submission_id 的后续终态覆盖其 SUBMITTING 事件。
    """
    blocking = set()
    latest: dict[str, dict] = {}
    if not EXEC_LOG.exists():
        return blocking
    for line in EXEC_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts.astimezone(CST).date() != today:
            continue
        submission_id = rec.get("submission_id")
        if submission_id:
            latest[submission_id] = rec
        elif rec.get("status") == "SUBMITTED":
            blocking.add((rec["side"], rec["symbol"]))
    for rec in latest.values():
        if rec.get("status") in (
            "SUBMITTING", "SUBMITTED", "UNKNOWN_SUBMISSION"
        ):
            blocking.add((rec["side"], rec["symbol"]))
    return blocking


def _load_state(today: date) -> dict:
    path = STATE_DIR / f"{today.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"date": today.isoformat(), "attempts": []}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{state['date']}.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_cycle(today: date, *, execute: bool) -> dict:
    """单轮：读日志 → 算待补 → 实时价过闸门 → 补单。返回本轮摘要。"""
    log = _today_log(today)
    if log is None:
        return {"status": "WAITING_REBALANCE_LOG"}
    targets = latest_targets()
    if targets is None:
        return {"status": "NO_TARGETS"}
    state = _load_state(today)
    done = {(a["side"], a["symbol"]) for a in state["attempts"]
            if a.get("status") in (
                "SUBMITTING", "SUBMITTED", "UNKNOWN_SUBMISSION", "DRY_RUN"
            )}
    filled = _filled_today(today) | done
    todo = [o for o in pending_orders(log, targets)
            if (o["side"], o["symbol"]) not in filled]
    if not todo:
        return {"status": "NOTHING_PENDING"}

    quotes = {q["symbol"]: q for q in fetch_quotes([o["symbol"] for o in todo])}
    results = []
    submitted = sum(1 for a in state["attempts"]
                    if a.get("status") in ("SUBMITTED", "DRY_RUN"))
    for order in todo:
        if submitted >= MAX_INTRADAY_ORDERS:
            results.append({"symbol": order["symbol"], "status": "SKIPPED",
                            "reason": "单日补单上限"})
            continue
        quote = quotes.get(order["symbol"]) or {}
        last = quote.get("last")
        if not last:
            results.append({"symbol": order["symbol"], "status": "SKIPPED",
                            "reason": "无实时价(停牌或接口缺失)"})
            continue
        deviation = last / order["decision_price"] - 1
        if abs(deviation) > MAX_DEVIATION:
            results.append({"symbol": order["symbol"], "status": "WAITING",
                            "last": last, "deviation": round(deviation, 4)})
            continue
        res = place_order(order["side"], order["symbol"], order["qty"],
                          decision_price=order["decision_price"],
                          max_deviation=MAX_DEVIATION, dry_run=not execute)
        state["attempts"].append({
            "ts": datetime.now(timezone.utc).isoformat(), **order,
            "status": res.get("status"), "price": res.get("price"),
        })
        if res.get("status") in (
            "SUBMITTING", "SUBMITTED", "UNKNOWN_SUBMISSION", "DRY_RUN"
        ):
            submitted += 1
        results.append({"symbol": order["symbol"], "status": res.get("status"),
                        "price": res.get("price")})
        time.sleep(3)  # 手机 UI 操作间隔
    _save_state(state)
    return {"status": "OK", "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="盘中补单执行器")
    parser.add_argument("--execute", action="store_true", help="真实下单(默认 dry-run)")
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔秒")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    args = parser.parse_args()

    while True:
        now = datetime.now(CST)
        today = now.date()
        if not in_window(now):
            start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1],
                                second=0, microsecond=0)
            if now < start:
                # 窗口前(如 09:53 系统 cron 启动): 睡到窗口开始而不是退出
                time.sleep(min(60.0, (start - now).total_seconds()))
                continue
            print(f"[{now.isoformat()}] 窗口外({WINDOW_START}-{WINDOW_END} CST)，退出",
                  flush=True)
            return
        try:
            out = run_cycle(today, execute=args.execute)
        except Exception as exc:
            out = {"status": "ERROR", "reason": str(exc)}
        print(f"[{now.isoformat()}] {json.dumps(out, ensure_ascii=False)}", flush=True)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
