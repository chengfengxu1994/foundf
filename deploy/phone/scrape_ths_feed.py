#!/usr/bin/env python3
"""同花顺 App 信息爬取(手机端, fail-closed, RAW_ONLY staging)。

两类来源:
- 资讯要闻流(WebView, uiautomator 抓不了): 截图 + 宿主机 tesseract OCR
  → 标题列表。OCR 有误识别率, 产物只进 staging, 不直接进事件库。
- 个股行情快照(买入页原生控件 + 右侧五档盘口 OCR): 最新价/均价/涨跌停 +
  买卖五档。五档盘口是现有数据渠道(baostock/tushare 日线)没有的补盲来源。

安全约束:
- 只读屏幕(uiautomator/screencap), 不逆向协议、不抓包、不伪造请求。
- 频率克制: 每次运行抓资讯 1 屏 + 最多 MAX_QUOTES_PER_RUN 只标的。
- 任何一步离开预期页面(导航校验失败)即中止, 不静默产出错数据。

产物: data/phone_sim_capture/feed/<UTC时间戳>/
  news_ocr.txt / news.jsonl / quote_<code>.json / quote_<code>.png
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_ths_sim import (  # noqa: E402
    dismiss_marketing_popup, dismiss_popups, dump_ui, find_center,
    goto_sim_trade, launch_app, page_texts, screenshot, sh, tap, tap_text,
)
from phone_client import current_app, device_lock  # noqa: E402

FEED_ROOT = Path("data/phone_sim_capture/feed")
MAX_QUOTES_PER_RUN = 10
NEWS_TAB = (914, 2600)          # 底部「资讯」tab
BOOK_REGION = "830,380 390x860" # 五档盘口裁剪区(x,y WxH, 1220x2712 物理像素)


def _ocr(png: Path, out_base: Path, psm: str = "4", crop: str | None = None) -> str:
    src = png
    if crop:
        xy, wh = crop.split(" ")
        x, y = map(int, xy.split(","))
        cropped = png.with_suffix(".crop.png")
        subprocess.run(
            ["convert", str(png), "-crop", f"{wh}+{x}+{y}", "+repage", str(cropped)],
            check=True, capture_output=True,
        )
        src = cropped
    subprocess.run(
        ["tesseract", str(src), str(out_base), "-l", "chi_sim", "--psm", psm],
        check=True, capture_output=True,
    )
    return out_base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace")


def _scrape_news(out: Path) -> dict:
    """资讯要闻页截图 + OCR → 标题行 staging。"""

    launch_app(cold=True)
    if current_app() != "com.hexin.plat.android":
        return {"channel": "news_ocr", "status": "ABORTED",
                "reason": "同花顺未处于前台"}
    tap(*NEWS_TAB)
    time.sleep(6)
    if current_app() != "com.hexin.plat.android":
        return {"channel": "news_ocr", "status": "ABORTED",
                "reason": "资讯导航后同花顺不在前台"}
    png = out / "news.png"
    screenshot(png)
    raw = _ocr(png, out / "news_ocr", psm="4",
               crop="0,100 1220x2460")
    lines, seen = [], set()
    for line in raw.splitlines():
        line = line.strip()
        cjk = len(re.findall(r"[一-鿿]", line))
        if cjk >= 8 and line not in seen:
            seen.add(line)
            lines.append(line)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "PHONE_THS_FEED",
        "channel": "news_ocr",
        "reliability": "OCR_UNVERIFIED",
        "lines": lines,
        "status": "OK" if lines else "EMPTY_UNVERIFIED",
    }
    (out / "news.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"channel": "news_ocr", "headlines": len(lines),
            "status": record["status"]}


def scrape_news(out: Path) -> dict:
    """串行化手机 UI；锁屏解锁失败或已有操作者时立即中止。"""

    with device_lock():
        return _scrape_news(out)


def _scrape_quote(symbol: str, out: Path, workdir: Path) -> dict:
    """买入页抓单票快照: XML 读价, OCR 读五档盘口。"""

    if not re.fullmatch(r"\d{6}", symbol):
        return {"symbol": symbol, "status": "REJECTED", "reason": "bad symbol"}
    root = goto_sim_trade(workdir)
    for attempt in range(3):
        if attempt > 0:
            # 营销弹窗(WebView 浮层, 如「8月值得投」)吞点按且 BACK 无效
            # (2026-08-10 实证), 用红卡检测精准关闭后重建状态
            dismiss_marketing_popup(workdir)
            root = goto_sim_trade(workdir)
        if not tap_text(root, "买入"):
            return {"symbol": symbol, "status": "ABORTED", "reason": "无买入入口"}
        time.sleep(3)
        root = dump_ui(workdir / "q_buy.xml")
        if "买 入(模拟炒股)" in page_texts(root):
            break
    else:
        return {"symbol": symbol, "status": "ABORTED", "reason": "进不了买入页"}

    field = find_center(root, "股票代码/简拼") or (426, 488)
    tap(*field)
    time.sleep(1)
    sh(["shell", "input", "keyevent", "KEYCODE_MOVE_HOME"])
    for _ in range(12):
        sh(["shell", "input", "keyevent", "KEYCODE_FORWARD_DEL"])
    sh(["shell", "input", "text", symbol])
    time.sleep(3)

    xml_path = out / f"quote_{symbol}.xml"
    root = dump_ui(xml_path)
    texts = page_texts(root)
    if "买 入(模拟炒股)" not in texts or symbol not in texts:
        return {"symbol": symbol, "status": "REJECTED",
                "reason": "模拟买入页或代码未确认"}

    def _after(label: str) -> str | None:
        for i, t in enumerate(texts):
            if t.startswith(label):
                for f in texts[i + 1:i + 4]:
                    if re.fullmatch(r"[\d.%-]+", f):
                        return f
        return None

    png = out / f"quote_{symbol}.png"
    screenshot(png)
    book_raw = _ocr(png, out / f"quote_{symbol}_book", psm="6", crop=BOOK_REGION)
    book = {}
    for line in book_raw.splitlines():
        m = re.match(r"\s*([买卖])([1-5])\s*[::]?\s*([\d.]+)\s+(\d+)", line)
        if m:
            side = "ask" if m.group(1) == "卖" else "bid"
            book[f"{side}{m.group(2)}"] = {"price": float(m.group(3)),
                                           "volume": int(m.group(4))}
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "PHONE_THS_QUOTE",
        "symbol": symbol,
        "last": _after("最新"),
        "avg": _after("均价"),
        "limit_up": _after("涨停"),
        "limit_down": _after("跌停"),
        "book": book,
        "book_reliability": "OCR_UNVERIFIED",
        "status": "OK",
    }
    (out / f"quote_{symbol}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def scrape_quote(symbol: str, out: Path, workdir: Path) -> dict:
    """串行化单票 UI 抓取，避免与调仓/成交抓取交叉点按。"""

    with device_lock():
        return _scrape_quote(symbol, out, workdir)


def main() -> None:
    ap = argparse.ArgumentParser(description="同花顺手机端信息爬取(RAW_ONLY)")
    ap.add_argument("--news", action="store_true", help="抓资讯要闻流(OCR)")
    ap.add_argument("--quotes", nargs="*", default=None,
                    help="抓指定 6 位代码的行情快照(最多 10 只)")
    ap.add_argument("--out", default=str(FEED_ROOT))
    args = ap.parse_args()
    if not args.news and args.quotes is None:
        ap.error("至少指定 --news 或 --quotes")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    workdir = out / "_work"
    workdir.mkdir()

    results = []
    if args.news:
        results.append(scrape_news(out))
    for symbol in (args.quotes or [])[:MAX_QUOTES_PER_RUN]:
        try:
            results.append(scrape_quote(symbol, out, workdir))
        except Exception as exc:  # 单票失败不拖垮整批, 但不静默: 记入结果
            results.append({"symbol": symbol, "status": "ERROR", "reason": str(exc)})
    print(json.dumps({"out": str(out), "results": results},
                     ensure_ascii=False, indent=1))
    ok = bool(results) and all(r.get("status") == "OK" for r in results)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
