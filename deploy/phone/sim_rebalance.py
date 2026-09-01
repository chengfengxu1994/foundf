#!/usr/bin/env python3
"""模拟盘按目标权重调仓（充分验证模式核心）。

流程: 抓现状(capture) → 解析持仓(sim_state) → 读目标权重
(reports/sim_targets/<data_as_of>.json, 由 daily_candidates 生成)
→ 计算买卖差 → 先卖后买(place_sim_order) → 记录计划与结果。

纪律(不可突破):
- 每天最多运行一次调仓(由 cron 保证); 单次最多 MAX_ORDERS 笔委托。
- 目标/实际差额的市值低于 MIN_TRADE_VALUE 不动(避免碎股噪音)。
- 卖出量不超过可用(T+1 的今天买入不可用); 每笔走 place_order 的全部闸门
  (模拟页校验、偏离 >2% 拒单、名义金额上限)。
- --dry-run 默认, --execute 才真实下单; 计划与结果都落盘留痕。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim_state import get_state  # noqa: E402

TARGETS_DIR = Path("reports/sim_targets")
REBALANCE_LOG = Path("data/phone_sim_capture/rebalance")
MIN_TRADE_VALUE = 1500.0   # 低于此市值差额不动
MAX_ORDERS = 6             # 单次调仓委托笔数上限
MAX_DEVIATION = 0.02
MAX_TARGET_AGE_DAYS = 4    # 容纳周末；更旧目标 fail-closed
CST = timezone(timedelta(hours=8))


def build_prices(state: dict, targets: dict,
                 db_path: str = "data/finance.duckdb") -> dict[str, float]:
    """参考价优先级: 目标快照决策价 > 持仓现价 > DuckDB 收盘价。

    - ``targets["prices"]`` 由 daily_candidates 生成(tushare 当日补齐或
      T-1 收盘), 是偏离闸门的诚实基准; 持仓现价兜底不在快照里的票
      (主要是全退持仓), DuckDB 兜底快照缺失的老快照文件。
    """
    prices: dict[str, float] = {}
    snap = targets.get("prices") or {}
    for sym in targets["weights"]:
        if sym != "CASH" and snap.get(sym):
            prices[sym] = snap[sym]
    for h in state.get("holdings", []):
        code = state.get("name_map", {}).get(h["name"])
        if code and h.get("last") and code not in prices:
            prices[code] = h["last"]
    missing = [s for s in targets["weights"]
               if s != "CASH" and s not in prices]
    if missing:
        import duckdb  # 局部导入, 避免手机脚本硬依赖
        con = duckdb.connect(db_path, read_only=True)
        for sym in missing:
            row = con.execute(
                "SELECT close FROM daily_price WHERE symbol=? AND date<=? "
                "ORDER BY date DESC LIMIT 1",
                [sym, targets["data_as_of"]]).fetchone()
            if row:
                prices[sym] = row[0]
        con.close()
    return prices


def latest_targets(
    *, now: datetime | None = None, targets_dir: Path | None = None
) -> dict | None:
    """读取最新且可执行的目标快照。

    文件名不作为新鲜度证据；必须校验 data_as_of/generated_at，
    防止候选生成中断后继续执行旧策略。
    """

    root = targets_dir or TARGETS_DIR
    files = sorted(root.glob("*.json"))
    if not files:
        return None
    snapshots = []
    for path in files:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            as_of = date.fromisoformat(str(item["data_as_of"]))
            generated = datetime.fromisoformat(str(item["generated_at"]))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            if not isinstance(item.get("weights"), dict) or not item["weights"]:
                continue
            snapshots.append((as_of, generated, item))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not snapshots:
        raise ValueError("目标快照全部损坏或缺少时点证据")
    as_of, generated, item = max(snapshots, key=lambda row: (row[0], row[1]))
    current = (now or datetime.now(timezone.utc)).astimezone(CST)
    generated_local = generated.astimezone(CST)
    age_days = (current.date() - as_of).days
    if generated > current.astimezone(timezone.utc):
        raise ValueError("目标快照 generated_at 来自未来")
    if generated_local.date() != as_of:
        raise ValueError("目标快照生成日与 data_as_of 不一致")
    if age_days < 0 or age_days > MAX_TARGET_AGE_DAYS:
        raise ValueError(f"目标快照已过期({age_days} 天)")
    return item


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(temporary, path)


def has_execute_run(today: date | None = None, log_dir: Path | None = None) -> bool:
    """当日只允许一个 execute run，不依赖 cron “恰好一次”。"""

    target_date = today or datetime.now(CST).date()
    root = log_dir or REBALANCE_LOG
    if not root.exists():
        return False
    for path in root.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(str(item["ts"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if item.get("execute") is True and stamp.astimezone(CST).date() == target_date:
            return True
    return False


def compute_plan(state: dict, weights: dict[str, float],
                 prices: dict[str, float]) -> dict:
    """纯函数: 现状 + 目标权重 + 参考价 → 买卖计划。"""

    total = (state.get("account") or {}).get("total_assets")
    if not total or total <= 0:
        return {"status": "ABORTED", "reason": "读不到总资产"}
    name_map = state.get("name_map", {})
    holdings = {h["name"]: h for h in state.get("holdings", [])}
    open_order_names = {
        o.get("name") for o in state.get("open_orders", []) if o.get("name")
    }
    open_order_symbols = {
        name_map[name] for name in open_order_names if name in name_map
    }
    targets = {s: w for s, w in weights.items() if s != "CASH" and w > 0}

    sells, buys, skips = [], [], []
    # 持仓解析完整性守门(2026-08-12 建设银行事故: 虚拟化裁行使解析漏掉
    # 1000 股, 计划层误判 0 持仓超额买入): 解析市值合计与账户总市值
    # 偏差超 5% 即 fail-closed——宁可当日不调仓, 不在残缺状态上计划。
    account_mv = (state.get("account") or {}).get("market_value")
    parsed_mv = sum(h.get("market_value") or 0 for h in holdings.values())
    if account_mv and account_mv > 0:
        gap = abs(parsed_mv - account_mv) / account_mv
        if gap > 0.05:
            return {"status": "ABORTED",
                    "reason": f"持仓解析市值 {parsed_mv:.0f} 与账户总市值 "
                              f"{account_mv:.0f} 偏差 {gap:.1%} 超 5%, 状态不可信"}
    # 持仓代码化: 名称 → 代码(对账表缺失则跳过卖出, 绝不误卖)
    held_by_symbol: dict[str, dict] = {}
    for name, h in holdings.items():
        symbol = name_map.get(name)
        if symbol is None:
            skips.append({"name": name, "reason": "名称无法对账代码, 跳过(保护)"})
            continue
        held_by_symbol[symbol] = h

    for symbol, h in held_by_symbol.items():
        price = prices.get(symbol) or h.get("last")
        if not price:
            skips.append({"symbol": symbol, "reason": "无参考价"})
            continue
        target_qty = (
            math.floor(total * targets.get(symbol, 0.0) / price / 100) * 100
        )
        diff_qty = (h["qty"] or 0) - target_qty
        diff_value = diff_qty * price
        # 完全退出(目标 0 股)不受最小交易额限制, 避免遗留碎仓
        full_exit = target_qty == 0
        if diff_qty >= 100 and (full_exit or diff_value >= MIN_TRADE_VALUE):
            sellable = min(diff_qty, h.get("available") or 0)
            if h["name"] in open_order_names:
                skips.append({"symbol": symbol, "reason": "有未成交委托, 跳过"})
            elif sellable >= 100:
                sells.append({"symbol": symbol, "name": h["name"],
                              "qty": sellable, "ref_price": price,
                              "reason": f"目标 {target_qty} 股, 超出 {diff_qty} 股"})
            else:
                skips.append({"symbol": symbol,
                              "reason": f"可用 {h.get('available')} 不足(T+1)"})

    held_value = sum(h.get("market_value") or 0 for h in holdings.values())
    cash = total - held_value
    for symbol, w in sorted(targets.items(), key=lambda kv: -kv[1]):
        price = prices.get(symbol)
        if not price:
            skips.append({"symbol": symbol, "reason": "无参考价"})
            continue
        h = held_by_symbol.get(symbol)
        target_qty = math.floor(total * w / price / 100) * 100
        cur_qty = (h or {}).get("qty") or 0
        diff_qty = target_qty - cur_qty
        if diff_qty < 100 or diff_qty * price < MIN_TRADE_VALUE:
            continue
        if symbol in open_order_symbols:
            skips.append({"symbol": symbol, "reason": "有未成交委托, 跳过"})
            continue
        buys.append({"symbol": symbol, "qty": diff_qty, "ref_price": price,
                     "reason": f"目标 {target_qty} 股, 缺口 {diff_qty} 股"})

    orders = sells + buys
    omitted = orders[MAX_ORDERS:]
    for order in omitted:
        skips.append({
            "symbol": order["symbol"],
            "reason": f"超过单次 {MAX_ORDERS} 笔委托上限, 跳过",
        })
    selected = orders[:MAX_ORDERS]
    selected_sells = [item for item in selected if item in sells]
    selected_buys = [item for item in selected if item in buys]
    return {
        "status": "OK",
        "total_assets": total,
        "cash_estimate": round(cash, 2),
        "sells": selected_sells,
        "buys": selected_buys,
        "skips": skips,
    }


def main() -> None:
    os.umask(0o027)
    ap = argparse.ArgumentParser(description="模拟盘目标权重调仓")
    ap.add_argument("--execute", action="store_true", help="真实下单(默认 dry-run)")
    ap.add_argument("--capture", default=None, help="指定抓取目录(默认最新)")
    args = ap.parse_args()

    state = get_state(Path(args.capture) if args.capture else None)
    if state.get("status") != "OK":
        print(json.dumps({"status": "ABORTED", "reason": "无有效抓取",
                          "state": state}, ensure_ascii=False))
        sys.exit(1)
    try:
        targets = latest_targets()
    except ValueError as exc:
        print(json.dumps({"status": "ABORTED", "reason": str(exc)},
                         ensure_ascii=False))
        sys.exit(1)
    if targets is None:
        print(json.dumps({"status": "ABORTED",
                          "reason": "无目标权重快照(reports/sim_targets)"},
                         ensure_ascii=False))
        sys.exit(1)

    prices = build_prices(state, targets)

    plan = compute_plan(state, targets["weights"], prices)
    if args.execute and has_execute_run():
        print(json.dumps({
            "status": "ABORTED",
            "reason": "当日已存在 execute 调仓记录, 拒绝重复执行",
        }, ensure_ascii=False))
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REBALANCE_LOG / f"{stamp}.json"
    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "execute": args.execute,
        "run_status": "RUNNING" if args.execute else "DRY_RUN",
        "targets_data_as_of": targets["data_as_of"],
        "plan": plan,
        "fills": [],
    }
    # 在任何委托前持久化执行意图；进程崩溃后当日也不会重跑。
    _atomic_json(out, result)
    if args.execute and plan["status"] == "OK":
        from place_sim_order import place_order
        for order in plan["sells"] + plan["buys"]:
            side = "SELL" if order in plan["sells"] else "BUY"
            res = place_order(side, order["symbol"], order["qty"],
                              decision_price=order["ref_price"],
                              max_deviation=MAX_DEVIATION)
            result["fills"].append(res)
            _atomic_json(out, result)
            # REJECTED 是单笔闸门决策(偏离/可卖不足), 不影响其他委托;
            # ABORTED/FAILED 是环境或执行异常, 立即停止留人工核查。
            if res["status"] in ("ABORTED", "FAILED", "UNKNOWN_SUBMISSION"):
                break

    result["run_status"] = "COMPLETED" if plan["status"] == "OK" else "ATTENTION"
    if any(item.get("status") in {"ABORTED", "FAILED", "UNKNOWN_SUBMISSION"}
           for item in result["fills"]):
        result["run_status"] = "ATTENTION"
    _atomic_json(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    sys.exit(0 if plan["status"] == "OK" else 1)


if __name__ == "__main__":
    main()
