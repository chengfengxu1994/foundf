"""
returns.py — Return calculation engine (Phase I2).

Computes:
- TWR (Time-Weighted Return, Modified Dietz): investment skill evaluation
- XIRR (Extended IRR): real money-making ability
- Simple Return: total gain / total deposits

Usage:
    calc = ReturnsCalculator()
    result = calc.compute_all()
    print(result["twr"]["annualized_twr_pct"])
    print(result["xirr"]["annualized_xirr_pct"])
"""

from __future__ import annotations

import csv
import calendar as cal
from collections import defaultdict
from datetime import date as dt_date, datetime
from pathlib import Path
from typing import Any

REPORTS_DIR = Path("reports/reconciliation")

# Try scipy for XIRR
try:
    import scipy.optimize as opt
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


EXTERNAL_CF_TYPES = frozenset({
    "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT",
    "DIVIDEND", "INTEREST",
})


class ReturnsCalculator:
    """Portfolio return calculator.
    
    Reads economic events + snapshot series.
    Computes TWR (Modified Dietz), XIRR, and simple returns.
    Only EXTERNAL cash flows (transfers, dividends, interest) affect returns.
    Repo, IPO, BUY/SELL, FEE are internal and excluded.
    """
    
    def __init__(self, events_path: Path = None, 
                 snapshot_path: Path = None):
        self.events_path = events_path or REPORTS_DIR / "broker_economic_event_v4.csv"
        self.snapshot_path = snapshot_path or REPORTS_DIR / "portfolio_snapshot_series.csv"
        
        self._events: list[dict[str, Any]] = []
        self._snapshots: list[dict[str, Any]] = []
        self._external_cf: list[tuple[str, float]] = []
        self._opening_value: float = 0.0
        self._closing_value: float = 0.0
        self._start_date: str = ""
        self._end_date: str = ""
        
        self._load()
    
    def _load(self) -> None:
        """Load events and snapshots."""
        with open(self.events_path, encoding="utf-8-sig") as f:
            self._events = list(csv.DictReader(f))
        
        with open(self.snapshot_path, encoding="utf-8-sig") as f:
            self._snapshots = list(csv.DictReader(f))
        
        if self._snapshots:
            self._opening_value = float(self._snapshots[0]["total_asset"])
            self._closing_value = float(self._snapshots[-1]["total_asset"])
            self._start_date = self._snapshots[0]["date"]
            self._end_date = self._snapshots[-1]["date"]
        
        # External cash flows only
        for e in self._events:
            et = e["event_type"]
            if et not in EXTERNAL_CF_TYPES:
                continue
            amt = float(e["cash_impact"])
            d = e["date"]
            if len(d) == 8:
                d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            
            # XIRR: transfer_in (money in) = negative; transfer_out = positive
            if et == "CASH_TRANSFER_IN":
                self._external_cf.append((d, -abs(amt)))
            elif et == "CASH_TRANSFER_OUT":
                self._external_cf.append((d, abs(amt)))
            elif et in ("DIVIDEND", "INTEREST"):
                self._external_cf.append((d, abs(amt)))
        
        self._external_cf.sort(key=lambda x: x[0])

    # ── Simple Return ─────────────────────────────────

    def simple_return(self) -> dict[str, Any]:
        """Return on invested capital."""
        net_cf = sum(a for _, a in self._external_cf)
        invested = -sum(a for _, a in self._external_cf if a < 0)
        total_gain = self._closing_value - self._opening_value + net_cf
        
        if invested > 0:
            ret = total_gain / invested
        elif self._opening_value > 0:
            ret = (self._closing_value - self._opening_value) / self._opening_value
        else:
            ret = 0.0
        
        return {
            "opening_value": round(self._opening_value, 2),
            "closing_value": round(self._closing_value, 2),
            "net_cash_flow": round(net_cf, 2),
            "total_invested": round(invested, 2),
            "total_gain": round(total_gain, 2),
            "simple_return_pct": f"{ret * 100:.2f}%",
            "simple_return": round(ret, 6),
        }

    # ── TWR (Modified Dietz, monthly) ────────────────

    def twr(self) -> dict[str, Any]:
        """Time-Weighted Return via Modified Dietz (monthly)."""
        snap_by_date = {}
        for s in self._snapshots:
            snap_by_date[s["date"]] = float(s["total_asset"])
        
        if not snap_by_date:
            return {"twr_pct": "0.00%", "sub_periods": []}
        
        start_dt = datetime.strptime(self._start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(self._end_date, "%Y-%m-%d").date()
        
        # Monthly sub-periods
        periods = []
        cur = start_dt
        while cur < end_dt:
            last_day = cal.monthrange(cur.year, cur.month)[1]
            me = cur.replace(day=min(last_day, (end_dt - cur).days + cur.day))
            if me > end_dt:
                me = end_dt
            if me > cur:
                periods.append((f"{cur.year}-{cur.month:02d}", cur.isoformat(), me.isoformat()))
            cur = cur.replace(day=28) + __import__("datetime").timedelta(days=4)
            cur = cur.replace(day=1)
        
        sub_periods = []
        for label, p_start, p_end in periods:
            p_sd = datetime.strptime(p_start, "%Y-%m-%d").date()
            p_ed = datetime.strptime(p_end, "%Y-%m-%d").date()
            days_in_p = (p_ed - p_sd).days + 1
            
            bmv = snap_by_date.get(p_start, 0.0)
            emv = snap_by_date.get(p_end, bmv)
            if bmv <= 0:
                continue
            
            period_cfs = [(d, a) for d, a in self._external_cf if p_start <= d <= p_end]
            total_cf = sum(a for _, a in period_cfs)
            
            weighted_cf = 0.0
            for cf_date, cf_amt in period_cfs:
                cfd = datetime.strptime(cf_date, "%Y-%m-%d").date()
                d_fs = (cfd - p_sd).days
                w = (days_in_p - d_fs) / days_in_p if days_in_p > 0 else 0
                weighted_cf += cf_amt * w
            
            denom = bmv + weighted_cf
            r = (emv - bmv - total_cf) / denom if denom != 0 else 0.0
            
            sub_periods.append({
                "label": label, "start": p_start, "end": p_end,
                "bmv": round(bmv, 2), "emv": round(emv, 2),
                "total_cf": round(total_cf, 2),
                "return_pct": f"{r * 100:.4f}%",
                "r": round(r, 6),
            })
        
        twr = 1.0
        for sp in sub_periods:
            twr *= (1 + sp["r"])
        twr -= 1.0
        
        days = (end_dt - start_dt).days
        years = days / 365.25
        ann = (1 + twr) ** (1 / years) - 1 if years > 0 else 0.0
        
        return {
            "twr_pct": f"{twr * 100:.2f}%",
            "annualized_twr_pct": f"{ann * 100:.2f}%",
            "annualized_twr": round(ann, 6),
            "period_days": days,
            "period_years": round(years, 2),
            "sub_periods": sub_periods,
        }

    # ── XIRR ─────────────────────────────────────────

    def _xirr_npv(self, rate: float, flows: list[tuple[float, float]]) -> float:
        total = 0.0
        for days, amount in flows:
            total += amount / ((1 + rate) ** (days / 365.0))
        return total

    def xirr(self, guess: float = 0.1) -> dict[str, Any]:
        """Extended IRR via scipy newton."""
        if not HAS_SCIPY:
            return {"xirr_pct": "N/A (scipy required)"}
        
        start_dt = datetime.strptime(self._start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(self._end_date, "%Y-%m-%d")
        final_days = float((end_dt - start_dt).days)
        
        # Flows: initial (neg), external cash flows, final value (pos)
        flows: list[tuple[float, float]] = [(0.0, -self._opening_value)]
        for d, amt in self._external_cf:
            cfd = datetime.strptime(d, "%Y-%m-%d")
            flows.append((float((cfd - start_dt).days), amt))
        flows.append((final_days, self._closing_value))
        flows.sort(key=lambda x: x[0])
        
        # Newton
        for g in [guess, 0.05, 0.2, -0.05, 0.5]:
            try:
                r = float(opt.newton(lambda x: self._xirr_npv(x, flows), g, maxiter=1000))
                years = final_days / 365.25
                ann = r
                return {
                    "xirr_pct": f"{r * 100:.2f}%",
                    "annualized_xirr_pct": f"{ann * 100:.2f}%",
                    "annualized_xirr": round(ann, 6),
                    "period_days": int(final_days),
                    "period_years": round(years, 2),
                    "cash_flow_count": len(self._external_cf),
                }
            except (RuntimeError, ValueError):
                continue
        return {"xirr_pct": "N/A (no convergence)"}

    # ── Annual ───────────────────────────────────────

    def annual_returns(self) -> dict[str, Any]:
        """Per-calendar-year simple return."""
        snap_by_date = {s["date"]: float(s["total_asset"]) for s in self._snapshots}
        cf_by_year: dict[str, list] = defaultdict(list)
        for d, a in self._external_cf:
            cf_by_year[d[:4]].append((d, a))
        
        years = sorted(set(s["date"][:4] for s in self._snapshots))
        annual = {}
        for year in years:
            ys = [s for s in self._snapshots if s["date"].startswith(year)]
            if not ys:
                continue
            yo = float(ys[0]["total_asset"])
            yc = float(ys[-1]["total_asset"])
            ycfs = cf_by_year.get(year, [])
            net = sum(a for _, a in ycfs)
            invested = -sum(a for _, a in ycfs if a < 0)
            gain = yc - yo + net
            
            if invested > 0:
                ret = gain / invested
            elif yo > 0:
                ret = (yc - yo) / yo
            else:
                ret = 0.0
            
            annual[year] = {
                "period": f"{ys[0]['date']} → {ys[-1]['date']}",
                "opening": round(yo, 2),
                "closing": round(yc, 2),
                "net_cash_flow": round(net, 2),
                "invested": round(invested, 2),
                "gain": round(gain, 2),
                "return_pct": f"{ret * 100:.2f}%",
            }
        return annual

    # ── All ──────────────────────────────────────────

    def compute_all(self) -> dict[str, Any]:
        return {
            "period": f"{self._start_date} → {self._end_date}",
            "simple_return": self.simple_return(),
            "twr": self.twr(),
            "xirr": self.xirr(),
            "annual_returns": self.annual_returns(),
        }


if __name__ == "__main__":
    calc = ReturnsCalculator()
    r = calc.compute_all()
    
    print("=== Phase I2: Return Calculation ===\n")
    print(f"Period: {r['period']}\n")
    
    sr = r["simple_return"]
    print(f"Simple (Book) Return:")
    print(f"  Opening: {sr['opening_value']:>12,.2f}")
    print(f"  Closing: {sr['closing_value']:>12,.2f}")
    print(f"  Invested: {sr['total_invested']:>12,.2f}")
    print(f"  Gain:     {sr['total_gain']:>+12,.2f}")
    print(f"  Return:   {sr['simple_return_pct']:>12}")
    print()
    
    tw = r["twr"]
    print(f"TWR (Modified Dietz, monthly):")
    print(f"  Note: Not meaningful — cash flows dominate portfolio value changes")
    print(f"  Monthly periods: {len(tw['sub_periods'])} (denominator BMV+weighted_CF near zero)")
    print()
    
    print("XIRR — Primary Metric (handles irregular cash flows):")
    xr = r["xirr"]
    print(f"  Rate: {xr.get('xirr_pct', 'N/A')}")
    print(f"  Annualized: {xr.get('annualized_xirr_pct', 'N/A')}")
    print(f"  Cash flows: {xr.get('cash_flow_count', 0)} (external: transfers + dividends)")
    print()
    
    print("Simple Book Return (complementary):")
    for yr, d in sorted(r["annual_returns"].items()):
        print(f"  {yr}: {d['period']}")
        print(f"       Invested={d['invested']:>8,.0f}  Gain={d['gain']:>+8,.0f}  Return={d['return_pct']}")
