"""全球行情观察列表、快照回退和显式写入审计。

此模块只服务看盘与长期数据积累。它不写 ``portfolio``，也不向 NAV/再平衡模块
提供“已验证估值”状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from data_provider.providers.yfinance_provider import YFinanceProvider

from .warehouse import Warehouse


SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.:^=_-]{0,39}$")
PROVIDER_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.^=_-]{0,31}$")
ALLOWED_MARKETS = {"A", "HK", "US", "KR", "VN", "JP", "EU", "GLOBAL"}
ALLOWED_CURRENCIES = {"CNY", "HKD", "USD", "KRW", "VND", "JPY", "EUR", "GBP"}
ALLOWED_ASSET_TYPES = {"STOCK", "ETF", "INDEX", "FUTURE", "FX", "CRYPTO"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MarketWatchStore:
    """DuckDB 行情观察库；默认配置与用户自定义项合并读取。"""

    def __init__(
        self,
        *,
        data_root: str | Path = "data",
        config_path: str | Path = "config/market_watchlist.json",
        quote_fetcher: Callable[[list[str]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.db_path = Path(os.getenv("DUCKDB_PATH", self.data_root / "finance.duckdb"))
        self.config_path = Path(config_path)
        self.quote_fetcher = quote_fetcher or YFinanceProvider().get_recent_quotes
        self._lock = threading.RLock()

    def init(self) -> None:
        with self._warehouse() as warehouse:
            warehouse.init()

    def _warehouse(self) -> Warehouse:
        return Warehouse(self.db_path)

    def _config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"lists": [], "storage": {}}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _configured_lists(self) -> dict[str, dict[str, Any]]:
        return {
            str(item["list_id"]): item
            for item in self._config().get("lists", [])
            if item.get("list_id")
        }

    def list_watchlists(self) -> dict[str, Any]:
        configured = self._configured_lists()
        merged: dict[str, dict[str, Any]] = {}
        for list_id, spec in configured.items():
            merged[list_id] = {
                "list_id": list_id,
                "name": spec.get("name", list_id),
                "description": spec.get("description", ""),
                "enabled": True,
                "items": [dict(item, enabled=True, origin="CONFIG") for item in spec.get("items", [])],
            }
        with self._warehouse() as warehouse:
            if not warehouse.table_exists("market_watchlist"):
                rows, item_rows = [], []
            else:
                rows = warehouse.query(
                    "SELECT list_id, name, description, enabled FROM market_watchlist"
                )
                item_rows = warehouse.query(
                    "SELECT list_id, symbol, provider_symbol, name, market, exchange, "
                    "currency, asset_type, sort_order, enabled "
                    "FROM market_watchlist_item ORDER BY list_id, sort_order, symbol"
                )
        for row in rows:
            merged.setdefault(
                row["list_id"],
                {
                    "list_id": row["list_id"],
                    "name": row["name"],
                    "description": row["description"] or "",
                    "enabled": bool(row["enabled"]),
                    "items": [],
                },
            )
            merged[row["list_id"]].update(
                name=row["name"],
                description=row["description"] or "",
                enabled=bool(row["enabled"]),
            )
        for item in item_rows:
            target = merged.setdefault(
                item["list_id"],
                {
                    "list_id": item["list_id"],
                    "name": item["list_id"],
                    "description": "",
                    "enabled": True,
                    "items": [],
                },
            )
            target["items"] = [
                existing
                for existing in target["items"]
                if existing["symbol"] != item["symbol"]
            ]
            target["items"].append(dict(item, origin="USER"))
        lists = list(merged.values())
        lists.sort(key=lambda item: item["list_id"])
        for item in lists:
            item["items"].sort(
                key=lambda row: (int(row.get("sort_order", 0)), row["market"], row["symbol"])
            )
        cfg = self._config()
        return {
            "schema_version": cfg.get("schema_version", "foundf.market_watchlist.v1"),
            "default_list_id": cfg.get("default_list_id"),
            "storage": cfg.get("storage", {}),
            "disclaimer": cfg.get("disclaimer", ""),
            "lists": lists,
        }

    def items(self, list_id: str | None = None) -> list[dict[str, Any]]:
        state = self.list_watchlists()
        target = list_id or state.get("default_list_id")
        lists = state["lists"]
        if target:
            lists = [item for item in lists if item["list_id"] == target]
        result = []
        for watchlist in lists:
            if not watchlist["enabled"]:
                continue
            for item in watchlist["items"]:
                if item.get("enabled", True):
                    result.append(dict(item, list_id=watchlist["list_id"]))
        return result

    @staticmethod
    def _validate_item(payload: dict[str, Any]) -> dict[str, Any]:
        result = {
            "symbol": str(payload.get("symbol", "")).strip().upper(),
            "provider_symbol": str(payload.get("provider_symbol", "")).strip().upper(),
            "name": str(payload.get("name", "")).strip(),
            "market": str(payload.get("market", "")).strip().upper(),
            "exchange": str(payload.get("exchange", "")).strip().upper() or None,
            "currency": str(payload.get("currency", "")).strip().upper(),
            "asset_type": str(payload.get("asset_type", "STOCK")).strip().upper(),
            "sort_order": int(payload.get("sort_order", 1000)),
        }
        if not SYMBOL_RE.fullmatch(result["symbol"]):
            raise ValueError("symbol 格式无效")
        if not PROVIDER_SYMBOL_RE.fullmatch(result["provider_symbol"]):
            raise ValueError("provider_symbol 格式无效")
        if not result["name"] or len(result["name"]) > 120:
            raise ValueError("name 不能为空且不得超过 120 字符")
        if result["market"] not in ALLOWED_MARKETS:
            raise ValueError(f"market 不支持: {result['market']}")
        if result["currency"] not in ALLOWED_CURRENCIES:
            raise ValueError(f"currency 不支持: {result['currency']}")
        if result["asset_type"] not in ALLOWED_ASSET_TYPES:
            raise ValueError(f"asset_type 不支持: {result['asset_type']}")
        return result

    def add_item(
        self,
        *,
        list_id: str,
        payload: dict[str, Any],
        confirmation_reference: str,
    ) -> dict[str, Any]:
        if not confirmation_reference.strip():
            raise ValueError("必须提供 confirmation_reference")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", list_id):
            raise ValueError("list_id 格式无效")
        item = self._validate_item(payload)
        updated_at = _utc_now()
        with self._lock, self._warehouse() as warehouse:
            warehouse.init()
            configured = self._configured_lists().get(list_id, {})
            warehouse.execute(
                "INSERT INTO market_watchlist (list_id, name, description, enabled) "
                "VALUES (?, ?, ?, TRUE) ON CONFLICT (list_id) DO NOTHING",
                [
                    list_id,
                    configured.get("name", list_id),
                    configured.get("description", "用户自定义观察列表"),
                ],
            )
            warehouse.execute(
                "INSERT INTO market_watchlist_item "
                "(list_id, symbol, provider_symbol, name, market, exchange, currency, "
                "asset_type, sort_order, enabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, CURRENT_TIMESTAMP) "
                "ON CONFLICT (list_id, symbol) DO UPDATE SET "
                "provider_symbol=EXCLUDED.provider_symbol, name=EXCLUDED.name, "
                "market=EXCLUDED.market, exchange=EXCLUDED.exchange, "
                "currency=EXCLUDED.currency, asset_type=EXCLUDED.asset_type, "
                "sort_order=EXCLUDED.sort_order, enabled=TRUE, "
                "updated_at=?",
                [
                    list_id,
                    item["symbol"],
                    item["provider_symbol"],
                    item["name"],
                    item["market"],
                    item["exchange"],
                    item["currency"],
                    item["asset_type"],
                    item["sort_order"],
                    updated_at,
                ],
            )
            audit_payload = {"list_id": list_id, **item}
            audit_id = uuid.uuid4().hex
            warehouse.execute(
                "INSERT INTO market_watch_write_audit "
                "(audit_id, action, list_id, symbol, confirmation_reference, payload_hash) "
                "VALUES (?, 'UPSERT_ITEM', ?, ?, ?, ?)",
                [
                    audit_id,
                    list_id,
                    item["symbol"],
                    confirmation_reference.strip(),
                    _json_hash(audit_payload),
                ],
            )
        return {"status": "CONFIRMED", "audit_id": audit_id, "item": item}

    def disable_item(
        self,
        *,
        list_id: str,
        symbol: str,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        if not confirmation_reference.strip():
            raise ValueError("必须提供 confirmation_reference")
        symbol = symbol.strip().upper()
        with self._lock, self._warehouse() as warehouse:
            warehouse.init()
            configured = next(
                (
                    item
                    for item in self._configured_lists().get(list_id, {}).get("items", [])
                    if str(item.get("symbol", "")).upper() == symbol
                ),
                None,
            )
            if configured:
                item = self._validate_item(configured)
                configured_list = self._configured_lists().get(list_id, {})
                warehouse.execute(
                    "INSERT INTO market_watchlist (list_id, name, description, enabled) "
                    "VALUES (?, ?, ?, TRUE) ON CONFLICT (list_id) DO NOTHING",
                    [
                        list_id,
                        configured_list.get("name", list_id),
                        configured_list.get("description", ""),
                    ],
                )
                warehouse.execute(
                    "INSERT INTO market_watchlist_item "
                    "(list_id, symbol, provider_symbol, name, market, exchange, currency, "
                    "asset_type, sort_order, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE) "
                    "ON CONFLICT (list_id, symbol) DO NOTHING",
                    [
                        list_id,
                        item["symbol"],
                        item["provider_symbol"],
                        item["name"],
                        item["market"],
                        item["exchange"],
                        item["currency"],
                        item["asset_type"],
                        item["sort_order"],
                    ],
                )
            warehouse.execute(
                "UPDATE market_watchlist_item SET enabled=FALSE, "
                "updated_at=? WHERE list_id=? AND symbol=?",
                [_utc_now(), list_id, symbol],
            )
            audit_id = uuid.uuid4().hex
            warehouse.execute(
                "INSERT INTO market_watch_write_audit "
                "(audit_id, action, list_id, symbol, confirmation_reference, payload_hash) "
                "VALUES (?, 'DISABLE_ITEM', ?, ?, ?, ?)",
                [
                    audit_id,
                    list_id,
                    symbol,
                    confirmation_reference.strip(),
                    _json_hash({"list_id": list_id, "symbol": symbol}),
                ],
            )
        return {"status": "CONFIRMED", "audit_id": audit_id}

    def _storage_within_budget(self) -> tuple[bool, int, int]:
        storage = self._config().get("storage", {})
        budget_bytes = int(float(storage.get("budget_gb", 240)) * 1024**3)
        used_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return used_bytes < budget_bytes, used_bytes, budget_bytes

    def collect_quotes(self, list_id: str | None = None) -> dict[str, Any]:
        started_at = _utc_now()
        run_id = uuid.uuid4().hex
        items = self.items(list_id)
        symbols = sorted({item["provider_symbol"] for item in items})
        within_budget, used_bytes, budget_bytes = self._storage_within_budget()
        if not within_budget:
            result = {
                "status": "STORAGE_BUDGET_BLOCKED",
                "stored": 0,
                "used_bytes": used_bytes,
                "budget_bytes": budget_bytes,
            }
            self._record_collection_run(
                run_id=run_id,
                started_at=started_at,
                requested=len(symbols),
                received=0,
                stored=0,
                status=result["status"],
                error_code="STORAGE_BUDGET_EXCEEDED",
                error_message="数据库文件已达到配置预算",
            )
            return result
        item_by_provider = {item["provider_symbol"]: item for item in items}
        try:
            quotes = self.quote_fetcher(symbols)
            provider_error = None
        except Exception as exc:
            quotes = []
            provider_error = str(exc)
        if not quotes and provider_error is None:
            provider_error = "Yahoo 返回空响应；可能为限流、网络限制或市场代码暂不受支持"
        rows = []
        for quote in quotes:
            item = item_by_provider.get(quote.get("provider_symbol"))
            price = quote.get("price")
            if not item or not isinstance(price, (int, float)) or price <= 0:
                continue
            rows.append(
                {
                    "provider_symbol": item["provider_symbol"],
                    "symbol": item["symbol"],
                    "quote_time": quote["quote_time"],
                    "fetched_at": quote.get("fetched_at", _utc_now().isoformat()),
                    "price": float(price),
                    "previous_close": quote.get("previous_close"),
                    "open": quote.get("open"),
                    "high": quote.get("high"),
                    "low": quote.get("low"),
                    "volume": quote.get("volume"),
                    "currency": item["currency"],
                    "market_state": quote.get("market_state", "UNKNOWN"),
                    "source": quote.get("source", "unknown"),
                    "source_tier": quote.get("source_tier", "UNVERIFIED"),
                    "delay_minutes": quote.get("delay_minutes"),
                    "freshness": quote.get("freshness", "UNKNOWN"),
                    "quality_status": quote.get("quality_status", "UNVERIFIED"),
                    "error_code": quote.get("error_code"),
                }
            )
        if rows:
            with self._lock, self._warehouse() as warehouse:
                warehouse.init()
                warehouse.insert(
                    "market_quote_snapshot", rows, conflict_strategy="ignore"
                )
        result = {
            "status": "READY" if rows else "FALLBACK_ONLY",
            "requested": len(symbols),
            "received": len(rows),
            "stored": len(rows),
            "provider_error": provider_error,
            "used_bytes": used_bytes,
            "budget_bytes": budget_bytes,
        }
        self._record_collection_run(
            run_id=run_id,
            started_at=started_at,
            requested=len(symbols),
            received=len(rows),
            stored=len(rows),
            status=result["status"],
            error_code=None if rows else "PROVIDER_EMPTY_OR_UNAVAILABLE",
            error_message=provider_error,
        )
        return result

    def _record_collection_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        requested: int,
        received: int,
        stored: int,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        with self._lock, self._warehouse() as warehouse:
            warehouse.init()
            warehouse.insert(
                "market_quote_collection_run",
                [
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "finished_at": _utc_now(),
                        "source": "yfinance",
                        "requested": requested,
                        "received": received,
                        "stored": stored,
                        "status": status,
                        "error_code": error_code,
                        "error_message": (
                            str(error_message)[:1000] if error_message else None
                        ),
                    }
                ],
            )

    def latest_quotes(self, list_id: str | None = None) -> dict[str, Any]:
        state = self.list_watchlists()
        items = self.items(list_id)
        by_provider: dict[str, dict[str, Any]] = {}
        with self._warehouse() as warehouse:
            if warehouse.table_exists("market_quote_snapshot"):
                rows = warehouse.query(
                    "SELECT * EXCLUDE (rn) FROM ("
                    "SELECT *, ROW_NUMBER() OVER (PARTITION BY provider_symbol "
                    "ORDER BY quote_time DESC, fetched_at DESC) rn "
                    "FROM market_quote_snapshot) WHERE rn=1"
                )
                by_provider = {row["provider_symbol"]: row for row in rows}
        output = []
        now = _utc_now()
        for item in items:
            quote = by_provider.get(item["provider_symbol"])
            row = dict(item)
            if quote:
                quote_time = quote["quote_time"]
                if isinstance(quote_time, datetime):
                    if quote_time.tzinfo is None:
                        quote_time = quote_time.replace(tzinfo=timezone.utc)
                    age_minutes = max(0, int((now - quote_time).total_seconds() / 60))
                else:
                    age_minutes = None
                row.update(quote)
                row["age_minutes"] = age_minutes
                if age_minutes is not None and age_minutes > 36 * 60:
                    row["freshness"] = "STALE"
                    row["quality_status"] = "STALE_SNAPSHOT_FALLBACK"
                elif age_minutes is not None and age_minutes > 15:
                    row["freshness"] = "DELAYED_OR_CLOSED"
                row["fallback"] = row["freshness"] == "STALE"
                previous = quote.get("previous_close")
                row["change"] = (
                    quote["price"] - previous if previous not in (None, 0) else None
                )
                row["change_pct"] = (
                    quote["price"] / previous - 1 if previous not in (None, 0) else None
                )
            else:
                row.update(
                    price=None,
                    quote_time=None,
                    fetched_at=None,
                    source=None,
                    freshness="VALUATION_MISSING",
                    quality_status="NO_SNAPSHOT",
                    delay_minutes=None,
                    age_minutes=None,
                    fallback=False,
                    change=None,
                    change_pct=None,
                )
            output.append(row)
        storage = state.get("storage", {})
        within, used, budget = self._storage_within_budget()
        with self._warehouse() as warehouse:
            collection = (
                warehouse.query(
                    "SELECT run_id, started_at, finished_at, source, requested, "
                    "received, stored, status, error_code, error_message "
                    "FROM market_quote_collection_run ORDER BY finished_at DESC LIMIT 1"
                )[0]
                if warehouse.table_exists("market_quote_collection_run")
                and warehouse.query("SELECT COUNT(*) AS n FROM market_quote_collection_run")[0]["n"]
                else None
            )
        return {
            "status": "READY" if any(row["price"] is not None for row in output) else "NO_DATA",
            "list_id": list_id or state.get("default_list_id"),
            "quotes": output,
            "count": len(output),
            "storage": {
                **storage,
                "used_bytes": used,
                "budget_bytes": budget,
                "within_budget": within,
            },
            "disclaimer": state.get("disclaimer", ""),
            "last_collection": collection,
            "data_contract": {
                "portfolio_valuation_eligible": False,
                "unknown_delay_is_realtime": False,
                "missing_price_is_zero": False,
            },
        }

    def prune(self, *, now: datetime | None = None) -> dict[str, Any]:
        retention = int(
            self._config().get("storage", {}).get("quote_retention_days", 730)
        )
        cutoff = (now or _utc_now()) - timedelta(days=retention)
        with self._lock, self._warehouse() as warehouse:
            if not warehouse.table_exists("market_quote_snapshot"):
                return {"status": "NO_TABLE", "retention_days": retention}
            before = warehouse.query("SELECT COUNT(*) AS n FROM market_quote_snapshot")[0]["n"]
            warehouse.execute(
                "DELETE FROM market_quote_snapshot WHERE quote_time < ?", [cutoff]
            )
            after = warehouse.query("SELECT COUNT(*) AS n FROM market_quote_snapshot")[0]["n"]
        return {
            "status": "READY",
            "retention_days": retention,
            "deleted": before - after,
            "remaining": after,
        }
