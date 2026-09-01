#!/usr/bin/env python3
"""同花顺模拟盘下单执行器（BUY/SELL、仅模拟环境、fail-closed）。

安全闸门：
- 每步校验仍在模拟交易页面（"买/卖 入出(模拟炒股)"字样），离开立即中止。
- 数量必须 100 的整数倍，单票股数与买入名义金额有硬上限；
  卖出额外校验页面"可卖 N 股"，超出拒单。
- 提供 decision_price 时，最新价相对偏离超过 max_deviation 拒绝下单
  （防止拿陈旧信号追价/杀跌）。
- --dry-run 只走到确认前一步并打印将要提交的订单。
- 执行结果(含识别到的股票名称)追加到 data/phone_sim_capture/executions.jsonl，
  名称供持仓页(只显示名称)与代码对账用。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_ths_sim import (  # noqa: E402
    dump_ui, dismiss_popups, find_center, goto_sim_trade,
    page_texts, sh, tap, tap_text,
)
import phone_client  # noqa: E402

MAX_QTY = 5000                 # 单票股数硬上限
MAX_NOTIONAL = 60000.0         # 单笔买入名义金额硬上限(虚拟资金)
EXEC_LOG = Path("data/phone_sim_capture/executions.jsonl")

SIDE_UI = {
    "BUY": {
        "tab": "买入",
        "button": "买 入(模拟炒股)",
        "confirm_title": "委托买入确认",
        "confirm_button": "确认买入",
    },
    "SELL": {
        "tab": "卖出",
        "button": "卖 出(模拟炒股)",
        "confirm_title": "委托卖出确认",
        "confirm_button": "确认卖出",
    },
}


def _type(field_center: tuple[int, int], text: str) -> None:
    tap(*field_center)
    time.sleep(1)
    # 清空残留内容: 光标移到最前再逐个删除
    sh(["shell", "input", "keyevent", "KEYCODE_MOVE_HOME"])
    for _ in range(12):
        sh(["shell", "input", "keyevent", "KEYCODE_FORWARD_DEL"])
    sh(["shell", "input", "text", text])
    time.sleep(2)


def _latest_price(texts: list[str]) -> float | None:
    for i, t in enumerate(texts):
        if t.startswith("最新"):
            for follow in texts[i + 1:i + 4]:
                try:
                    return float(follow)
                except ValueError:
                    continue
    return None


def _avail_qty(texts: list[str]) -> int | None:
    """卖出页「可卖N股」/ 买入页「可买N股」。"""
    for t in texts:
        m = re.fullmatch(r"可[买卖](\d+)股", t)
        if m:
            return int(m.group(1))
    return None


def _stock_name(texts: list[str], symbol: str) -> str | None:
    """代码输入后, 名称出现在代码文本之后(如 '601398' → '工商银行')。"""
    for i, t in enumerate(texts):
        if t == symbol and i + 1 < len(texts):
            nxt = texts[i + 1]
            if re.search(r"[一-鿿]", nxt) and not nxt.startswith("最新"):
                return nxt
    return None


def _log_execution(record: dict) -> None:
    EXEC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EXEC_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def place_order(
    side: str,
    symbol: str,
    qty: int,
    *,
    decision_price: float | None = None,
    max_deviation: float = 0.02,
    dry_run: bool = False,
    workdir: Path | None = None,
    _lock_held: bool = False,
) -> dict:
    """在模拟练习区提交一笔委托, 返回执行记录(dict)。"""

    side = side.upper()
    workdir = workdir or Path("data/phone_sim_capture/_exec")
    workdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    base = {"ts": ts, "side": side, "symbol": symbol, "qty": qty,
            "dry_run": dry_run}

    if side not in SIDE_UI:
        return {**base, "status": "REJECTED", "reason": "side 必须是 BUY 或 SELL"}
    ui = SIDE_UI[side]
    if not re.fullmatch(r"\d{6}", symbol):
        return {**base, "status": "REJECTED", "reason": "symbol 必须是 6 位代码"}
    if qty <= 0 or qty % 100 != 0 or qty > MAX_QTY:
        return {**base, "status": "REJECTED",
                "reason": f"qty 必须是 100 的整数倍且 ≤ {MAX_QTY}"}

    # 纪律守门(2026-08-06 接入): 任何 BUY 在执行前必须过 failure_check
    # (内含 discipline_engine 纪律门)。守门自身故障时 fail-closed 拒单。
    if side == "BUY" and not _lock_held:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            from investment_agent.failure_feedback import failure_check
            gate = failure_check(symbol, "BUY", 0.5)
            if not gate.get("allowed", False):
                return {**base, "status": "REJECTED",
                        "reason": f"纪律守门拒单: {gate.get('risk_warning') or 'BLOCK'}"}
        except Exception as exc:
            return {**base, "status": "REJECTED",
                    "reason": f"纪律守门故障 fail-closed: {type(exc).__name__}"}

    if not _lock_held:
        try:
            with phone_client.device_lock():
                return place_order(
                    side, symbol, qty, decision_price=decision_price,
                    max_deviation=max_deviation, dry_run=dry_run,
                    workdir=workdir, _lock_held=True,
                )
        except phone_client.PhoneNotReadyError as exc:
            return {**base, "status": "ABORTED_LOCKED", "reason": str(exc)}

    root = goto_sim_trade(workdir)
    # 顶部标签与表单同名, 且点击后可能仍在过渡; 带验证重试
    texts: list[str] = []
    for _ in range(3):
        if not tap_text(root, ui["tab"]):
            return {**base, "status": "ABORTED",
                    "reason": f"找不到{ui['tab']}入口"}
        time.sleep(3)
        root = dump_ui(workdir / "order_page.xml")
        texts = page_texts(root)
        if ui["button"] in texts or "股票代码/简拼" in texts:
            break
    else:
        return {**base, "status": "ABORTED",
                "reason": f"连续点击仍未进入{ui['tab']}表单, 已中止"}
    if ui["button"] not in texts:
        return {**base, "status": "ABORTED",
                "reason": f"页面缺少模拟标识({ui['button']}), 已中止"}

    code_field = find_center(root, "股票代码/简拼")
    if code_field is None:
        # 代码框可能残留上次输入, 直接用固定坐标
        code_field = (426, 488)
    _type(code_field, symbol)
    time.sleep(2)

    root = dump_ui(workdir / "order_filled.xml")
    texts = page_texts(root)
    if ui["button"] not in texts:
        return {**base, "status": "ABORTED", "reason": "输入代码后离开模拟页面"}
    if symbol not in texts:
        return {**base, "status": "REJECTED", "reason": "代码未被识别",
                "texts": texts[:30]}
    name = _stock_name(texts, symbol)
    price = _latest_price(texts)
    if price is None:
        return {**base, "status": "ABORTED", "reason": "读不到最新价"}
    if side == "BUY" and price * qty > MAX_NOTIONAL:
        return {**base, "status": "REJECTED",
                "reason": f"名义金额 {price * qty:.0f} 超过上限 {MAX_NOTIONAL:.0f}",
                "price": price}
    avail = _avail_qty(texts)
    if side == "SELL" and avail is not None and qty > avail:
        return {**base, "status": "REJECTED",
                "reason": f"卖出 {qty} 股超过可卖 {avail} 股",
                "available": avail}
    deviation = None
    if decision_price is not None and decision_price > 0:
        deviation = (price - decision_price) / decision_price
        if abs(deviation) > max_deviation:
            return {**base, "status": "REJECTED",
                    "reason": f"最新价 {price} 相对决策价 {decision_price} "
                              f"偏离 {deviation:+.2%}, 超过 {max_deviation:.0%}",
                    "price": price, "deviation": deviation}

    qty_field = find_center(root, "数量") or (426, 895)
    _type(qty_field, str(qty))
    time.sleep(1)

    if dry_run:
        record = {**base, "status": "DRY_RUN", "name": name, "price": price,
                  "deviation": deviation, "notional": round(price * qty, 2)}
        _log_execution(record)
        return record

    if not tap_text(dump_ui(workdir / "order_submit.xml"), ui["button"]):
        return {**base, "status": "ABORTED",
                "reason": f"找不到{ui['button']}按钮"}
    time.sleep(3)

    # 确认弹窗: 核对代码与数量
    root = dump_ui(workdir / "confirm.xml")
    texts = page_texts(root)
    if ui["confirm_title"] not in texts:
        return {**base, "status": "ABORTED", "reason": "未出现委托确认弹窗",
                "texts": texts[:30]}
    if symbol not in texts or str(qty) not in texts:
        return {**base, "status": "ABORTED",
                "reason": "确认弹窗内容与订单不一致", "texts": texts[:30]}
    submission_id = uuid.uuid4().hex
    submitting = {
        **base, "submission_id": submission_id, "status": "SUBMITTING",
        "name": name, "price": price, "deviation": deviation,
        "notional": round(price * qty, 2),
    }
    # 必须先持久化再点击确认；进程在点击后崩溃时，盘中执行器也会把
    # SUBMITTING 当作不可重试状态，等待委托/成交对账。
    _log_execution(submitting)
    try:
        confirm_tapped = tap_text(root, ui["confirm_button"])
    except Exception as exc:
        record = {
            **submitting, "status": "UNKNOWN_SUBMISSION",
            "reason": f"确认动作结果未知: {type(exc).__name__}",
        }
        _log_execution(record)
        return record
    if not confirm_tapped:
        record = {**submitting, "status": "ABORTED",
                  "reason": f"找不到{ui['confirm_button']}按钮"}
        _log_execution(record)
        return record
    try:
        time.sleep(4)
        root = dump_ui(workdir / "result.xml")
        texts = page_texts(root)
        dismiss_popups(root)
    except Exception as exc:
        record = {
            **submitting, "status": "UNKNOWN_SUBMISSION",
            "reason": f"确认后结果不可读: {type(exc).__name__}",
        }
        _log_execution(record)
        return record
    contract_no = None
    error_msg = None
    for t in texts:
        m = re.search(r"合同号为[:：](\d+)", t)
        if m:
            contract_no = m.group(1)
        if "失败" in t or "拒绝" in t or "废单" in t:
            error_msg = t
    status = "SUBMITTED" if contract_no else (
        "REJECTED" if error_msg else "UNKNOWN_SUBMISSION"
    )
    record = {
        **submitting,
        "status": status,
        "contract_no": contract_no,
        "error": error_msg,
    }
    _log_execution(record)
    return record


def place_buy(symbol: str, qty: int, **kwargs) -> dict:
    return place_order("BUY", symbol, qty, **kwargs)


def place_sell(symbol: str, qty: int, **kwargs) -> dict:
    return place_order("SELL", symbol, qty, **kwargs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="同花顺模拟盘委托执行器(仅模拟环境)")
    ap.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    ap.add_argument("--symbol", required=True, help="6 位股票代码")
    ap.add_argument("--qty", required=True, type=int, help="股数(100 的整数倍)")
    ap.add_argument("--decision-price", type=float, default=None)
    ap.add_argument("--max-deviation", type=float, default=0.02)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = place_order(
        args.side, args.symbol, args.qty,
        decision_price=args.decision_price,
        max_deviation=args.max_deviation,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] in ("SUBMITTED", "DRY_RUN") else 1)
