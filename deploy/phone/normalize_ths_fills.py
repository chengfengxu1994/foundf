#!/usr/bin/env python3
"""同花顺手机模拟盘成交 → broker_sim_normalized_fill 规范化映射器。

映射合同（ths_phone_sim.v1，验收记录见
``docs/guides/THS_PHONE_SIM_FILL_MAPPING.md``）：

- 来源：``data/phone_sim_capture/parsed/*.jsonl`` 中
  ``order_成交``（当日）与 ``query_历史成交``（历史）页的数据行，
  形如 ``name_time="工商银行 09:32:24", price="7.520", qty="100",
  amount_status="买入 752.000"``。表头/导航/占位行自动跳过。
- ``fill_time``：当日页取抓取日期 + 成交时分秒；历史页名称段含
  ``MM-DD`` 或 ``YYYYMMDD`` 日期时优先用记录内日期（同花顺历史页
  实为 ``名称 20260812 10:20:26`` 八位日期格式，2026-08-14 前未识别
  导致名称带日期后缀对不上映射、且成交日落成抓取日）。一律
  Asia/Shanghai → UTC。
- ``symbol``：只走 ``sim_state.load_name_map()`` 对账表（executions 学习
  + ``config/sim_names.json`` 人工兜底），对不上**跳过并计数，永不猜**。
- ``fee_amount``：同花顺模拟盘不展示费用，恒为 NULL——回归层标
  ``MISSING_FEE``，绝不补 0。
- ``candidate_id``：同票同方向、``generated_at <= fill_time`` 的最近一条
  候选；无则 NULL（孤儿成交，回归层拒收计数——手工试单属此类）。
- ``fill_id``：``sha256(symbol|side|fill_time|price|qty)`` 语义去重，
  多次抓取/多日历史页重复出现不产生重复行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim_state import load_name_map  # noqa: E402
from foundf_db.warehouse import Warehouse  # noqa: E402

CST = timezone(timedelta(hours=8))
PARSED_DIR = Path("data/phone_sim_capture/parsed")
FILL_PAGES = {"order_成交", "query_历史成交"}
MAPPING_VERSION = "ths_phone_sim.v1"

_NAME_TIME = re.compile(
    r"^(.+?)\s+(?:(\d{8}|\d{2}-\d{2})\s+)?(\d{2}:\d{2}:\d{2})$")
_SIDE_AMOUNT = re.compile(r"^(买入|卖出)\s*([\d.]*)")
_BARE_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def _is_wrapped_fill_head(cells: dict) -> bool:
    """成交行被 UI 换行截断的「头行」：name_time 只有名称（无时间），
    但价/量/方向齐全。2026-08-14 实证：中国银行 1300 股买入在
    order_成交 页被拆成「中国银行 | 5.920 | 1300 | 买入」+
    下一行「10:16:13 | 7696.000」，修复前整笔成交丢失。"""
    nt = str(cells.get("name_time") or "").strip()
    if not nt or _NAME_TIME.match(nt) or _BARE_TIME.match(nt):
        return False  # 完整行/空行/裸时间行都不是头行
    price = _num(cells.get("price"))
    qty = _num(cells.get("qty"))
    sm = _SIDE_AMOUNT.match(str(cells.get("amount_status") or "").strip())
    return bool(price and qty and sm)


def _merge_wrapped(head: dict, tail: dict) -> dict | None:
    """裸时间尾行并入名称头行，合成完整 cells；尾行不是裸时间返回 None。"""
    nt = str(tail.get("name_time") or "").strip()
    if not _BARE_TIME.match(nt):
        return None
    sm = _SIDE_AMOUNT.match(str(head.get("amount_status") or "").strip())
    if not sm:
        return None
    amount = str(tail.get("amount_status") or "").strip()
    merged = dict(head)
    merged["name_time"] = f"{str(head['name_time']).strip()} {nt}"
    merged["amount_status"] = f"{sm.group(1)} {amount}".strip()
    return merged


def _num(value) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_fill_record(capture: str, page: str, cells: dict) -> dict | None:
    """单条 parsed cells → 规范化草稿；非数据行返回 None。"""
    if page not in FILL_PAGES:
        return None
    m = _NAME_TIME.match(str(cells.get("name_time") or "").strip())
    if not m:
        return None
    name, date_part, hms = m.group(1).strip(), m.group(2), m.group(3)
    price = _num(cells.get("price"))
    qty = _num(cells.get("qty"))
    sm = _SIDE_AMOUNT.match(str(cells.get("amount_status") or "").strip())
    if not (price and qty and sm):
        return None
    side = "BUY" if sm.group(1) == "买入" else "SELL"

    capture_date = datetime.strptime(capture[:8], "%Y%m%d").date()
    if date_part and "-" not in date_part:
        # 历史页八位日期（YYYYMMDD），年份月日齐全直接用
        fill_date = datetime.strptime(date_part, "%Y%m%d").date()
    elif date_part:  # 历史页记录自带日期（MM-DD），年份取抓取年
        month, day = int(date_part[:2]), int(date_part[3:])
        year = capture_date.year
        fill_date = datetime(year, month, day).date()
        if fill_date > capture_date:  # 跨年（如 12 月抓 01 月记录）
            fill_date = datetime(year - 1, month, day).date()
    else:
        fill_date = capture_date
    fill_time = datetime.strptime(
        f"{fill_date.isoformat()} {hms}", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=CST).astimezone(timezone.utc)

    return {
        "name": name,
        "side": side,
        "fill_time": fill_time.isoformat(),
        "fill_price": price,
        "quantity": qty,
        "source_record": json.dumps(
            {"capture": capture, "page": page, "cells": cells},
            ensure_ascii=False),
    }


def _find_candidate_id(con, symbol: str, side: str,
                       fill_time: str) -> str | None:
    """同票同方向、generated_at <= fill_time 的最近一条候选；无则 None。"""
    row = con.execute(
        "SELECT candidate_id FROM strategy_candidate "
        "WHERE symbol=? AND side=? AND generated_at<=? "
        "ORDER BY generated_at DESC LIMIT 1",
        [symbol, side, fill_time]).fetchone()
    return row[0] if row else None


def normalize_fills(db_path: str | Path, *, dry_run: bool = False) -> dict:
    """全量解析 → 规范化入库（幂等）。返回统计。

    dry-run 用只读连接（不碰生产库写锁），只统计不写库。
    """
    import duckdb

    name_map = load_name_map()
    stats = {"records_seen": 0, "fills_parsed": 0, "inserted": 0,
             "skipped_unmapped_name": 0, "orphan_no_candidate": 0,
             "skipped_names": {}, "fills": []}
    rows = []
    for path in sorted(PARSED_DIR.glob("*.jsonl")):
        pending_wrap: tuple[str, str, dict] | None = None  # 换行头行
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stats["records_seen"] += 1
            capture = rec.get("capture", "")
            page = rec.get("page", "")
            cells = rec.get("cells") or {}
            if pending_wrap is not None:
                # 头行之后紧跟同页裸时间尾行 → 合并为完整行处理；
                # 否则丢弃半成品头行，当前行照常走解析
                merged = (
                    _merge_wrapped(pending_wrap[2], cells)
                    if capture == pending_wrap[0] and page == pending_wrap[1]
                    else None
                )
                pending_wrap = None
                if merged is not None:
                    cells = merged
            draft = parse_fill_record(capture, page, cells)
            if draft is None:
                if page in FILL_PAGES and _is_wrapped_fill_head(cells):
                    pending_wrap = (capture, page, cells)
                continue
            stats["fills_parsed"] += 1
            raw_name = draft.pop("name")
            symbol = name_map.get(raw_name)
            if not symbol:
                stats["skipped_unmapped_name"] += 1
                # 记录被跳过的原始名称（含方向与示例时间），便于定位映射缺口
                entry = stats["skipped_names"].setdefault(
                    raw_name, {"count": 0, "sides": set(),
                               "sample_fill_time": draft["fill_time"]})
                entry["count"] += 1
                entry["sides"].add(draft["side"])
                continue
            fill_id = "tsf_" + hashlib.sha256(
                f"{symbol}|{draft['side']}|{draft['fill_time']}"
                f"|{draft['fill_price']}|{draft['quantity']}".encode()
            ).hexdigest()[:20]
            rows.append({"fill_id": fill_id, "symbol": symbol, **draft})

    if dry_run:
        con = duckdb.connect(str(db_path), read_only=True)
        warehouse = None
    else:
        warehouse = Warehouse(db_path)
        warehouse.init()
        con = warehouse.conn
    try:
        for row in rows:
            row["candidate_id"] = _find_candidate_id(
                con, row["symbol"], row["side"], row["fill_time"])
            if row["candidate_id"] is None:
                stats["orphan_no_candidate"] += 1
            row.update({
                "fee_amount": None,
                "account_mode": "SIMULATION",
                "source": "ths_phone_sim",
                "mapping_version": MAPPING_VERSION,
                "imported_at": datetime.now(timezone.utc).isoformat(),
            })
            stats["fills"].append({
                "fill_id": row["fill_id"], "symbol": row["symbol"],
                "side": row["side"], "fill_time": row["fill_time"],
                "fill_price": row["fill_price"], "quantity": row["quantity"],
                "candidate_id": row["candidate_id"],
            })
        if not dry_run and rows and warehouse is not None:
            before = warehouse.query(
                "SELECT COUNT(*) AS n FROM broker_sim_normalized_fill"
            )[0]["n"]
            warehouse.insert("broker_sim_normalized_fill", rows,
                             conflict_strategy="ignore")
            after = warehouse.query(
                "SELECT COUNT(*) AS n FROM broker_sim_normalized_fill"
            )[0]["n"]
            stats["inserted"] = int(after - before)
    finally:
        if warehouse is not None:
            warehouse.close()
        else:
            con.close()
    # sides set 转排序列表，保证 JSON 可序列化且输出稳定
    stats["skipped_names"] = {
        name: {"count": e["count"], "sides": sorted(e["sides"]),
               "sample_fill_time": e["sample_fill_time"]}
        for name, e in sorted(stats["skipped_names"].items())}
    if dry_run:
        stats["inserted"] = 0
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="同花顺模拟成交规范化映射")
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        stats = normalize_fills(args.db, dry_run=True)
    else:
        from foundf_db.runtime_scheduler import runtime_write_lock
        with runtime_write_lock(Path(args.db).resolve().parent) as acquired:
            if not acquired:
                raise SystemExit("FoundF 共享写锁忙，成交规范化已安全跳过")
            stats = normalize_fills(args.db, dry_run=False)
    print(json.dumps({k: v for k, v in stats.items() if k != "fills"},
                     ensure_ascii=False, indent=1))
    if stats["skipped_names"]:
        print("被跳过的未映射名称（需补 config/sim_names.json 或修解析）:")
        for name, info in stats["skipped_names"].items():
            print(f"  {name} x{info['count']} sides={info['sides']} "
                  f"示例时间 {info['sample_fill_time']}")
    for f in stats["fills"]:
        print(json.dumps(f, ensure_ascii=False))


if __name__ == "__main__":
    main()
