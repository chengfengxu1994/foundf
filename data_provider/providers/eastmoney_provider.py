"""东方财富批量实时行情客户端（A 股盘中快照）。

定位：盘中每分级行情通道，补齐 baostock(T+1 收盘) / tushare(当日收盘)
之间的盘中空白，供交易执行闸门（偏离校验）与晚间复盘使用。

特点与边界：

- ``push2`` 批量接口一次请求返回整个池的快照，无 token、无限频顾虑；
  只读公开行情，不触碰任何账户或交易接口。
- 返回的是**快照**（最新价 + 当日累计量额），不是分钟 K 线；
  量额口径：``f5`` 成交量=手、``f6`` 成交额=元（与 tushare 手口径一致，
  入库存 volume_hand 原值，换算由消费方负责）。
- 快照价是**未复权原始价**；仅用于盘中闸门与复盘，绝不写入
  ``daily_price``（canonical 仍为 baostock 前复权）。
"""

from __future__ import annotations

from typing import Any

import httpx

PUSH2_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
# f12 代码 f14 名称 f2 最新价 f3 涨跌幅% f5 成交量(手) f6 成交额(元)
# f15 最高 f16 最低 f17 今开 f18 昨收
QUOTE_FIELDS = "f12,f14,f2,f3,f5,f6,f15,f16,f17,f18"
_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) FoundF/1.0"}


def to_secid(code: str) -> str:
    """6 位代码 → 东财 secid（1.=沪，0.=深）。"""
    code = code.strip()
    if code.startswith(("6", "5", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _num(value: Any) -> float | None:
    """东财用 "-" 表示无数据（停牌等）。"""
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_quotes(codes: list[str], *, timeout: int = _TIMEOUT,
                 proxy: str | None = None) -> list[dict[str, Any]]:
    """批量取实时快照。返回规范化 dict 列表；停牌票 last=None 保留记录。

    proxy 仅供 proxy_guard 逃生路径使用（httpx `proxy=`），默认直连。
    """
    if not codes:
        return []
    resp = httpx.get(
        PUSH2_URL,
        params={
            "secids": ",".join(to_secid(c) for c in codes),
            "fields": QUOTE_FIELDS,
            "fltt": "2",
            "invt": "2",
        },
        headers=_HEADERS,
        timeout=timeout,
        proxy=proxy,
    )
    data = resp.json()
    diff = (data.get("data") or {}).get("diff") or []
    quotes = []
    for item in diff:
        quotes.append({
            "symbol": item.get("f12") or "",
            "name": item.get("f14") or "",
            "last": _num(item.get("f2")),
            "pct_chg": _num(item.get("f3")),
            "volume_hand": _num(item.get("f5")),
            "amount": _num(item.get("f6")),
            "high": _num(item.get("f15")),
            "low": _num(item.get("f16")),
            "open": _num(item.get("f17")),
            "prev_close": _num(item.get("f18")),
        })
    return [q for q in quotes if q["symbol"]]
