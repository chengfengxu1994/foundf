"""
investment_behavior — 个人投资行为分析。

基于真实交易流水分析用户投资习惯。

分析维度：
    1. 买入行为：频率/金额/行业偏好/追涨比例
    2. 卖出行为：盈利卖出/亏损割肉/持仓周期/过早卖出
    3. 收益归因：Beta / 行业 / 选股 / 择时
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import Warehouse


class BehaviorAnalyzer:
    """投资行为分析器。

    使用方式:
        analyzer = BehaviorAnalyzer("data/finance.duckdb")
        report = analyzer.analyze()
    """

    def __init__(self, warehouse: Warehouse | None = None,
                 duckdb_path: str | Path = "data/finance.duckdb"):
        if warehouse:
            self.warehouse = warehouse
        else:
            self.warehouse = Warehouse(duckdb_path)
            self.warehouse.init()

    def analyze(self) -> dict[str, Any]:
        """执行全部行为分析。"""
        txns = self.warehouse.query(
            "SELECT * FROM portfolio_transaction ORDER BY trade_date"
        )
        if not txns:
            return {"status": "no_data", "message": "无交易记录"}

        return {
            "buy_behavior": self._buy_analysis(txns),
            "sell_behavior": self._sell_analysis(txns),
            "attribution": self._attribution(txns),
            "summary": self._summary(txns),
        }

    def _buy_analysis(self, txns: list) -> dict[str, Any]:
        buys = [t for t in txns if t["side"] == "BUY"]
        if not buys:
            return {}

        total_buy_amount = sum(t["amount"] for t in buys)
        max_buy = max(buys, key=lambda t: t["amount"])

        # 行业偏好（从 symbol 推断市场）
        market_pref: dict[str, int] = {}
        for t in buys:
            m = t.get("market", "UNKNOWN")
            market_pref[m] = market_pref.get(m, 0) + 1

        # 追涨比例（买入前5日价格变动）
        chase_count = 0
        for t in buys:
            symbol = t["symbol"]
            bars = self.warehouse.query(
                "SELECT close FROM daily_price WHERE symbol = ? "
                "AND date < ? ORDER BY date DESC LIMIT 5",
                [symbol, str(t["trade_date"])[:10]],
            )
            if len(bars) >= 5:
                ret = (bars[0]["close"] / bars[-1]["close"] - 1)
                if ret > 0.05:  # 5日内涨超5%还买入 = 追涨
                    chase_count += 1

        return {
            "total_buys": len(buys),
            "total_buy_amount": round(total_buy_amount, 2),
            "avg_buy_amount": round(total_buy_amount / len(buys), 2),
            "max_single_buy": round(max_buy["amount"], 2),
            "max_single_symbol": max_buy["symbol"],
            "market_preference": market_pref,
            "chase_ratio": round(chase_count / len(buys), 3) if buys else 0,
        }

    def _sell_analysis(self, txns: list) -> dict[str, Any]:
        sells = [t for t in txns if t["side"] == "SELL"]
        if not sells:
            return {}

        # 计算每笔卖出的盈亏（需要查找买入均价）
        profit_sells = 0
        loss_sells = 0
        hold_days_list: list[int] = []
        early_sell_count = 0

        for s in sells:
            # 查找该标的之前的所有买入
            buys_before = [
                t for t in txns
                if t["symbol"] == s["symbol"] and t["side"] == "BUY"
                and t["trade_date"] < s["trade_date"]
            ]
            if not buys_before:
                continue

            # 简单平均成本
            total_qty = sum(b["quantity"] for b in buys_before)
            total_amount = sum(b["amount"] for b in buys_before)
            if total_qty <= 0:
                continue
            avg_cost = total_amount / total_qty
            sell_price = s["price"]
            pnl = (sell_price - avg_cost) / avg_cost if avg_cost > 0 else 0

            if pnl > 0:
                profit_sells += 1
            else:
                loss_sells += 1

            # 持仓天数
            if buys_before:
                first_buy = min(b["trade_date"] for b in buys_before)
                try:
                    from datetime import datetime as dt
                    d1 = dt.strptime(str(first_buy)[:10], "%Y-%m-%d")
                    d2 = dt.strptime(str(s["trade_date"])[:10], "%Y-%m-%d")
                    days = (d2 - d1).days
                    hold_days_list.append(days)
                    if days < 30:
                        early_sell_count += 1
                except (ValueError, TypeError):
                    pass

        total_sells = profit_sells + loss_sells
        return {
            "total_sells": len(sells),
            "profit_sell_ratio": round(profit_sells / total_sells, 3) if total_sells else 0,
            "loss_sell_ratio": round(loss_sells / total_sells, 3) if total_sells else 0,
            "avg_holding_days": round(sum(hold_days_list) / len(hold_days_list)) if hold_days_list else 0,
            "early_sell_ratio": round(early_sell_count / len(sells), 3) if sells else 0,
            "max_holding_days": max(hold_days_list) if hold_days_list else 0,
        }

    def _attribution(self, txns: list) -> dict[str, Any]:
        """简单收益归因。"""
        # 当前持仓盈亏
        positions = self.warehouse.query("SELECT * FROM portfolio_computed_position")
        total_pnl = sum(p["profit_loss"] or 0 for p in positions)
        total_cost = sum(p["total_cost"] or 0 for p in positions)

        # 基准收益（取 CSI300 最近一年）
        benchmark_bars = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol='CSI300' "
            "ORDER BY date DESC LIMIT 252"
        )
        benchmark_return = 0
        if len(benchmark_bars) >= 252:
            benchmark_return = benchmark_bars[0]["close"] / benchmark_bars[-1]["close"] - 1

        portfolio_return = (total_pnl / total_cost) if total_cost > 0 else 0

        return {
            "portfolio_return": round(portfolio_return, 4),
            "benchmark_return": round(benchmark_return, 4),
            "excess_return": round(portfolio_return - benchmark_return, 4),
            "alpha": "positive" if portfolio_return > benchmark_return else "negative",
        }

    def _summary(self, txns: list) -> dict[str, Any]:
        """生成行为摘要。"""
        first_txn = min(t["trade_date"] for t in txns)
        last_txn = max(t["trade_date"] for t in txns)
        symbols = set(t["symbol"] for t in txns)
        return {
            "trading_period": f"{str(first_txn)[:10]} ~ {str(last_txn)[:10]}",
            "total_transactions": len(txns),
            "unique_stocks": len(symbols),
            "total_traded_stocks": ", ".join(sorted(symbols)[:10]),
        }

    def to_markdown(self, report: dict[str, Any]) -> str:
        if report.get("status") == "no_data":
            return "# 投资行为分析\n\n暂无交易记录。"

        lines = [
            f"# 个人投资行为分析报告",
            f"",
            f"## 交易概况",
            f"- 交易期间: {report['summary']['trading_period']}",
            f"- 总交易次数: {report['summary']['total_transactions']}",
            f"- 交易标的: {report['summary']['unique_stocks']} 只",
            f"",
            f"## 买入行为",
        ]
        b = report.get("buy_behavior", {})
        if b:
            lines.extend([
                f"- 买入次数: {b.get('total_buys', 0)}",
                f"- 平均单笔: {b.get('avg_buy_amount', 0):,.2f}",
                f"- 最大单笔: {b.get('max_single_buy', 0):,.2f} ({b.get('max_single_symbol', '')})",
                f"- 追涨比例: {b.get('chase_ratio', 0):.1%}",
            ])

        lines.extend(["", "## 卖出行为"])
        s = report.get("sell_behavior", {})
        if s:
            lines.extend([
                f"- 盈利卖出: {s.get('profit_sell_ratio', 0):.1%}",
                f"- 亏损卖出: {s.get('loss_sell_ratio', 0):.1%}",
                f"- 平均持仓: {s.get('avg_holding_days', 0)} 天",
                f"- 过早卖出: {s.get('early_sell_ratio', 0):.1%}",
            ])

        lines.extend(["", "## 收益归因"])
        a = report.get("attribution", {})
        if a:
            lines.extend([
                f"- 组合收益: {a.get('portfolio_return', 0):.2%}",
                f"- 基准收益: {a.get('benchmark_return', 0):.2%}",
                f"- 超额收益: {a.get('excess_return', 0):+.2%}",
            ])

        return "\n".join(lines)

    def close(self) -> None:
        self.warehouse.close()
