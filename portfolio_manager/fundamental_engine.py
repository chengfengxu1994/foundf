"""
fundamental_engine.py — Phase F1: Financial Data Engine.

Fetches real fundamental data for portfolio holdings:
  - CN A-shares: ROE, revenue, net profit, gross margin, debt ratio, EPS, CF/share
  - HK stocks: PE, PB via yfinance/akshare (spot)
  - US stocks: via existing SEC EDGAR pipeline

Replaces price-based proxies with real financial data.
Saves to DuckDB financial_statement table.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPORTS = Path("reports/reconciliation")
DATA = Path("data")
CACHE_DIR = DATA / "market_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Parsing helpers ──────────────────────────────────

def parse_chinese_number(s: str | None) -> float:
    """Parse Chinese number formats like '123.45亿', '67.89万'."""
    if s is None or s is False or s == "":
        return 0.0
    s = str(s).strip().replace(",", "").replace(" ", "")
    if not s:
        return 0.0
    multiplier = 1.0
    if "亿" in s:
        multiplier = 1e8
        s = s.replace("亿", "")
    elif "万" in s:
        multiplier = 1e4
        s = s.replace("万", "")
    elif "元" in s:
        s = s.replace("元", "")
    if s.endswith("%"):
        return 0.0  # percentage values handled separately
    try:
        return float(s) * multiplier
    except ValueError:
        return 0.0


def parse_percentage(s: str | None) -> float:
    """Parse percentage like '54.27%' to 0.5427."""
    if s is None or s is False or s == "":
        return 0.0
    s = str(s).strip().replace("%", "").replace(" ", "")
    if not s:
        return 0.0
    try:
        return float(s) / 100.0
    except ValueError:
        return 0.0


class FundamentalEngine:
    """Fetch real financial data for portfolio holdings."""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def _estimate_available_date(self, period_end: str) -> str:
        """Estimate when financial data becomes available based on filing rules.
        
        China A-share rules:
        - Annual (12-31): due by April 30 (+120 days)
        - Half-year (06-30): due by August 31 (+62 days)
        - Q1 (03-31): due by April 30 (+30 days)
        - Q3 (09-30): due by October 31 (+31 days)
        """
        try:
            from datetime import datetime as dt, timedelta
            pe = dt.strptime(period_end[:10], "%Y-%m-%d").date()
            month = pe.month
            if month == 12:
                delay = 120  # annual
            elif month == 6:
                delay = 62   # half-year
            elif month == 3:
                delay = 30   # Q1
            elif month == 9:
                delay = 31   # Q3
            else:
                delay = 45
            available = pe + timedelta(days=delay)
            return available.isoformat()
        except Exception:
            return period_end  # fallback: use period end date

    def fetch_all(self, symbols: list[dict]) -> dict[str, dict]:
        """Fetch fundamentals for all symbols.
        
        Args:
            symbols: [{"symbol": "600415", "market": "CN"}, ...]
            
        Returns: {symbol: {field: value, ...}}
        """
        result = {}
        cn_symbols = [s["symbol"] for s in symbols if s.get("market") == "CN"]
        hk_symbols = [s["symbol"] for s in symbols if s.get("market") == "HK"]
        
        if cn_symbols:
            result.update(self._fetch_cn(cn_symbols))
        if hk_symbols:
            result.update(self._fetch_hk(hk_symbols))
        
        # Save to CSV for downstream use
        self._save_csv(result)
        
        return result

    def _fetch_cn(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch CN A-share fundamentals from akshare."""
        result = {}
        
        # First get spot PE/PB from existing cache
        try:
            from portfolio_manager.market_data import MarketDataFetcher
            mdf = MarketDataFetcher(use_cache=True)
            pe_pb = mdf.fetch_fundamentals(symbols)
        except Exception:
            pe_pb = {}
        
        # Then fetch real financial data from akshare
        import akshare as ak
        
        for sym in symbols:
            try:
                # Fetch from akshare (同花顺 financial abstract)
                time.sleep(0.3)  # rate limit
                df = ak.stock_financial_abstract_ths(symbol=sym)
                if df.empty:
                    continue
                
                # Get the latest (last) row — akshare returns oldest first
                df = df.sort_index(ascending=True)  # ensure oldest first
                row = df.iloc[-1]  # latest
                latest = row.to_dict()
                
                # Get previous period for YoY comparison
                prev = df.iloc[-2].to_dict() if df.shape[0] > 1 else {}
                
                # Parse key metrics
                report_period = str(latest.get("报告期", ""))[:10]
                
                entry = {
                    # Spot PE/PB
                    "pe": pe_pb.get(sym, {}).get("pe", 0.0),
                    "pb": pe_pb.get(sym, {}).get("pb", 0.0),
                    
                    # Income statement
                    "revenue": parse_chinese_number(latest.get("营业总收入")),
                    "net_profit": parse_chinese_number(latest.get("净利润")),
                    "revenue_growth": parse_percentage(latest.get("营业总收入同比增长率")),
                    "net_profit_growth": parse_percentage(latest.get("净利润同比增长率")),
                    
                    # Profitability
                    "roe": parse_percentage(latest.get("净资产收益率")),
                    "gross_margin": parse_percentage(latest.get("销售毛利率")),
                    "net_margin": parse_percentage(latest.get("销售净利率")),
                    
                    # Per share
                    "eps": parse_chinese_number(latest.get("基本每股收益")),
                    "bvps": parse_chinese_number(latest.get("每股净资产")),
                    "cf_per_share": parse_chinese_number(latest.get("每股经营现金流")),
                    
                    # Financial health
                    "debt_ratio": parse_percentage(latest.get("资产负债率")),
                    "current_ratio": parse_chinese_number(latest.get("流动比率")),
                    
                    # Point-in-time
                    "report_period": report_period,
                    "period_end": report_period,
                    "available_date": self._estimate_available_date(report_period),
                    "source": "akshare_stock_financial_abstract_ths",
                    "fetched_at": datetime.now().isoformat(),
                }
                
                # Calculate derived metrics
                entry["market_cap"] = round(self._estimate_market_cap(sym), 2)
                if entry["roe"] > 0 and entry["pe"] > 0:
                    entry["peg"] = round(entry["pe"] / (entry["roe"] * 100), 2) if entry["roe"] > 0 else 0
                
                result[sym] = entry
                name = latest.get("报告期", "?")
                print(f"    ✅ {sym}: ROE={entry['roe']:.1%} Rev={entry['revenue']/1e8:.1f}亿 ({name})")
                
            except Exception as e:
                print(f"    ⚠️  {sym}: {type(e).__name__}")
                continue
        
        return result

    def _fetch_hk(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch HK stock fundamentals from akshare (东方财富)."""
        result = {}
        try:
            import akshare as ak
            
            # Get HK spot data with PE/PB
            df = ak.stock_hk_spot_em()
            cols = list(df.columns)
            
            for sym in symbols:
                # Find the row
                row_data = df[df["代码"] == sym]
                if row_data.empty:
                    # Try stripping leading zeros (yfinance format)
                    sym_clean = sym.lstrip("0")
                    row_data = df[df["代码"] == sym_clean]
                if row_data.empty:
                    continue
                
                row = row_data.iloc[0]
                entry = {
                    "pe": float(row.get("市盈率-动态", 0) or 0),
                    "pb": float(row.get("市净率", 0) or 0),
                    "market_cap": float(row.get("总市值", 0) or 0),
                    "amp": float(row.get("振幅", 0) or 0),
                    "turnover": float(row.get("换手率", 0) or 0),
                    "source": "akshare_hk_spot",
                    "fetched_at": datetime.now().isoformat(),
                }
                
                # Get name from row
                entry["name"] = str(row.get("名称", ""))
                
                result[sym] = entry
                print(f"    ✅ {sym}: PE={entry['pe']:.1f} PB={entry['pb']:.2f}")
            
            # For symbols without PE, try yfinance as fallback
            missing_pe = [s for s in symbols if s not in result or result[s].get("pe", 0) == 0]
            if missing_pe:
                try:
                    import yfinance as yf
                    for sym in missing_pe:
                        try:
                            yf_sym = sym.lstrip("0").zfill(4)  # 00700→0700
                            tk = yf.Ticker(f"{yf_sym}.HK")
                            info = tk.info
                            entry = result.get(sym, {})
                            entry.update({
                                "pe": float(info.get("trailingPE", 0) or 0),
                                "roe": float(info.get("returnOnEquity", 0) or 0),
                                "revenue": float(info.get("totalRevenue", 0) or 0),
                                "net_profit": float(info.get("netIncomeToCommon", 0) or 0),
                                "source": "yfinance_fallback",
                            })
                            result[sym] = entry
                            print(f"    ✅ {sym} (yf): PE={entry['pe']:.1f} ROE={entry.get('roe', 0)*100:.1f}%")
                        except Exception:
                            continue
                except ImportError:
                    pass
                    
        except ImportError:
            print("    ⚠️  akshare not installed")
        except Exception as e:
            print(f"    ⚠️  HK fetch error: {e}")
        
        return result

    def _estimate_market_cap(self, symbol: str) -> float:
        """Estimate market cap from latest snapshot."""
        try:
            from portfolio_manager.state_engine import PortfolioStateEngine
            eng = PortfolioStateEngine()
            # Get latest price and shares
            latest = eng.latest_snapshot()
            if latest and "holdings" in latest:
                for h in latest["holdings"]:
                    if h.get("symbol") == symbol:
                        return float(h.get("market_value", 0)) / float(h.get("weight", 0.01)) if float(h.get("weight", 0.01)) > 0 else 0
        except Exception:
            pass
        return 0.0

    def _save_csv(self, data: dict[str, dict]) -> None:
        """Save fundamentals to CSV with point-in-time fields."""
        if not data:
            return
        path = REPORTS / "fundamental_data.csv"
        fieldnames = ["symbol", "pe", "pb", "roe", "revenue", "net_profit",
                       "revenue_growth", "net_profit_growth", "gross_margin",
                       "net_margin", "eps", "bvps", "cf_per_share", "debt_ratio",
                       "current_ratio", "market_cap", "peg", "report_period",
                       "period_end", "available_date", "source", "fetched_at"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for sym, entry in sorted(data.items()):
                row = {"symbol": sym}
                for field in fieldnames:
                    if field == "symbol":
                        continue
                    val = entry.get(field, "")
                    if isinstance(val, float):
                        row[field] = f"{val:.4f}"
                    else:
                        row[field] = str(val) if val else ""
                w.writerow(row)
        print(f"\n  💾 Saved: {path} ({len(data)} stocks)")


# ── CLI ──────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Phase F1: Fundamental Data Engine")
    print("=" * 60)
    print()
    
    # Portfolio holdings (20 stocks)
    holdings = [
        {"symbol": "600415", "market": "CN"},  # 小商品城
        {"symbol": "600597", "market": "CN"},  # 光明乳业
        {"symbol": "601318", "market": "CN"},  # 中国平安
        {"symbol": "688036", "market": "CN"},  # 传音控股
        {"symbol": "688223", "market": "CN"},  # 晶科能源
        {"symbol": "159330", "market": "CN"},  # 沪深300ETF
        {"symbol": "513010", "market": "CN"},  # 恒生科技ETF
        {"symbol": "600690", "market": "CN"},  # 海尔智家
        {"symbol": "000333", "market": "CN"},  # 美的集团
        {"symbol": "300005", "market": "CN"},  # 探路者
        {"symbol": "002241", "market": "CN"},  # 歌尔股份
        {"symbol": "002459", "market": "CN"},  # 晶澳科技
        {"symbol": "00700", "market": "HK"},   # 腾讯
        {"symbol": "09988", "market": "HK"},   # 阿里
        {"symbol": "01024", "market": "HK"},   # 快手
        {"symbol": "00992", "market": "HK"},   # 联想
        {"symbol": "03431", "market": "HK"},   # 南方港韩科技
        {"symbol": "02382", "market": "HK"},   # 舜宇光学
        {"symbol": "03396", "market": "HK"},   # 联想控股
        {"symbol": "03996", "market": "HK"},   # 中国能源
    ]
    
    eng = FundamentalEngine()
    data = eng.fetch_all(holdings)
    
    print(f"\n{'='*60}")
    print(f"  Summary: {len(data)}/{len(holdings)} holdings with fundamental data")
    print(f"{'='*60}")
    
    # Quick quality check
    with_roe = sum(1 for d in data.values() if d.get("roe", 0) > 0)
    with_pe = sum(1 for d in data.values() if d.get("pe", 0) > 0)
    with_revenue = sum(1 for d in data.values() if d.get("revenue", 0) > 0)
    print(f"\n  ROE: {with_roe}/{len(data)}")
    print(f"  PE:  {with_pe}/{len(data)}")
    print(f"  Rev: {with_revenue}/{len(data)}")
