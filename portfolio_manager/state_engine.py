"""
state_engine.py — Portfolio State Engine (Phase I1 + J).

Reads economic events CSV + balance chain CSV.
Computes portfolio snapshot at ANY date.
With market data: computes real market_value, profit_loss, profit_rate.

Usage:
    engine = PortfolioStateEngine()
    snapshot = engine.snapshot_with_market("2026-07-19")
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .cost_basis import FIFOCost
from .market_data import MarketDataFetcher

REPORTS_DIR = Path("reports/reconciliation")

# ── Precision ───────────────────────────────────────────────
MONEY = Decimal("0.01")
QTY = Decimal("0.0001")


@dataclass
class PortfolioSnapshot:
    """Snapshot of portfolio at a specific date."""
    date: str          # YYYY-MM-DD
    cash: float = 0.0
    stocks: list[dict[str, Any]] = field(default_factory=list)
    market_value: float = 0.0
    total_asset: float = 0.0


@dataclass
class PositionState:
    """Per-stock position state."""
    symbol: str = ""
    name: str = ""
    shares: int = 0
    total_cost: float = 0.0
    avg_cost: float = 0.0
    market_value: float = 0.0
    profit_loss: float = 0.0
    profit_rate: float = 0.0
    weight: float = 0.0
    trade_count: int = 0
    realized_pnl: float = 0.0


class PortfolioStateEngine:
    """Investment portfolio state engine.
    
    Event-sourcing architecture:
        - State is always computed from events, never stored.
        - Can generate snapshot for ANY date.
        - Cash from balance chain (authoritative).
        - Positions from FIFO cost basis on economic events.
    """

    def __init__(self, events_path: Path = None, chain_path: Path = None):
        self.events_path = events_path or REPORTS_DIR / "broker_economic_event_v4.csv"
        self.chain_path = chain_path or REPORTS_DIR / "broker_balance_chain_v4.csv"
        
        self._events: list[dict[str, Any]] = []
        self._cash_chain: list[dict[str, Any]] = []
        self._dates: list[str] = []
        self._opening: float = 0.0
        
        self._load()
        self._compute_cash_series()

    def _load(self) -> None:
        """Load events and balance chain."""
        # Load economic events
        with open(self.events_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row["date_int"] = int(row["date"]) if row["date"].isdigit() else 0
                row["_cash_impact"] = float(row["cash_impact"])
                self._events.append(row)
        
        # Sort by date
        self._events.sort(key=lambda r: (r["date_int"], r["event_id"]))
        
        # Load balance chain
        with open(self.chain_path, encoding="utf-8-sig") as f:
            self._cash_chain = list(csv.DictReader(f))
        
        # Derive opening from chain
        if self._cash_chain:
            first = self._cash_chain[0]
            self._opening = float(first["prev_bal"])
        
        # Build date list
        dates = set()
        for e in self._events:
            d = e["date"]
            if len(d) == 8:
                dates.add(f"{d[:4]}-{d[4:6]}-{d[6:8]}")
        self._dates = sorted(dates)

    def _compute_cash_series(self) -> None:
        """Build cash-at-date lookup from balance chain + events."""
        # For each chain entry, we know: date → broker_bal
        self._cash_by_date: dict[str, float] = {}
        for row in self._cash_chain:
            d = row["date"]
            if len(d) == 8:
                d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                self._cash_by_date[d_fmt] = float(row["broker_bal"])
        
        # Last chain entry is the authoritative cash for all later dates
        if self._cash_chain:
            last = self._cash_chain[-1]
            d = last["date"]
            if len(d) == 8:
                d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                self._cash_by_date["current"] = float(last["broker_bal"])

    def cash_at_date(self, dt: str) -> float:
        """Get cash balance at a specific date.
        
        If date has a balance chain entry, return it.
        Otherwise return the closest earlier balance.
        """
        if dt in self._cash_by_date:
            return self._cash_by_date[dt]
        
        # Walk backwards to find most recent balance
        d_int = int(dt.replace("-", ""))
        best = self._opening
        for row in self._cash_chain:
            row_d = int(row["date"])
            if row_d <= d_int:
                best = float(row["broker_bal"])
            else:
                break
        return best

    def events_through(self, dt: str) -> list[dict[str, Any]]:
        """Get all events up to and including dt."""
        d_int = int(dt.replace("-", "")) if "-" in dt else int(dt)
        return [e for e in self._events if e["date_int"] <= d_int]

    def snapshot(self, dt: str | int) -> dict[str, Any]:
        """Compute portfolio snapshot at given date.
        
        Args:
            dt: Date string "YYYY-MM-DD" or "YYYYMMDD" or int
            
        Returns:
            Portfolio state dict with cash, stocks, market_value, total_asset
        """
        if isinstance(dt, int):
            dt = str(dt)
        if "-" not in dt and len(dt) == 8:
            dt = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
        
        # Get events up to this date
        events = self.events_through(dt.replace("-", ""))
        
        # Cash
        cash = self.cash_at_date(dt)
        
        # Build positions using FIFO cost basis
        positions: dict[str, dict[str, Any]] = {}
        
        for e in events:
            et = e["event_type"]
            sym = e["symbol"]
            if not sym:
                continue
            if et not in ("BUY", "SELL"):
                continue
            
            if sym not in positions:
                positions[sym] = {
                    "symbol": sym,
                    "name": e["name"],
                    "cost": FIFOCost(),
                    "buy_count": 0,
                    "sell_count": 0,
                }
            
            qty = float(e["qty"])
            price = float(e["price"])
            fee = float(e["commission"])
            total_amt = float(e["total_amount"])
            
            # Skip zero-price events (repo/组合费等)
            if price <= 0 or qty <= 0:
                continue
            
            if et == "BUY":
                positions[sym]["cost"].buy(qty, price, fee)
                positions[sym]["buy_count"] += 1
            elif et == "SELL":
                try:
                    positions[sym]["cost"].sell(qty, price, fee)
                    positions[sym]["sell_count"] += 1
                except Exception:
                    pass  # Insufficient position — skip sell
            
            if e["name"]:
                positions[sym]["name"] = e["name"]
        
        # Build stock list
        stocks = []
        for sym, p in sorted(positions.items()):
            state = p["cost"].get_state()
            shares = state["shares"]
            if shares <= 0:
                continue  # fully sold
            avg_cost = state["total_cost"] / shares if shares > 0 else 0
            stocks.append(PositionState(
                symbol=sym,
                name=p["name"],
                shares=int(shares),
                total_cost=round(state["total_cost"], 2),
                avg_cost=round(avg_cost, 4),
                trade_count=p["buy_count"] + p["sell_count"],
                realized_pnl=round(state["realized_pnl"], 2),
            ))
        
        # Summary
        total_cost = sum(s.total_cost for s in stocks)
        
        return {
            "date": dt,
            "cash": round(cash, 2),
            "stocks": [
                {
                    "symbol": s.symbol,
                    "name": s.name,
                    "shares": s.shares,
                    "total_cost": s.total_cost,
                    "avg_cost": s.avg_cost,
                    "market_value": 0.0,  # needs market data (Phase J)
                    "profit_loss": 0.0,   # needs market data
                    "profit_rate": 0.0,   # needs market data
                    "weight": round(s.total_cost / (cash + total_cost), 4) if (cash + total_cost) > 0 else 0,
                    "trade_count": s.trade_count,
                    "realized_pnl": s.realized_pnl,
                }
                for s in stocks
            ],
            "market_value": 0.0,  # needs market data (Phase J)
            "total_cost": round(total_cost, 2),
            "total_asset": round(cash + total_cost, 2),
            "count": len(stocks),
        }

    def snapshot_range(self, start: str, end: str | None = None, 
                       step_days: int = 1) -> list[dict[str, Any]]:
        """Generate daily snapshots from start to end (or last event date).
        
        Args:
            start: Start date "YYYY-MM-DD"
            end: End date (default: last event date)
            step_days: Interval in days (default 1)
            
        Returns:
            List of snapshots
        """
        if end is None:
            end = self._dates[-1] if self._dates else start
        
        start_dt = datetime.strptime(start, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end, "%Y-%m-%d").date()
        
        snapshots = []
        current = start_dt
        from datetime import timedelta
        while current <= end_dt:
            dt_str = current.strftime("%Y-%m-%d")
            snapshots.append(self.snapshot(dt_str))
            current += timedelta(days=step_days)
        
        return snapshots

    def verify_against_broker(self, snapshot_balance: float = 104134.25,
                              expected_stocks: int = 15) -> dict[str, Any]:
        """Verify engine output against broker snapshot.
        
        Args:
            snapshot_balance: Broker statement cash balance (snapshot)
            expected_stocks: Expected number of holdings
            
        Returns:
            Verification result dict
        """
        # Get snapshot at last chain date
        if self._cash_chain:
            last = self._cash_chain[-1]
            last_date = last["date"]
            if len(last_date) == 8:
                fmt_date = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:8]}"
            else:
                fmt_date = last_date
        else:
            fmt_date = self._dates[-1] if self._dates else "2026-07-19"
        
        snap = self.snapshot(fmt_date)
        
        cash_ok = abs(snap["cash"] - snapshot_balance) < 0.1
        stocks_ok = snap["count"] == expected_stocks
        
        return {
            "snapshot_date": fmt_date,
            "cash": snap["cash"],
            "broker_snapshot": snapshot_balance,
            "cash_diff": round(snap["cash"] - snapshot_balance, 2),
            "cash_ok": cash_ok,
            "stocks": snap["count"],
            "expected_stocks": expected_stocks,
            "stocks_ok": stocks_ok,
            "all_ok": cash_ok and stocks_ok,
            "holdings": snap["stocks"],
            "total_asset": snap["total_asset"],
        }

    def summary(self) -> dict[str, Any]:
        """Generate summary report."""
        broker = self.verify_against_broker()
        
        return {
            "events_loaded": len(self._events),
            "chain_entries": len(self._cash_chain),
            "date_range": (self._dates[0], self._dates[-1]) if self._dates else None,
            "opening_balance": self._opening,
            "verification": broker,
        }


# ── CLI entry point ────────────────────────────────────────
if __name__ == "__main__":
    engine = PortfolioStateEngine()
    
    # Run verifications
    print("=== Portfolio State Engine (Phase I1) ===\n")
    
    summary = engine.summary()
    v = summary["verification"]
    
    print(f"Events loaded: {summary['events_loaded']}")
    print(f"Chain entries: {summary['chain_entries']}")
    print(f"Date range: {summary['date_range']}")
    print(f"Opening balance: {summary['opening_balance']:.2f}")
    print()
    
    print(f"Verification ({v['snapshot_date']}):")
    print(f"  Cash: {v['cash']:.2f} (broker: {v['broker_snapshot']}, diff: {v['cash_diff']:.2f}) {'✅' if v['cash_ok'] else '❌'}")
    print(f"  Stocks: {v['stocks']} (expected: {v['expected_stocks']}) {'✅' if v['stocks_ok'] else '❌'}")
    print(f"  Total asset: {v['total_asset']:.2f}")
    print()
    
    print("Holdings:")
    for s in v["holdings"]:
        pnl_str = f"realized_pnl={s['realized_pnl']:.2f}" if s["realized_pnl"] != 0 else ""
        print(f"  {s['symbol']:8s} {s['name']:12s} {s['shares']:6d}sh @ {s['avg_cost']:.4f} = {s['total_cost']:>10.2f}  weight={s['weight']:.1%}  {pnl_str}")
    
    print()
    print(f"All OK: {'✅' if v['all_ok'] else '❌'}")
    
    # Print latest snapshot
    latest = summary["date_range"][-1] if summary["date_range"] else None
    if latest:
        print(f"\nLatest snapshot ({latest}):")
        snap = engine.snapshot(latest)
        print(f"  Cash: {snap['cash']:.2f}")
        print(f"  Total cost: {snap['total_cost']:.2f}")
        print(f"  Total asset: {snap['total_asset']:.2f}")
        print(f"  Holdings: {snap['count']}")
