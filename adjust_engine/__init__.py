"""
adjust_engine — 复权价格系统。

解决 120 积分 Tushare 没有 adj_factor 的问题。

方法：
    1. 基于价格跳变检测：当单日出现除权除息特征时（价格跳变但前日收盘≠当日开盘），
       自动计算调整因子
    2. 前复权：全部调整为当前股本基准
    3. 后复权：从上市日起计算

必须避免未来函数：历史某一天只能看到当时已公布的信息。

数据库新增：
    adjusted_daily_price — 复权后日线
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from foundf_db import Warehouse


# 分红除息检测参数
EX_DIVIDEND_THRESHOLD = 0.05     # 价格跳变 5% 以上
MAX_DAILY_CHANGE_NORMAL = 0.12   # 正常的最大单日涨跌幅


@dataclass
class AdjustmentEvent:
    """复权事件（分红/送股/转增/配股）。"""
    date: str
    symbol: str
    event_type: str         # 'dividend', 'split', 'rights_offering'
    ratio: float = 1.0       # 调整比例
    description: str = ""


@dataclass
class AdjustedPrice:
    """复权后价格。"""
    date: str
    symbol: str
    close_raw: float
    close_adj: float
    adj_factor: float       # 复权因子
    is_adjusted: bool = False  # 当日是否发生了调整


class AdjustEngine:
    """复权引擎。

    使用方式:
        engine = AdjustEngine("data/finance.duckdb")
        engine.adjust_symbol("600519")
        engine.adjust_all()
    """

    def __init__(self, duckdb_path: str | Path = "data/finance.duckdb"):
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        # 确保复权表存在
        self.warehouse.execute("""
            CREATE TABLE IF NOT EXISTS adjusted_daily_price (
                symbol VARCHAR NOT NULL,
                date DATE NOT NULL,
                close_raw DOUBLE NOT NULL,
                close_adj DOUBLE NOT NULL,
                adj_factor DOUBLE NOT NULL DEFAULT 1.0,
                is_adjusted BOOLEAN DEFAULT FALSE,
                UNIQUE (symbol, date)
            )
        """)

    # ── 公开 API ──────────────────────────────────────

    def adjust_symbol(self, symbol: str) -> list[AdjustedPrice]:
        """对单个标的执行前复权。

        方法：检测除权除息日，反向计算调整因子。
        """
        rows = self.warehouse.query(
            "SELECT date, open, high, low, close, volume "
            "FROM daily_price WHERE symbol = ? "
            "ORDER BY date",
            [symbol],
        )
        if len(rows) < 20:
            return []

        dates = [r["date"] for r in rows]
        closes = np.array([r["close"] for r in rows], dtype=float)
        opens = np.array([r["open"] for r in rows], dtype=float)
        highs = np.array([r["high"] for r in rows], dtype=float)
        lows = np.array([r["low"] for r in rows], dtype=float)

        # 检测除权除息日
        events = self._detect_adjustment_events(dates, opens, closes, highs, lows)

        # 计算累积调整因子（反向：从最新往最旧）
        n = len(closes)
        factors = np.ones(n, dtype=float)

        for event in events:
            # 找到事件日期在数组中的位置
            for i, d in enumerate(dates):
                if str(d)[:10] == event.date:
                    if i > 0:
                        # 调整因子 = 前日收盘 / (当日开盘 - 理论上应开盘价)
                        # 简化：使用跳变比例
                        prev_close = closes[i - 1]
                        curr_open = opens[i]
                        if prev_close > 0 and curr_open > 0:
                            ratio = curr_open / prev_close
                            if ratio < 1.0:  # 除权（送股/转增/拆股）
                                event.ratio = 1.0 / ratio
                            elif ratio >= 1.01:  # 并股
                                event.ratio = ratio
                    break

        # 反向累积：从最旧到最新（前复权）
        cum_factor = 1.0
        for i in range(n):
            # 查找是否有调整事件在今天发生
            today_str = str(dates[i])[:10]
            for event in events:
                if event.date == today_str:
                    cum_factor *= event.ratio
                    break
            factors[i] = cum_factor

        # 应用复权
        max_factor = factors[-1]  # 最新日的因子（应该接近1）
        adjusted_prices: list[AdjustedPrice] = []
        for i in range(n):
            adj_close = closes[i] * factors[i] / max_factor
            is_adj = str(dates[i])[:10] in {e.date for e in events}
            adjusted_prices.append(AdjustedPrice(
                date=str(dates[i])[:10],
                symbol=symbol,
                close_raw=float(closes[i]),
                close_adj=float(adj_close),
                adj_factor=float(factors[i] / max_factor),
                is_adjusted=is_adj,
            ))

        # 写入 DuckDB
        for ap in adjusted_prices:
            self.warehouse.execute(
                "INSERT OR REPLACE INTO adjusted_daily_price "
                "(symbol, date, close_raw, close_adj, adj_factor, is_adjusted) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [ap.symbol, ap.date, ap.close_raw, ap.close_adj,
                 ap.adj_factor, ap.is_adjusted],
            )

        return adjusted_prices

    def adjust_all(self, symbols: list[str] | None = None) -> dict[str, int]:
        """对所有标的执行复权。返回 {symbol: 行数}。"""
        if symbols is None:
            symbols = [
                r["symbol"]
                for r in self.warehouse.query(
                    "SELECT DISTINCT symbol FROM daily_price ORDER BY symbol"
                )
            ]
        stats = {}
        for sym in symbols:
            try:
                result = self.adjust_symbol(sym)
                if result:
                    stats[sym] = len(result)
            except Exception:
                continue
        return stats

    # ── 事件检测 ──────────────────────────────────────

    def _detect_adjustment_events(
        self, dates: list, opens: np.ndarray, closes: np.ndarray,
        highs: np.ndarray, lows: np.ndarray,
    ) -> list[AdjustmentEvent]:
        """检测除权除息事件。"""
        events: list[AdjustmentEvent] = []
        for i in range(1, len(dates)):
            prev_close = closes[i - 1]
            curr_open = opens[i]

            if prev_close <= 0 or curr_open <= 0:
                continue

            # 开盘相对前日收盘的跳变
            gap = curr_open / prev_close - 1
            # 当日涨幅（基于开盘）
            daily_return = (closes[i] - curr_open) / max(curr_open, 0.01)

            # 除权除息特征：
            # 1. 低开超过阈值（gap < -5%）
            # 2. 但当日又涨回来（低开高走）
            if gap < -EX_DIVIDEND_THRESHOLD and abs(daily_return) < MAX_DAILY_CHANGE_NORMAL:
                ratio = curr_open / prev_close
                events.append(AdjustmentEvent(
                    date=str(dates[i])[:10],
                    symbol="",
                    event_type="dividend_or_split",
                    ratio=1.0 / ratio if ratio < 1.0 else ratio,
                    description=f"跳变 {gap:.2%}, 前收 {prev_close:.2f}→开 {curr_open:.2f}",
                ))
        return events

    # ── 验证 ──────────────────────────────────────────

    def validate_random(self, n: int = 10) -> dict[str, bool]:
        """验证复权后序列在无事件区间收益与原始价格一致。"""
        symbols = [
            r["symbol"]
            for r in self.warehouse.query(
                "SELECT DISTINCT symbol FROM daily_price ORDER BY RANDOM() LIMIT ?",
                [n],
            )
        ]
        # 提前获取所有事件日期，用于判断无事件区间
        event_dates: set[str] = set()
        try:
            event_rows = self.warehouse.query(
                "SELECT DISTINCT date FROM adjusted_daily_price WHERE is_adjusted = TRUE"
            )
            event_dates = {str(r["date"])[:10] for r in event_rows}
        except Exception:
            pass

        results = {}
        for sym in symbols:
            rows = self.warehouse.query(
                "SELECT date, close_raw, close_adj, is_adjusted "
                "FROM adjusted_daily_price "
                "WHERE symbol = ? ORDER BY date",
                [sym],
            )
            if len(rows) < 20:
                results[sym] = False
                continue

            ok = True
            for i in range(1, len(rows)):
                prev_raw = rows[i - 1]["close_raw"]
                curr_raw = rows[i]["close_raw"]
                prev_adj = rows[i - 1]["close_adj"]
                curr_adj = rows[i]["close_adj"]

                # 检查无事件区间：今天和昨天都不是事件日
                today_adj = str(rows[i]["date"])[:10] in event_dates or rows[i]["is_adjusted"]
                prev_adj_flag = str(rows[i - 1]["date"])[:10] in event_dates or rows[i - 1]["is_adjusted"]

                if not today_adj and not prev_adj_flag:
                    if prev_raw > 0 and prev_adj > 0:
                        raw_ratio = curr_raw / prev_raw
                        adj_ratio = curr_adj / prev_adj
                        if abs(raw_ratio - adj_ratio) > 0.01:
                            ok = False
                            break
            results[sym] = ok
        return results

    def close(self) -> None:
        self.warehouse.close()
