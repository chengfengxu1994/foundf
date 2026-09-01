#!/usr/bin/env python3
"""盘前手机端信息爬取（系统 cron 自治版，RAW_ONLY staging）。

标的清单自动确定：最新 BUY 候选批次 + 能对账到代码的持仓（去重，≤8 只），
然后新闻 OCR + 逐票行情快照。产物 feed/<UTC时间戳>/，会话 cron 只负责
事后解读汇报，不参与执行。

用法: python3 deploy/phone/premarket_scrape.py  (项目根目录下, 系统 cron 08:37)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrape_ths_feed import FEED_ROOT, scrape_news, scrape_quote  # noqa: E402
from sim_state import get_state  # noqa: E402

MAX_SYMBOLS = 8


def candidate_symbols() -> list[str]:
    import duckdb  # 局部导入, 手机脚本不硬依赖 DB
    con = duckdb.connect(str(ROOT / "data/finance.duckdb"), read_only=True)
    try:
        rows = con.execute(
            """SELECT symbol FROM strategy_candidate
               WHERE generated_at = (SELECT max(generated_at) FROM strategy_candidate)
                 AND side = 'BUY'
               ORDER BY conviction DESC""").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def holding_symbols() -> list[str]:
    """持仓名称 → 代码(对账表缺失的跳过, 绝不猜)。"""
    state = get_state(None)
    if state.get("status") != "OK":
        return []
    name_map = state.get("name_map", {})
    codes = []
    for h in state.get("holdings", []):
        code = name_map.get(h["name"])
        if code:
            codes.append(code)
    return codes


def main() -> int:
    symbols, seen = [], set()
    for s in candidate_symbols() + holding_symbols():
        if re.fullmatch(r"\d{6}", s) and s not in seen:
            seen.add(s)
            symbols.append(s)
    symbols = symbols[:MAX_SYMBOLS]
    if not symbols:
        print(json.dumps({"status": "NO_SYMBOLS", "reason": "无候选且无持仓"}))
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / FEED_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)
    workdir = out / "_work"
    workdir.mkdir()

    results = [scrape_news(out)]
    for symbol in symbols:
        try:
            results.append(scrape_quote(symbol, out, workdir))
        except Exception as exc:  # 单票失败不拖垮整批, 但不静默
            results.append({"symbol": symbol, "status": "ERROR", "reason": str(exc)})
    print(json.dumps({"out": str(out), "symbols": symbols, "results": results},
                     ensure_ascii=False, indent=1))
    ok = bool(results) and all(r.get("status") == "OK" for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
