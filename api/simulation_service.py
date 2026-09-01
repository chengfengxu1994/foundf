"""Read-only simulation dashboard projection.

The projection deliberately excludes raw broker records and never emits order
amounts, executable instructions, or mutation capabilities.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

SCHEMA_VERSION = "foundf.simulation_dashboard.v2"


def _security_type(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"1", "STOCK", "A_STOCK", "STOCK_CN", "HK_STOCK", "US_STOCK"}:
        return "STOCK"
    if "ETF" in raw:
        return "ETF"
    if "FUND" in raw:
        return "FUND"
    if "INDEX" in raw:
        return "INDEX"
    return "UNKNOWN"


def _symbol_keys(symbol: Any) -> set[str]:
    raw = str(symbol or "").strip()
    if not raw:
        return set()
    keys = {raw, raw.upper(), raw.lower()}
    lowered = raw.lower()
    if lowered.startswith(("sh.", "sz.", "bj.")):
        keys.add(raw[3:])
    uppered = raw.upper()
    if uppered.endswith((".SH", ".SZ", ".BJ")):
        keys.add(raw[:-3])
    return keys


def _security_master(
    conn: duckdb.DuckDBPyConnection,
    tables: set[str],
    symbols: set[str],
    names: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve bounded display metadata without guessing from numeric prefixes."""

    wanted_keys = {key for symbol in symbols for key in _symbol_keys(symbol)}
    by_symbol: dict[str, dict[str, Any]] = {}
    name_candidates: dict[str, list[dict[str, Any]]] = {}

    def add(
        symbol: Any,
        display_name: Any,
        asset_type: Any,
        exchange: Any,
        source: str,
    ) -> None:
        raw_symbol = str(symbol or "").strip()
        raw_name = str(display_name or "").strip()
        if not raw_symbol or not raw_name:
            return
        if wanted_keys and not (_symbol_keys(raw_symbol) & wanted_keys) and raw_name not in names:
            return
        metadata = {
            "display_name": raw_name,
            "security_type": _security_type(asset_type),
            "exchange": str(exchange or "").strip() or None,
            "name_source": source,
        }
        for key in _symbol_keys(raw_symbol):
            by_symbol.setdefault(key, metadata)
        name_candidates.setdefault(raw_name, []).append({"symbol": raw_symbol, **metadata})

    # Cross-asset stock_basic is authoritative when present (including ETF rows).
    if "stock_basic" in tables:
        for row in conn.execute(
            "SELECT code, name, asset_type, market FROM stock_basic"
        ).fetchall():
            add(*row, "stock_basic")
    # stock_registry is a complete A-share stock fallback, but intentionally excludes ETFs.
    if "stock_registry" in tables:
        for row in conn.execute(
            "SELECT symbol, code_name, security_type, exchange FROM stock_registry"
        ).fetchall():
            add(*row, "stock_registry")
    # User-reviewed portfolio/broker names provide a controlled fund/legacy fallback.
    if "portfolio" in tables:
        for row in conn.execute(
            "SELECT symbol, name, asset_type, market FROM portfolio"
        ).fetchall():
            add(*row, "portfolio")
    if "portfolio_computed_position" in tables:
        for row in conn.execute(
            "SELECT symbol, name, NULL, market FROM portfolio_computed_position"
        ).fetchall():
            add(*row, "portfolio_computed_position")
    if "broker_transaction_normalized" in tables:
        for row in conn.execute(
            "SELECT symbol, MAX(security_name), NULL, MAX(market) "
            "FROM broker_transaction_normalized GROUP BY symbol"
        ).fetchall():
            add(*row, "broker_transaction_normalized")

    # Indices need an explicit identity; never infer them by stripping a prefix.
    explicit = {
        "sh.000300": {
            "display_name": "沪深300",
            "security_type": "INDEX",
            "exchange": "SH",
            "name_source": "foundf_explicit_index",
        }
    }
    for symbol, metadata in explicit.items():
        if _symbol_keys(symbol) & wanted_keys:
            for key in _symbol_keys(symbol):
                by_symbol[key] = metadata
            name_candidates.setdefault(metadata["display_name"], []).append(
                {"symbol": symbol, **metadata}
            )

    by_name: dict[str, dict[str, Any]] = {}
    for name, candidates in name_candidates.items():
        unique_symbols = {item["symbol"] for item in candidates}
        if len(unique_symbols) == 1:
            by_name[name] = candidates[0]
    return by_symbol, by_name


