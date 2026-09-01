"""
market_data.py — Market data fetcher (Phase J).

Fetches real-time prices for:
- CN A-shares + ETFs: via akshare (东方财富)
- HK stocks: via yfinance

Caches results to avoid repeated network calls.
Maps codes: CN (600xxx, 688xxx, 159xxx, 51xxxx, 52xxxx), HK (00xxx, 01xxx, etc.)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

CACHE_DIR = Path("data/market_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def is_hk_symbol(symbol: str) -> bool:
    """判断账本代码是否为港股。

    港股账本代码为 5 位数字且以 0 开头（如 00700、09988）；
    6 位代码（000/002/300 等深市、600 等沪市）一律为 A 股。
    """
    s = str(symbol)
    return len(s) == 5 and s.startswith("0")


class MarketDataFetcher:
    """Fetch market prices for portfolio holdings."""

    def __init__(self, use_cache: bool = True, intraday: bool = False):
        self.use_cache = use_cache
        self.intraday = intraday
        self._cn_cache: dict[str, float] = {}
        self._hk_cache: dict[str, float] = {}
        self._cn_fundamentals: dict[str, dict[str, Any]] = {}
        self._last_fetch: str | None = None

    def _is_market_hours_cn(self) -> bool:
        """Check if CN market is currently in trading session."""
        now = datetime.now()
        if now.weekday() >= 5:  # Sat/Sun
            return False
        t = now.hour * 100 + now.minute
        # Morning: 09:30-11:30, Afternoon: 13:00-15:00
        in_morning = 930 <= t <= 1130
        in_afternoon = 1300 <= t <= 1500
        return in_morning or in_afternoon

    def _should_refresh(self) -> bool:
        """Check if cached data needs refreshing for intraday."""
        if not self.intraday or not self._is_market_hours_cn():
            return False
        # Read timestamp from cache file
        mtime = 0.0
        for name in ["cn_prices", "hk_prices"]:
            path = self._cache_path(name)
            if path.exists():
                mtime = max(mtime, path.stat().st_mtime)
        if mtime <= 0:
            return True  # no cache → must fetch
        # During market hours, refresh every 10 min
        import time
        elapsed = time.time() - mtime
        return elapsed > 600

    def _cache_path(self, name: str) -> Path:
        return CACHE_DIR / f"{name}.json"

    def _load_cache(self, name: str) -> dict[str, Any] | None:
        path = self._cache_path(name)
        if not path.exists() or not self.use_cache:
            return None
        with open(path) as f:
            data = json.load(f)
        cached_date = data.get("_date", "")
        if cached_date != date.today().isoformat():
            return None  # stale
        return data

    def _save_cache(self, name: str, data: dict[str, Any]) -> None:
        data["_date"] = date.today().isoformat()
        data["_timestamp"] = datetime.now().isoformat()
        with open(self._cache_path(name), "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_cache(self, name: str) -> dict[str, Any] | None:
        """Load cache, respecting intraday refresh logic."""
        path = self._cache_path(name)
        if not path.exists() or not self.use_cache:
            return None
        with open(path) as f:
            data = json.load(f)
        cached_date = data.get("_date", "")
        if cached_date != date.today().isoformat():
            return None  # stale (yesterday or older)
        if self._should_refresh():
            return None  # intraday: time to refresh
        return data

    def fetch_prices(self, symbols: list[dict[str, str]]) -> dict[str, float]:
        """Fetch current prices for a list of symbols.
        
        Uses full market pool cache for first fetch, then individual queries
        for intraday updates.
        
        Args:
            symbols: [{"symbol": "600415", "market": "CN"}, ...]
            
        Returns: {symbol: price, ...}
        """
        result: dict[str, float] = {}
        
        # Try cache first
        cn_cache = self._load_cache("cn_prices")
        hk_cache = self._load_cache("hk_prices")
        
        if cn_cache:
            for s in symbols:
                if s["market"] == "CN" and s["symbol"] in cn_cache:
                    try:
                        result[s["symbol"]] = float(cn_cache[s["symbol"]])
                    except (ValueError, TypeError):
                        pass
        
        if hk_cache:
            for s in symbols:
                if s["market"] == "HK" and s["symbol"] in hk_cache:
                    try:
                        result[s["symbol"]] = float(hk_cache[s["symbol"]])
                    except (ValueError, TypeError):
                        pass
        
        # Get missing symbols from full pool
        missing_cn = [s["symbol"] for s in symbols 
                     if s["market"] == "CN" and s["symbol"] not in result]
        missing_hk = [s["symbol"] for s in symbols 
                     if s["market"] == "HK" and s["symbol"] not in result]
        
        # Fast intraday refresh: if cache exists, just re-fetch portfolio symbols
        # (avoids downloading the entire 5000-stock market pool every time)
        # Fallback: fetch from full pool if missing
        if missing_cn:
            cn_prices = self._fetch_cn_prices(missing_cn)
            result.update(cn_prices)
        if missing_hk:
            hk_prices = self._fetch_hk_prices(missing_hk)
            result.update(hk_prices)
        
        return result

    def _fetch_cn_prices(self, symbols: list[str]) -> dict[str, float]:
        """Fetch CN A-share prices via akshare (东方财富)."""
        # Check cache first
        cached = self._load_cache("cn_prices")
        if cached:
            return {sym: cached.get(sym, 0.0) for sym in symbols}
        
        try:
            import akshare as ak
            
            # Get all A-share real-time quotes
            df = ak.stock_zh_a_spot_em()
            # Columns: 代码, 名称, 最新价, 涨跌幅, etc.
            price_map: dict[str, float] = {}
            
            # Map the dataframe
            for _, row in df.iterrows():
                code = str(row["代码"])
                price = float(row.get("最新价", 0) or 0)
                if code in symbols and price > 0:
                    price_map[code] = price
            
            # Try etf as fallback
            missing = [s for s in symbols if s not in price_map]
            if missing:
                try:
                    etf_df = ak.fund_etf_spot_em()
                    for _, row in etf_df.iterrows():
                        code = str(row.get("代码", ""))
                        price = float(row.get("最新价", 0) or 0)
                        if code in missing and price > 0:
                            price_map[code] = price
                except Exception:
                    pass
            
            self._save_cache("cn_prices", price_map)
            
            return {sym: price_map.get(sym, 0.0) for sym in symbols}
            
        except ImportError:
            return {}
        except Exception as e:
            print(f"[WARN] CN price fetch failed: {e}")
            return {}

    def _fetch_hk_prices(self, symbols: list[str]) -> dict[str, float]:
        """Fetch HK stock prices via akshare (东方财富)."""
        cached = self._load_cache("hk_prices")
        if cached:
            return {sym: cached.get(sym, 0.0) for sym in symbols}
        
        try:
            import akshare as ak
            
            df = ak.stock_hk_spot_em()
            price_map: dict[str, float] = {}
            
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if code in symbols:
                    price = float(row.get("最新价", 0) or 0)
                    if price > 0:
                        price_map[code] = price
            
            self._save_cache("hk_prices", price_map)
            return {sym: price_map.get(sym, 0.0) for sym in symbols}
            
        except ImportError:
            return {}
        except Exception as e:
            print(f"[WARN] HK price fetch failed: {e}")
            return {}

    def fetch_fundamentals(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch PE, PB, ROE for CN A-shares from spot data."""
        cached = self._load_cache("cn_fundamentals")
        if cached:
            return {s: cached.get(s, {}) for s in symbols}
        
        try:
            import akshare as ak
            
            df = ak.stock_zh_a_spot_em()
            result: dict[str, dict[str, Any]] = {}
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if code in symbols:
                    result[code] = {
                        "pe": float(row.get("市盈率-动态", 0) or 0),
                        "pb": float(row.get("市净率", 0) or 0),
                        "turnover": float(row.get("换手率", 0) or 0),
                        "amplitude": float(row.get("振幅", 0) or 0),
                        "high": float(row.get("最高", 0) or 0),
                        "low": float(row.get("最低", 0) or 0),
                        "volume": float(row.get("成交量", 0) or 0),
                    }
            
            self._save_cache("cn_fundamentals", result)
            return result
            
        except ImportError:
            return {}
        except Exception as e:
            print(f"[WARN] Fundamentals fetch failed: {e}")
            return {}


