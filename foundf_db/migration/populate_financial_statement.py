"""Migration: populate financial_statement table from fundamental_data.csv."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPORTS = Path("reports/reconciliation")
DATA = Path("data")
DUCK_PATH = DATA / "finance.duckdb"


def _build_rows(reader) -> tuple[list[tuple], int]:
    """把 CSV 行转换为 financial_statement 元组行。

    返回 (rows, skipped_missing_period)：period_end 缺失的行跳过并计数，
    不再用当天日期兜底（避免污染时间序列）。
    """
    rows = []
    skipped_missing_period = 0
    for r in reader:
        period = r.get("period_end", "")
        avail = r.get("available_date", "")
        # period_end 缺失（如 yfinance 港股）时跳过该行，不得用 date.today() 兜底
        if not period or period == "0":
            skipped_missing_period += 1
            continue
        if not avail or avail == "0":
            avail = period
        try:
            rows.append((
                r["symbol"],                         # symbol
                period[:10],                         # report_date
                "Q",                                 # report_type
                avail[:10] if avail else None,       # filed_at
                _f(r.get("revenue")),                # revenue
                _f(r.get("net_profit")),             # net_profit
                # 源字段 cf_per_share 是每股经营现金流，与 operating_cf
                # （经营活动现金流总额）口径不同，源数据无总额字段，留 NULL
                None,                                # operating_cf
                0.0,                                 # free_cash_flow
                0.0,                                 # total_assets
                0.0,                                 # total_liabilities
                0.0,                                 # equity
                _f(r.get("roe")),                    # roe
                0.0,                                 # roa
                _f(r.get("debt_ratio")),             # debt_ratio
                _f(r.get("gross_margin")),           # gross_margin
                _f(r.get("net_margin")),             # net_margin
                _f(r.get("eps")),                    # eps
                _f(r.get("bvps")),                   # bvps
                _f(r.get("revenue_growth")),         # revenue_growth
                _f(r.get("net_profit_growth")),      # profit_growth
                0.0,                                 # r_and_d_expense
                r.get("source", "akshare"),          # source
                _f(r.get("pe")),                     # pe
                _f(r.get("pb")),                     # pb
            ))
        except (ValueError, KeyError) as e:
            print(f"  ⚠️  Skipping {r.get('symbol', '?')}: {e}")
    return rows, skipped_missing_period


def run(reports_dir: Path = REPORTS, duck_path: Path = DUCK_PATH):
    """Populate financial_statement table in DuckDB."""
    fund_csv = reports_dir / "fundamental_data.csv"
    if not fund_csv.exists():
        print("❌ fundamental_data.csv not found. Run fundamental_engine first.")
        return False
    
    try:
        import duckdb
    except ImportError:
        print("❌ duckdb not installed.")
        return False
    
    con = duckdb.connect(str(duck_path))
    
    # Create table if not exists (standalone 兼容；正式建表口径以 foundf_db/schema.py 为准)
    con.execute("""
        CREATE TABLE IF NOT EXISTS financial_statement (
            symbol VARCHAR,
            report_date DATE,
            report_type VARCHAR,
            filed_at DATE,
            revenue DOUBLE,
            net_profit DOUBLE,
            operating_cf DOUBLE,
            free_cash_flow DOUBLE,
            total_assets DOUBLE,
            total_liabilities DOUBLE,
            equity DOUBLE,
            roe DOUBLE,
            roa DOUBLE,
            debt_ratio DOUBLE,
            gross_margin DOUBLE,
            net_margin DOUBLE,
            eps DOUBLE,
            bvps DOUBLE,
            revenue_growth DOUBLE,
            profit_growth DOUBLE,
            r_and_d_expense DOUBLE,
            source VARCHAR,
            pe DOUBLE,
            pb DOUBLE,
            UNIQUE (symbol, report_date, report_type)
        )
    """)
    
    # Check if pe/pb columns exist (migration may have run before)
    existing_cols = [c[0] for c in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='financial_statement' AND table_schema='main'"
    ).fetchall()]
    
    if "pe" not in existing_cols:
        con.execute("ALTER TABLE financial_statement ADD COLUMN pe DOUBLE")
    if "pb" not in existing_cols:
        con.execute("ALTER TABLE financial_statement ADD COLUMN pb DOUBLE")
    
    # Load CSV data
    with open(fund_csv, encoding="utf-8") as f:
        rows, skipped_missing_period = _build_rows(csv.DictReader(f))
    if skipped_missing_period:
        print(f"  ⚠️  跳过 period_end 缺失的行: {skipped_missing_period} 条")

    if rows:
        # 幂等 upsert：按 UNIQUE(symbol, report_date, report_type) 冲突替换，
        # 不再全表 DELETE 重写（保留增量与审计轨迹，口径同 Warehouse.insert
        # 的 conflict_strategy='replace'）
        con.executemany("""
            INSERT OR REPLACE INTO financial_statement VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
        """, rows)
        count = con.execute("SELECT COUNT(*) FROM financial_statement").fetchone()[0]
        print(f"  Populated financial_statement: upsert {len(rows)} rows, total {count} rows")
    else:
        print("⚠️  No rows to insert")
    
    con.close()
    return True


def _f(v) -> float:
    """Parse float, return 0.0 on failure."""
    if v is None or v == "" or v == "0":
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    run()