def _metadata_for(
    symbol: Any,
    by_symbol: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    for key in _symbol_keys(symbol):
        if key in by_symbol:
            return dict(by_symbol[key])
    return {
        "display_name": None,
        "security_type": "UNKNOWN",
        "exchange": None,
        "name_source": None,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _latest_review(review_dir: Path | None) -> dict[str, Any] | None:
    if review_dir is None or not review_dir.exists():
        return None
    for path in sorted(review_dir.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        execution = payload.get("execution") or {}
        signal = payload.get("signal") or {}
        quality = payload.get("execution_quality") or {}
        return {
            "date": payload.get("date"),
            "generated_at": payload.get("generated_at"),
            "execution": {
                "planned_orders": execution.get("planned_orders"),
                "submitted": execution.get("submitted"),
                "rejected": execution.get("rejected"),
                "fill_rate": execution.get("fill_rate"),
                "exec_score": execution.get("exec_score"),
            },
            "signal": {
                "data_as_of": signal.get("prev_data_as_of"),
                "hit_rate": signal.get("hit_rate"),
                "avg_return": signal.get("avg_return"),
                "signal_score": signal.get("signal_score"),
            },
            "execution_quality": {
                "status": quality.get("status", "UNAVAILABLE"),
                "fills_count": quality.get("fills_count"),
                "avg_slippage_decision_bps": quality.get(
                    "avg_slippage_decision_bps"
                ),
                "vwap_win_rate": quality.get("vwap_win_rate"),
                "avg_latency_sec": quality.get("avg_latency_sec"),
                "orphan_ratio": quality.get("orphan_ratio"),
                "fee_status": "MISSING_FEE",
            },
            "holdings": [
                {
                    "name": item.get("name"),
                    "quantity": item.get("qty"),
                    "market_value": item.get("market_value"),
                    "pnl": item.get("pnl"),
                    "pnl_pct": item.get("pnl_pct"),
                    "last": item.get("last"),
                    "partial": bool(item.get("partial")),
                }
                for item in (payload.get("holdings") or [])
            ],
        }
    return None


def _empty(status: str, blocker: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "account_mode": "SIMULATION",
        "data_as_of": None,
        "blockers": [blocker],
        "summary": None,
        "nav_history": [],
        "positions": [],
        "recent_fills": [],
        "candidate_batch": None,
        "daily_review": None,
        "automatic_trade_allowed": False,
        "mutation_allowed": False,
        "disclaimer": "仅为模拟观察数据，不代表真实券商资产或交易授权。",
    }


def build_simulation_dashboard(
    db_path: str | Path,
    *,
    review_dir: str | Path | None = None,
    fill_limit: int = 20,
    nav_limit: int = 30,
) -> dict[str, Any]:
    """Build a bounded, credential-free simulation projection."""

    path = Path(db_path)
    if not path.exists():
        return _empty("BLOCKED_DATA", "SIMULATION_DATABASE_MISSING")
    try:
        conn = duckdb.connect(str(path), read_only=True)
    except Exception:
        return _empty("UNAVAILABLE", "SIMULATION_DATABASE_UNAVAILABLE")

    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        required = {
            "broker_sim_normalized_fill",
            "sim_nav_daily",
            "strategy_candidate",
        }
        missing = sorted(required - tables)
        if missing:
            return _empty(
                "BLOCKED_DATA", "SIMULATION_TABLES_MISSING:" + ",".join(missing)
            )

        nav_rows = conn.execute(
            "SELECT date, total_assets, cash, market_value, holdings_json, "
            "source, captured_at "
            "FROM sim_nav_daily ORDER BY date DESC LIMIT ?",
            [max(1, min(nav_limit, 365))],
        ).fetchall()
        nav_rows.reverse()
        nav_history = [
            {
                "date": _iso(row[0]),
                "total_assets": row[1],
                "cash": row[2],
                "market_value": row[3],
                "source": row[5],
                "captured_at": _iso(row[6]),
            }
            for row in nav_rows
        ]

        fill_rows = conn.execute(
            "SELECT fill_id, symbol, side, fill_time, fill_price, quantity, "
            "fee_amount, candidate_id, source, imported_at "
            "FROM broker_sim_normalized_fill "
            "ORDER BY fill_time DESC LIMIT ?",
            [max(1, min(fill_limit, 100))],
        ).fetchall()
        recent_fills = [
            {
                "fill_id": row[0],
                "symbol": row[1],
                "side": row[2],
                "fill_time": _iso(row[3]),
                "fill_price": row[4],
                "quantity": row[5],
                "notional": round(float(row[4]) * float(row[5]), 2),
                "fee_amount": row[6],
                "fee_status": "AVAILABLE" if row[6] is not None else "MISSING_FEE",
                "candidate_id": row[7],
                "link_status": "LINKED" if row[7] else "ORPHAN",
                "source": row[8],
                "imported_at": _iso(row[9]),
            }
            for row in fill_rows
        ]

        latest_candidate_date = conn.execute(
            "SELECT MAX(data_as_of) FROM strategy_candidate"
        ).fetchone()[0]
        candidate_batch = None
        if latest_candidate_date is not None:
            # 同一 data_as_of 可能重跑生成多批候选（同 symbol 重复）；
            # 每个 symbol 只保留 generated_at 最新的一行
            rows = conn.execute(
                "SELECT candidate_id, symbol, side, conviction, decision_price, "
                "strategy_version, status, generated_at, source "
                "FROM strategy_candidate WHERE data_as_of = ? "
                "QUALIFY row_number() OVER ("
                "  PARTITION BY symbol, side ORDER BY generated_at DESC) = 1 "
                "ORDER BY conviction DESC, symbol",
                [latest_candidate_date],
            ).fetchall()
            candidate_batch = {
                "data_as_of": _iso(latest_candidate_date),
                "strategy_version": rows[0][5] if rows else None,
                "generated_at": _iso(rows[0][7]) if rows else None,
                "items": [
                    {
                        "candidate_id": row[0],
                        "symbol": row[1],
                        "side": row[2],
                        "conviction": row[3],
                        "decision_price": row[4],
                        "status": row[6],
                        "source": row[8],
                    }
                    for row in rows
                ],
            }

        daily_review = _latest_review(
            Path(review_dir) if review_dir is not None else None
        )
        raw_positions = list((daily_review or {}).get("holdings") or [])
        if not raw_positions and nav_rows:
            try:
                raw_positions = json.loads(nav_rows[-1][4] or "[]")
            except (TypeError, json.JSONDecodeError):
                raw_positions = []

        symbols = {str(item["symbol"]) for item in recent_fills}
        symbols.update(
            str(item["symbol"])
            for item in ((candidate_batch or {}).get("items") or [])
        )
        names = {
            str(item.get("name") or "").strip()
            for item in raw_positions
            if str(item.get("name") or "").strip()
        }
        by_symbol, by_name = _security_master(conn, tables, symbols, names)
        for item in recent_fills:
            item.update(_metadata_for(item["symbol"], by_symbol))
        for item in ((candidate_batch or {}).get("items") or []):
            item.update(_metadata_for(item["symbol"], by_symbol))

        positions = []
        for raw in raw_positions:
            raw_name = str(raw.get("name") or "").strip()
            resolved = by_name.get(raw_name)
            symbol = resolved.get("symbol") if resolved else None
            positions.append({
                "symbol": symbol,
                "display_name": raw_name or (
                    resolved.get("display_name") if resolved else None
                ),
                "security_type": (
                    resolved.get("security_type") if resolved else "UNKNOWN"
                ),
                "exchange": resolved.get("exchange") if resolved else None,
                "name_source": resolved.get("name_source") if resolved else None,
                "quantity": raw.get("quantity", raw.get("qty")),
                "available": raw.get("available"),
                "market_value": raw.get("market_value"),
                "pnl": raw.get("pnl"),
                "pnl_pct": raw.get("pnl_pct"),
                "cost": raw.get("cost"),
                "last": raw.get("last"),
                "partial": bool(raw.get("partial")),
                "identity_status": "RESOLVED" if symbol else "UNRESOLVED",
            })

        latest_nav = nav_history[-1] if nav_history else None
        previous_nav = nav_history[-2] if len(nav_history) >= 2 else None
        nav_change = None
        if (
            latest_nav
            and previous_nav
            and latest_nav["total_assets"] is not None
            and previous_nav["total_assets"]
        ):
            nav_change = round((
                float(latest_nav["total_assets"])
                / float(previous_nav["total_assets"])
                - 1
            ), 8)
        assets = [
            float(row["total_assets"])
            for row in nav_history
            if row["total_assets"] is not None
        ]
        drawdown = None
        total_return = None
        if assets:
            peak = assets[0]
            drawdowns = []
            for value in assets:
                peak = max(peak, value)
                drawdowns.append(value / peak - 1 if peak else 0)
            drawdown = min(drawdowns)
            if assets[0]:
                total_return = assets[-1] / assets[0] - 1

        fill_count = conn.execute(
            "SELECT COUNT(*) FROM broker_sim_normalized_fill"
        ).fetchone()[0]
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate"
        ).fetchone()[0]
        data_dates = [
            item for item in (
                latest_nav["date"] if latest_nav else None,
                _iso(latest_candidate_date),
                recent_fills[0]["fill_time"][:10] if recent_fills else None,
            ) if item
        ]
        status = "READY" if latest_nav and candidate_batch else "DATA_BUILDING"
        blockers = [] if status == "READY" else ["SIMULATION_HISTORY_INCOMPLETE"]
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "account_mode": "SIMULATION",
            "data_as_of": max(data_dates) if data_dates else None,
            "blockers": blockers,
            "summary": {
                "total_assets": latest_nav["total_assets"] if latest_nav else None,
                "cash": latest_nav["cash"] if latest_nav else None,
                "market_value": latest_nav["market_value"] if latest_nav else None,
                "cash_weight": (
                    float(latest_nav["cash"]) / float(latest_nav["total_assets"])
                    if latest_nav and latest_nav["cash"] is not None
                    and latest_nav["total_assets"] else None
                ),
                "nav_change": nav_change,
                "max_drawdown": drawdown,
                "total_return": total_return,
                "nav_observations": len(nav_history),
                "fills_total": fill_count,
                "candidates_total": candidate_count,
                "positions_count": len(positions),
            },
            "nav_history": nav_history,
            "positions": positions,
            "recent_fills": recent_fills,
            "candidate_batch": candidate_batch,
            "daily_review": daily_review,
            "automatic_trade_allowed": False,
            "mutation_allowed": False,
            "disclaimer": "仅为模拟观察数据，不代表真实券商资产或交易授权。",
        }
    except Exception:
        return _empty("UNAVAILABLE", "SIMULATION_PROJECTION_FAILED")
    finally:
        conn.close()
