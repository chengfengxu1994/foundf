#!/usr/bin/env python3
"""把 capture_ths_sim.py 抓取的 UI XML 解析成结构化记录(staging JSONL)。

只做"页面文本 → 行记录"的忠实转写, 不做任何字段映射推断:
- 委托/成交表按列 x 区间分行转写, 单元格原样保留字符串。
- 产物写入 data/phone_sim_capture/parsed/<抓取目录名>.jsonl。
- 该 staging 是同花顺模拟成交进入 broker_sim_normalized_fill 之前的
  RAW_ONLY 层; 字段映射未经人工验收前, forward_learning_dataset 保持
  fail-closed, 本脚本产物不得直接用于训练。
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# 查询页(当日/历史 委托成交)列布局, 1220x2712 物理像素。
# 来自 2026-08-05 实测: 表头 成交时间/成交价/成交量/成交额 等。
COLUMNS = {
    "name_time": (0, 300),
    "price": (300, 660),
    "qty": (660, 930),
    "amount_status": (930, 1220),
}
TABLE_TOP_Y = 700          # 表头/筛选区之下才是数据行
ROW_HEIGHT = 130           # 一行约占两行为一组(名称+时间上下堆叠)


def cell_x_center(bounds: str) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def parse_table(xml_path: Path) -> list[dict]:
    """把一页 UI XML 的表格区文本按 (行,列) 归组, 原样转写。"""

    root = ET.parse(str(xml_path)).getroot()
    cells: list[dict] = []
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        c = cell_x_center(n.get("bounds") or "")
        if not t or c is None:
            continue
        x, y = c
        if y < TABLE_TOP_Y:
            continue
        col = next((k for k, (lo, hi) in COLUMNS.items() if lo <= x < hi), None)
        if col is None:
            continue
        cells.append({"row": y // ROW_HEIGHT, "col": col, "text": t})
    rows: dict[int, dict] = {}
    for cell in cells:
        rows.setdefault(cell["row"], {})[cell["col"]] = (
            rows.get(cell["row"], {}).get(cell["col"], "") + " " + cell["text"]
        ).strip()
    return [{"cells": v} for _, v in sorted(rows.items()) if len(v) >= 2]


def parse_capture(capture_dir: Path) -> list[dict]:
    out: list[dict] = []
    for xml_path in sorted(capture_dir.glob("*.xml")):
        page = xml_path.stem
        if page in ("probe", "nav"):
            continue
        if not (page.startswith("query_") or page.startswith("order_")):
            continue
        for rec in parse_table(xml_path):
            out.append({"capture": capture_dir.name, "page": page, **rec})
    return out


def main() -> None:
    cap_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/phone_sim_capture")
    out_dir = cap_root / "parsed"
    out_dir.mkdir(parents=True, exist_ok=True)
    for capture_dir in sorted(p for p in cap_root.iterdir() if p.is_dir() and p.name != "parsed"):
        records = parse_capture(capture_dir)
        if not records:
            continue
        out = out_dir / f"{capture_dir.name}.jsonl"
        out.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        print(f"{out} {len(records)} rows")


if __name__ == "__main__":
    main()