# ── CLI test ────────────────────────────────────────
if __name__ == "__main__":
    fetcher = MarketDataFetcher(use_cache=True)
    
    cn_test = [
        {"symbol": "600415", "market": "CN"},
        {"symbol": "600597", "market": "CN"},
        {"symbol": "601318", "market": "CN"},
        {"symbol": "688036", "market": "CN"},
        {"symbol": "159330", "market": "CN"},
        {"symbol": "513010", "market": "CN"},
    ]
    hk_test = [
        {"symbol": "00700", "market": "HK"},
        {"symbol": "09988", "market": "HK"},
        {"symbol": "01024", "market": "HK"},
    ]
    
    all_test = cn_test + hk_test
    
    print("=== Phase J: Market Data Test ===\n")
    print("Fetching prices...")
    prices = fetcher.fetch_prices(all_test)
    
    print(f"{'Symbol':>8s} {'Market':>6s} {'Price':>10s}")
    print("-" * 28)
    for s in all_test:
        sym = s["symbol"]
        price = prices.get(sym, 0.0)
        print(f"{sym:>8s} {s['market']:>6s} {price:>10.4f}" if price > 0 else f"{sym:>8s} {s['market']:>6s} {'N/A':>10s}")
    
    print("\nFetching fundamentals (CN)...")
    fundamentals = fetcher.fetch_fundamentals([s["symbol"] for s in cn_test])
    print(f"{'Symbol':>8s} {'PE':>8s} {'PB':>8s} {'ROE':>8s}")
    print("-" * 36)
    for s in cn_test:
        sym = s["symbol"]
        f = fundamentals.get(sym, {})
        pe = f.get("pe", 0)
        pb = f.get("pb", 0)
        roe = f.get("roe", 0)
        print(f"{sym:>8s} {pe:>8.2f} {pb:>8.2f} {roe:>7.1f}%" if pe > 0 else f"{sym:>8s} {'N/A':>8s} {'N/A':>8s} {'N/A':>8s}")
