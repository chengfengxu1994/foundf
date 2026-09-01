#!/usr/bin/env python3
"""模拟账户状态解析：capture 产物 → 类型化 账户/持仓/委托。

- 输入为 capture_ths_sim.py 抓取的 UI XML(默认读最新抓取目录)。
- 持仓页只有股票名称没有代码; 代码对账依赖 executions.jsonl 里下单时
  记录的名称, 以及 config/sim_names.json 的人工兜底映射。
- 输出供 sim_rebalance.py 做目标权重 vs 实际持仓的调仓决策。
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CAPTURE_ROOT = Path("data/phone_sim_capture")
NAME_MAP_PATH = Path("config/sim_names.json")

# order_持仓 页列布局(表头实测 x 区间)
HOLD_COLS = {"mv": (0, 300), "pnl": (300, 600), "qty": (600, 890), "cost": (890, 1220)}
# order_委托 页列布局
ORDER_COLS = {"name": (0, 300), "price": (300, 600), "qty": (600, 900), "status": (900, 1220)}


def _center(bounds: str) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _cells(xml_path: Path, cols: dict[str, tuple[int, int]],
           header_y: int) -> list[dict[str, list[str]]]:
    """表头之下的单元格按 (行带, 列) 归组。"""
    root = ET.parse(str(xml_path)).getroot()
    rows: dict[int, dict[str, list[str]]] = {}
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        c = _center(n.get("bounds") or "")
        if not t or c is None:
            continue
        x, y = c
        if y <= header_y:
            continue
        col = next((k for k, (lo, hi) in cols.items() if lo <= x < hi), None)
        if col is None:
            continue
        rows.setdefault(y // 130, {}).setdefault(col, []).append(t)
    return [rows[k] for k in sorted(rows)]


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_holdings(xml_path: Path) -> list[dict]:
    """持仓行: 名称/市值/盈亏/盈亏%/持仓/可用/成本/现价。

    每只股票占两行(名称行 + 数值行, 间距约 67px), 以名称单元格为锚点归组。
    """
    root = ET.parse(str(xml_path)).getroot()
    cells: list[tuple[str, int, str]] = []  # (col, y, text)
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        c = _center(n.get("bounds") or "")
        if not t or c is None:
            continue
        x, y = c
        if y <= 1500:  # 表头及其上
            continue
        col = next((k for k, (lo, hi) in HOLD_COLS.items() if lo <= x < hi), None)
        if col:
            cells.append((col, y, t))

    anchors = sorted(y for col, y, t in cells
                     if col == "mv" and re.search(r"[一-鿿]", t))
    out = []
    for y0 in anchors:
        block = [(col, y, t) for col, y, t in cells if y0 - 10 <= y < y0 + 110]
        by_col: dict[str, list[str]] = {}
        for col, _, t in sorted(block, key=lambda c: c[1]):
            by_col.setdefault(col, []).append(t)
        mv = by_col.get("mv", [])
        qty = by_col.get("qty", [])
        cost = by_col.get("cost", [])
        pnl = by_col.get("pnl", [])
        qty_n = int(_num(qty[0]) or 0) if qty else None
        cost_v = _num(cost[0]) if cost else None
        market_value = _num(mv[1]) if len(mv) > 1 else None
        partial = False
        if market_value is None and qty_n and cost_v:
            # 列表虚拟化裁剪的残缺行(第二行数字未渲染, 2026-08-12 实证:
            # 第 6 行建设银行只剩名称行, 被过滤后调仓误判 0 持仓超额买入)。
            # 数量/成本在名称行, 市值按 qty*cost 估算(仅影响现金估算,
            # 调仓计划的关键字段 qty 是精确值)。
            market_value = round(qty_n * cost_v, 2)
            partial = True
        out.append({
            "name": mv[0],
            "market_value": market_value,
            "pnl": _num(pnl[0]) if pnl else None,
            "pnl_pct": pnl[1] if len(pnl) > 1 else None,
            "qty": qty_n,
            # 残缺行无可用数: 回退 qty(乐观口径, 实际可卖由下单表单校验兜底)
            "available": int(_num(qty[1]) or 0) if len(qty) > 1 else qty_n,
            "cost": cost_v,
            "last": _num(cost[1]) if len(cost) > 1 else cost_v,
            "partial": partial,
        })
    # 底部导航等杂项: 无市值且无数量的行不是持仓
    return [h for h in out if h["market_value"] is not None and h["qty"]]


def parse_open_orders(xml_path: Path) -> list[dict]:
    """未成交委托行: 名称/委托价/委托量/方向/状态。"""
    out = []
    for row in _cells(xml_path, ORDER_COLS, header_y=1450):
        name = row.get("name", [])
        if len(name) < 2 or not re.search(r"[一-鿿]", name[0]):
            continue
        status = row.get("status", [])
        qty = row.get("qty", [])
        price = row.get("price", [])
        rec = {
            "name": name[0],
            "order_time": name[1] if len(name) > 1 else None,
            "order_price": _num(price[0]) if price else None,
            "qty": int(_num(qty[0]) or 0) if qty else None,
            "side": status[0] if status else None,
            "status": status[1] if len(status) > 1 else None,
        }
        if rec["status"] and "未成交" in rec["status"]:
            out.append(rec)
    return out


def parse_account(xml_path: Path) -> dict:
    """sim_home: 总资产/浮动盈亏/总市值。"""
    texts = [(n.get("text") or "").strip()
             for n in ET.parse(str(xml_path)).getroot().iter("node")]
    texts = [t for t in texts if t]

    def _after(label: str) -> float | None:
        for i, t in enumerate(texts):
            if t == label:
                for f in texts[i + 1:i + 4]:
                    v = _num(f)
                    if v is not None:
                        return v
        return None

    return {
        "total_assets": _after("总资产"),
        "float_pnl": _after("浮动盈亏"),
        "market_value": _after("总市值"),
    }


def load_name_map() -> dict[str, str]:
    """名称↔代码对账表: executions.jsonl 学习 + config/sim_names.json 兜底。"""
    mapping: dict[str, str] = {}
    if NAME_MAP_PATH.is_file():
        mapping.update(json.loads(NAME_MAP_PATH.read_text(encoding="utf-8")))
    exec_log = CAPTURE_ROOT / "executions.jsonl"
    if exec_log.is_file():
        for line in exec_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("name") and rec.get("symbol"):
                mapping[rec["name"]] = rec["symbol"]
    return mapping  # name -> symbol


def latest_capture() -> Path | None:
    dirs = sorted(p for p in CAPTURE_ROOT.iterdir()
                  if p.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z", p.name))
    return dirs[-1] if dirs else None


def merge_holdings(a: list[dict], b: list[dict]) -> list[dict]:
    """按名称合并两屏持仓; 完整行优先于 partial 行, 同名后者不覆盖前者。"""

    merged: dict[str, dict] = {}
    for h in list(a) + list(b):
        cur = merged.get(h["name"])
        if cur is None or (cur.get("partial") and not h.get("partial")):
            merged[h["name"]] = h
    return list(merged.values())


def get_state(capture_dir: Path | None = None) -> dict:
    d = capture_dir or latest_capture()
    if d is None:
        return {"status": "NO_CAPTURE"}
    state = {"capture": d.name, "status": "OK"}
    home = d / "sim_home.xml"
    hold = d / "order_持仓.xml"
    orders = d / "order_委托.xml"
    required = {"sim_home.xml": home, "order_持仓.xml": hold,
                "order_委托.xml": orders}
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        return {
            "capture": d.name,
            "status": "INCOMPLETE_CAPTURE",
            "missing": missing,
        }
    try:
        state["account"] = parse_account(home)
        state["holdings"] = parse_holdings(hold)
        hold_b = d / "order_持仓_b.xml"
        if hold_b.is_file():
            # 第二屏(上滑后)合并: 完整行优先于 partial(虚拟化裁行防护)
            state["holdings"] = merge_holdings(
                state["holdings"], parse_holdings(hold_b)
            )
        state["open_orders"] = parse_open_orders(orders)
    except (ET.ParseError, OSError, ValueError) as exc:
        return {
            "capture": d.name,
            "status": "INVALID_CAPTURE",
            "error_type": type(exc).__name__,
        }
    if not state["account"].get("total_assets"):
        return {
            "capture": d.name,
            "status": "INVALID_CAPTURE",
            "reason": "TOTAL_ASSETS_MISSING",
        }
    state["name_map"] = load_name_map()
    return state


if __name__ == "__main__":
    print(json.dumps(get_state(), ensure_ascii=False, indent=1))
