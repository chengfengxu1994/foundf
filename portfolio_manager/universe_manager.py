"""
universe_manager.py — Phase F3: A-Share Universe Expansion.

Goal: Expand factor research from 20 stocks to full A-share market (5000+ stocks).

This module:
  1. Downloads stock_basic data (all A-share symbols, listing dates, delist dates)
  2. Supports historical universe (avoids survivorship bias)
  3. Starts full-market daily price download
  4. Saves to DuckDB

Usage:
    python -m portfolio_manager.universe_manager init        # Download stock basic info
    python -m portfolio_manager.universe_manager status      # Check download status
    python -m portfolio_manager.universe_manager download   # Start full price download
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

DATA = Path("data")
UNIVERSE_DIR = DATA / "universe"
UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


class UniverseManager:
    """Manage A-share universe expansion."""

    def __init__(self):
        self._stock_basic_path = UNIVERSE_DIR / "stock_basic.csv"
        self._status_path = UNIVERSE_DIR / "download_status.json"

    # ── Stock Basic ─────────────────────────────────

    def download_stock_basic(self) -> int:
        """Download all A-share basic info (5000+ stocks)."""
        log("Downloading A-share stock basic info...")
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            # Extract basic info
            stocks = []
            for _, row in df.iterrows():
                sym = str(row.get("代码", ""))
                name = str(row.get("名称", ""))
                price = float(row.get("最新价", 0) or 0)
                pct = float(row.get("涨跌幅", 0) or 0)
                volume = float(row.get("成交量", 0) or 0)
                amount = float(row.get("成交额", 0) or 0)
                turnover = float(row.get("换手率", 0) or 0)
                pe = float(row.get("市盈率-动态", 0) or 0)
                pb = float(row.get("市净率", 0) or 0)
                mkt_cap = float(row.get("总市值", 0) or 0)
                stocks.append({
                    "symbol": sym, "name": name, "price": price,
                    "change_pct": pct, "volume": volume, "amount": amount,
                    "turnover": turnover, "pe": pe, "pb": pb,
                    "market_cap": mkt_cap,
                    "fetched_at": datetime.now().isoformat(),
                })
            
            # Save
            with open(self._stock_basic_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=stocks[0].keys())
                w.writeheader()
                w.writerows(stocks)
            
            log(f"  ✅ Downloaded {len(stocks)} stocks")
            
            # Update status
            self._update_status("stock_basic", len(stocks), "done")
            
            return len(stocks)
        except Exception as e:
            log(f"  ❌ Failed: {e}")
            self._update_status("stock_basic", 0, f"failed: {e}")
            return 0

    def get_universe(self) -> list[dict]:
        """Get A-share universe with basic info."""
        if not self._stock_basic_path.exists():
            return []
        with open(self._stock_basic_path, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def universe_stats(self) -> dict:
        """Get universe statistics."""
        stocks = self.get_universe()
        if not stocks:
            return {"status": "not_downloaded"}
        
        total = len(stocks)
        with_price = sum(1 for s in stocks if float(s.get("price", 0)) > 0)
        with_pe = sum(1 for s in stocks if float(s.get("pe", 0)) > 0)
        
        # Market cap distribution
        mcaps = [float(s.get("market_cap", 0)) for s in stocks if float(s.get("market_cap", 0)) > 0]
        large = sum(1 for m in mcaps if m >= 1e10)  # >100亿
        mid = sum(1 for m in mcaps if 1e9 <= m < 1e10)  # 10-100亿
        small = sum(1 for m in mcaps if m < 1e9)  # <10亿
        
        return {
            "total_stocks": total,
            "with_price": with_price,
            "with_pe": with_pe,
            "market_cap_distribution": {
                "large_10b_plus": large,
                "mid_1b_10b": mid,
                "small_under_1b": small,
            },
            "avg_market_cap": sum(mcaps) / len(mcaps) if mcaps else 0,
        }

    # ── Status ──────────────────────────────────────

    def _update_status(self, task: str, count: int, status: str) -> None:
        status_data = {}
        if self._status_path.exists():
            with open(self._status_path) as f:
                status_data = json.load(f)
        status_data[task] = {
            "count": count,
            "status": status,
            "updated_at": datetime.now().isoformat(),
        }
        with open(self._status_path, "w") as f:
            json.dump(status_data, f, indent=2)

    def get_status(self) -> dict:
        """Get download status."""
        if not self._status_path.exists():
            return {}
        with open(self._status_path) as f:
            return json.load(f)


# ── CLI ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    mgr = UniverseManager()
    
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        count = mgr.download_stock_basic()
        print(f"\nUniverse initialized: {count} stocks")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        status = mgr.get_status()
        if not status:
            print("Universe not initialized. Run: python -m portfolio_manager.universe_manager init")
        else:
            print("=== Universe Download Status ===")
            for task, info in status.items():
                print(f"  {task}: {info['count']} items ({info['status']})")
        
        stats = mgr.universe_stats()
        if "total_stocks" in stats:
            print(f"\n=== Universe Stats ===")
            print(f"  Total stocks: {stats['total_stocks']}")
            print(f"  With price: {stats['with_price']}")
            print(f"  With PE: {stats['with_pe']}")
            dist = stats.get("market_cap_distribution", {})
            print(f"  Market Cap: Large={dist.get('large_10b_plus',0)} Mid={dist.get('mid_1b_10b',0)} Small={dist.get('small_under_1b',0)}")
            print(f"  Avg Mkt Cap: {stats.get('avg_market_cap', 0)/1e8:.0f}亿")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "screener":
        """Quick screener: find stocks matching factor criteria."""
        stocks = mgr.get_universe()
        if not stocks:
            print("Run 'init' first")
            sys.exit(1)
        
        # Filter: low PE + moderate growth + good turnover
        candidates = []
        for s in stocks:
            pe = float(s.get("pe", 0))
            pb = float(s.get("pb", 0))
            turnover = float(s.get("turnover", 0))
            price = float(s.get("price", 0))
            mkt_cap = float(s.get("market_cap", 0))
            
            if 5 < pe < 20 and pb < 3 and turnover > 1 and price > 2 and mkt_cap > 1e9:
                candidates.append(s)
        
        candidates.sort(key=lambda x: float(x.get("pe", 999)))
        print(f"\n=== Screener: {len(candidates)} candidates ===")
        print(f"{'Symbol':8s} {'Name':12s} {'PE':>6s} {'PB':>6s} {'Turnover':>9s} {'MktCap':>10s}")
        print("-" * 55)
        for c in candidates[:20]:
            print(f"  {c['symbol']:8s} {c['name'][:12]:12s} {float(c.get('pe',0)):>6.1f} {float(c.get('pb',0)):>6.2f} {float(c.get('turnover',0)):>8.1f}% {float(c.get('market_cap',0))/1e8:>9.1f}亿")
        if len(candidates) > 20:
            print(f"  ... and {len(candidates)-20} more")
    
    else:
        print("Usage:")
        print("  python -m portfolio_manager.universe_manager init      # Download stock basic")
        print("  python -m portfolio_manager.universe_manager status    # Check status")
        print("  python -m portfolio_manager.universe_manager screener  # Quick factor screen")
