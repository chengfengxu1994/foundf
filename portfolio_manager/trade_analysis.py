"""
trade_analysis.py — Per-stock trade analysis (Phase I3).

Computes per-stock metrics:
- Trade count (buys/sells)
- Average cost
- Holding period (average days held for sold positions)
- Win/loss count
- Contribution to total P&L
- Position sizing patterns
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .cost_basis import FIFOCost

REPORTS_DIR = Path("reports/reconciliation")


class TradeAnalyzer:
    """Per-stock trade analysis engine.
    
    Reads economic events CSV.
    Produces trade analysis per symbol.
    """

    def __init__(self, events_path: Path = None):
        self.events_path = events_path or REPORTS_DIR / "broker_economic_event_v4.csv"
        self._events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        with open(self.events_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row["_date_int"] = int(row["date"]) if row["date"].isdigit() else 0
                row["_cash_impact"] = float(row["cash_impact"])
                row["_total_amount"] = float(row["total_amount"])
                row["_qty"] = float(row["qty"])
                row["_price"] = float(row["price"])
                row["_commission"] = float(row["commission"])
                self._events.append(row)
        self._events.sort(key=lambda r: (r["_date_int"], r["event_id"]))

    def analyze(self) -> dict[str, Any]:
        """Run full analysis per stock."""
        # Per-symbol trade tracking
        symbols: dict[str, dict[str, Any]] = {}
        
        for e in self._events:
            et = e["event_type"]
            sym = e["symbol"]
            if not sym or et not in ("BUY", "SELL"):
                continue
            
            if sym not in symbols:
                symbols[sym] = {
                    "symbol": sym,
                    "name": e["name"],
                    "buys": [],
                    "sells": [],
                    "total_buy_qty": 0.0,
                    "total_sell_qty": 0.0,
                    "total_buy_cost": 0.0,
                    "total_sell_proceeds": 0.0,
                    "total_commission": 0.0,
                    "first_trade": e["date"],
                    "last_trade": e["date"],
                }
            
            s = symbols[sym]
            if e["name"]:
                s["name"] = e["name"]
            
            d = e["date"]
            if len(d) == 8:
                d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            if d < s["first_trade"] if len(d) == 10 else True:
                s["first_trade"] = d
            if d > s["last_trade"]:
                s["last_trade"] = d
            
            if et == "BUY":
                s["buys"].append({
                    "date": d, "qty": e["_qty"], "price": e["_price"],
                    "total": e["_total_amount"], "commission": e["_commission"],
                })
                s["total_buy_qty"] += e["_qty"]
                s["total_buy_cost"] += e["_total_amount"]
                s["total_commission"] += e["_commission"]
            elif et == "SELL":
                s["sells"].append({
                    "date": d, "qty": e["_qty"], "price": e["_price"],
                    "total": e["_total_amount"], "commission": e["_commission"],
                })
                s["total_sell_qty"] += e["_qty"]
                s["total_sell_proceeds"] += e["_total_amount"]
                s["total_commission"] += e["_commission"]
        
        # Calculate per-symbol metrics
        results = []
        for sym, s in sorted(symbols.items()):
            buy_count = len(s["buys"])
            sell_count = len(s["sells"])
            total_trades = buy_count + sell_count
            
            # Current position
            remaining_qty = s["total_buy_qty"] - s["total_sell_qty"]
            
            # Average buy price (weighted by qty)
            if s["total_buy_qty"] > 0:
                avg_buy_price = s["total_buy_cost"] / s["total_buy_qty"]
            else:
                avg_buy_price = 0.0
            
            if s["total_sell_qty"] > 0:
                avg_sell_price = s["total_sell_proceeds"] / s["total_sell_qty"]
            else:
                avg_sell_price = 0.0
            
            # Net cash flow from this stock
            net_cash = s["total_sell_proceeds"] - s["total_buy_cost"]
            
            # Total commission
            total_comm = s["total_commission"]
            
            # Holding period estimation
            holding_periods = []
            for sell in s["sells"]:
                sell_date = sell["date"]
                # Find the closest buy before this sell
                best_buy = None
                for buy in s["buys"]:
                    if buy["date"] <= sell_date:
                        if best_buy is None or buy["date"] > best_buy["date"]:
                            best_buy = buy
                if best_buy:
                    try:
                        bd = datetime.strptime(best_buy["date"], "%Y-%m-%d")
                        sd = datetime.strptime(sell_date, "%Y-%m-%d")
                        holding_periods.append((sd - bd).days)
                    except ValueError:
                        pass
            
            avg_holding = sum(holding_periods) / len(holding_periods) if holding_periods else 0
            
            # Trade interval (avg days between trades)
            trade_dates = []
            for b in s["buys"]:
                try:
                    trade_dates.append(datetime.strptime(b["date"], "%Y-%m-%d"))
                except ValueError:
                    pass
            for sl in s["sells"]:
                try:
                    trade_dates.append(datetime.strptime(sl["date"], "%Y-%m-%d"))
                except ValueError:
                    pass
            trade_dates.sort()
            
            if len(trade_dates) >= 2:
                total_span = (trade_dates[-1] - trade_dates[0]).days
                avg_interval = total_span / (len(trade_dates) - 1) if len(trade_dates) > 1 else 0
            else:
                avg_interval = 0
            
            # Win/loss analysis (each sell vs its buy cost)
            wins = 0
            losses = 0
            total_profit = 0.0
            total_loss = 0.0
            
            fifo = FIFOCost()
            for e in self._events:
                if e["symbol"] != sym:
                    continue
                et = e["event_type"]
                if et == "BUY":
                    fifo.buy(e["_qty"], e["_price"], e["_commission"])
                elif et == "SELL":
                    try:
                        result = fifo.sell(e["_qty"], e["_price"], e["_commission"])
                        rpnl = result["realized_pnl"]
                        if rpnl > 0:
                            wins += 1
                            total_profit += rpnl
                        elif rpnl < 0:
                            losses += 1
                            total_loss += abs(rpnl)
                    except Exception:
                        pass
            
            win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
            profit_factor = total_profit / total_loss if total_loss > 0 else 0.0
            
            results.append({
                "symbol": sym,
                "name": s["name"],
                "first_trade": s["first_trade"],
                "last_trade": s["last_trade"],
                "trade_span_days": total_span if len(trade_dates) >= 2 else 0,
                "buy_count": buy_count,
                "sell_count": sell_count,
                "total_trades": total_trades,
                "total_buy_qty": round(s["total_buy_qty"], 0),
                "total_sell_qty": round(s["total_sell_qty"], 0),
                "remaining_qty": round(remaining_qty, 0),
                "avg_buy_price": round(avg_buy_price, 4),
                "avg_sell_price": round(avg_sell_price, 4),
                "total_buy_cost": round(s["total_buy_cost"], 2),
                "total_sell_proceeds": round(s["total_sell_proceeds"], 2),
                "net_cash_flow": round(net_cash, 2),
                "total_commission": round(total_comm, 2),
                "avg_holding_days": round(avg_holding, 1),
                "avg_interval_days": round(avg_interval, 1),
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "profit_factor": round(profit_factor, 2),
                "total_profit": round(total_profit, 2),
                "total_loss": round(total_loss, 2),
                "net_realized_pnl": round(total_profit - total_loss, 2),
            })
        
        return {
            "total_symbols": len(results),
            "total_trades": sum(r["total_trades"] for r in results),
            "total_commission": sum(r["total_commission"] for r in results),
            "total_realized_pnl": sum(r["net_realized_pnl"] for r in results),
            "symbols": results,
        }


if __name__ == "__main__":
    analyzer = TradeAnalyzer()
    result = analyzer.analyze()
    
    print("=== Phase I3: Trade Analysis ===\n")
    print(f"Symbols analyzed: {result['total_symbols']}")
    print(f"Total trades: {result['total_trades']} (BUY+SELL)")
    print(f"Total commission: {result['total_commission']:,.2f}")
    print(f"Net realized P&L: {result['total_realized_pnl']:+,.2f}")
    print()
    
    print(f"{'Symbol':8s} {'Name':14s} {'Trades':>6s} {'W/L':>8s} {'Win%':>6s} {'Hold':>6s} {'NetPnL':>10s} {'Comm':>8s}")
    print("-" * 75)
    for s in result["symbols"]:
        wl = f"{s['wins']}/{s['losses']}"
        print(f"{s['symbol']:8s} {s['name']:14s} {s['total_trades']:>6d} {wl:>8s} "
              f"{s['win_rate']*100:>5.0f}% {s['avg_holding_days']:>5.0f}d "
              f"{s['net_realized_pnl']:>+9.0f} {s['total_commission']:>7.0f}")
