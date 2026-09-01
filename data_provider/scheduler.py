"""
采集调度器 — 编排多个 Provider 的增量数据采集。

调度策略：
    1. A 股日线 → Tushare（主），yfinance（备）
    2. 港股 → HKEX（公告）, yfinance（价格）
    3. 美股 → yfinance（价格）, SEC（财务）
    4. 宏观 → FRED

所有数据经过验证后写入 DuckDB 数据仓库。
"""

from __future__ import annotations

import time
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .base import DataProvider, DailyPrice, ProviderHealth
from .providers.tushare_provider import TushareProvider
from .providers.baostock_provider import BaoStockProvider
from .providers.yfinance_provider import YFinanceProvider
from .providers.hkex_provider import HKEXProvider
from .providers.sec_provider import SECProvider
from .providers.fred_provider import FREDProvider
from .validator import DataValidator, ValidationResult

from foundf_db import Warehouse, DataProvider as WarehouseDP
from source_registry import SourceRegistry
from .external_intelligence import (
    BraveSearchProvider,
    ExternalEvidenceRouter,
    GoogleCustomSearchProvider,
    InvestingAuthorizedImportProvider,
    persist_router_result,
)
from .providers.bloomberg_provider import BloombergReferenceProvider
from foundf_db.event_store import InvestmentEventStore

BAOSTOCK_BASIC_HISTORY_FLOOR = "2007-01-01"


class CollectorScheduler:
    """采集调度器。

    使用方式:
        scheduler = CollectorScheduler()
        results = scheduler.run_daily()
    """

    def __init__(
        self,
        duckdb_path: str | Path = "data/finance.duckdb",
        raw_base: str | Path = "data/raw",
    ):
        self.duckdb_path = duckdb_path
        self.raw_base = Path(raw_base)
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        self.validator = DataValidator()
        # Phase O: 数据源可靠性注册表 — deprecated 源自动跳过，
        # 每次采集结果回写以更新 source_score
        self.registry = SourceRegistry()
        self.external_router = ExternalEvidenceRouter(
            [
                InvestingAuthorizedImportProvider(),
                BraveSearchProvider(),
                GoogleCustomSearchProvider(),
            ]
        )
        self.bloomberg = BloombergReferenceProvider()
        self.event_store = InvestmentEventStore(
            data_root=self.raw_base.parent,
            db_path=self.duckdb_path,
        )

        # 注册 Provider
        self.providers: dict[str, DataProvider] = {}
        self._register_providers()

    def _register_providers(self) -> None:
        """注册所有 Provider（静默处理缺失 API key 的情况）。"""
        provider_classes = [
            ("baostock", BaoStockProvider, {}),
            ("tushare", TushareProvider, {}),
            ("yfinance", YFinanceProvider, {}),
            ("hkex", HKEXProvider, {}),
            ("sec", SECProvider, {}),
            ("fred", FREDProvider, {}),
        ]
        for name, cls, kwargs in provider_classes:
            try:
                self.providers[name] = cls(**kwargs)
            except (RuntimeError, ImportError) as e:
                print(f"  [scheduler] ⚠ {name}: {e}")

    def run_daily(self) -> dict[str, Any]:
        """执行每日采集。返回统计。"""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        year_ago = (now - timedelta(days=365)).strftime("%Y-%m-%d")

        stats: dict[str, Any] = {
            "date": today,
            "providers": {},
            "total_prices": 0,
            "validation_results": [],
        }

        # Phase O: 各采集任务 → (源名, 采集函数)。deprecated 源跳过。
        # A 股日线主源固定为 baostock（adjustflag=2 前复权）。daily_price 按
        # UNIQUE(symbol,date) 后写覆盖，tushare daily 返回未复权原始价且
        # volume 单位为手（baostock 为股），若作为主源会静默改写全表口径，
        # 2026-08-06 已因此覆盖过 7 只票一年的前复权历史。tushare 仅用于
        # daily_basic 估值因子与 stk_mins 准实时价等补充用途。
        tasks: list[tuple[str, Any]] = []
        if "baostock" in self.providers:
            tasks.append(
                ("baostock_cn", ("baostock", self._collect_cn_stocks_baostock))
            )
        elif "tushare" in self.providers:
            tasks.append(("tushare", ("tushare", self._collect_cn_stocks)))
        # 每日估值指标（daily_basic 表）已并入 baostock_cn 任务随价格同会话
        # 拉取（turn/peTTM/pbMRQ，无限频，2026-08-06 起）；tushare
        # daily_basic 需 2000 积分且限频 1 次/分钟，仅留作 provider 兜底。
        if "yfinance" in self.providers:
            tasks.append(("yfinance_hk", ("yfinance", self._collect_hk_stocks)))
            tasks.append(("yfinance_us", ("yfinance", self._collect_us_stocks)))
            tasks.append(("benchmarks", ("yfinance", self._collect_benchmarks)))
        if "hkex" in self.providers:
            tasks.insert(
                1 if tasks else 0,
                ("hkex_basic", ("hkex", self._collect_hk_security_master)),
            )

        for task_name, (source, fn) in tasks:
            if self.registry.get_weight(source) == 0.0:
                print(f"  [scheduler] ⛔ {source} 已被 source_registry 弃用，跳过 {task_name}")
                stats["providers"][task_name] = {"skipped": "deprecated_source"}
                continue
            t0 = time.time()
            result = fn()
            stats["providers"][task_name] = result
            stats["total_prices"] += int(result.get("prices", 0) or 0)
            self.registry.record_fetch(
                source,
                success=(
                    not result.get("error")
                    and not result.get("failed_symbols")
                    and not result.get("basic_failures")
                    and not result.get("index_error")
                    and not result.get("skipped")
                ),
                records=result.get("prices", 0) or result.get("symbols", 0),
                expected=0,
                latency_s=time.time() - t0,
            )

        # 回写当日 source_score
        stats["source_scores"] = self.registry.update_scores()
        if os.getenv("EXTERNAL_INTELLIGENCE_ENABLED", "").lower() == "true":
            queries = [
                item.strip()
                for item in os.getenv(
                    "EXTERNAL_INTELLIGENCE_QUERIES", ""
                ).split(",")
                if item.strip()
            ]
            stats["external_intelligence"] = self.run_external_intelligence(
                queries
            )

        return stats

    def run_external_intelligence(
        self, queries: list[str]
    ) -> dict[str, Any]:
        """运行外部线索发现；搜索摘要不直接进入投资结论。"""

        if not queries:
            return {"status": "SKIPPED", "reason": "NO_QUERIES", "queries": []}
        results = []
        for query in queries:
            routed = self.external_router.search(query)
            audit = persist_router_result(
                routed,
                base_dir=self.raw_base / "external_evidence",
            )
            candidate = self.event_store.queue_external_candidate(
                {
                    "query_hash": audit["query_hash"],
                    "assessment": routed["assessment"],
                    "stored_count": audit["stored_count"],
                    "provider_status": routed["provider_status"],
                    "audit_relpath": (
                        self.raw_base
                        / "external_evidence"
                        / f"audit_{datetime.now(timezone.utc).date().isoformat()}.jsonl"
                    ).relative_to(self.raw_base.parent).as_posix(),
                },
                confirmation_reference="EXTERNAL_INTELLIGENCE_ENABLED",
            )
            for provider, status in routed["provider_status"].items():
                self.registry.record_fetch(
                    provider,
                    success=status.startswith("OK:"),
                    records=int(status.split(":")[-1]) if status.startswith("OK:") else 0,
                    expected=0,
                    latency_s=0.0,
                )
            results.append(
                {
                    "query_hash": audit["query_hash"],
                    "assessment": routed["assessment"],
                    "provider_status": routed["provider_status"],
                    "stored_count": audit["stored_count"],
                    "transient_count": audit["transient_count"],
                    "candidate_id": candidate["candidate_id"],
                }
            )
        return {"status": "COMPLETE", "queries": results}

    def fetch_bloomberg_reference(
        self, securities: list[str], fields: list[str]
    ) -> dict[str, Any]:
        """显式 Bloomberg 请求；未确认授权时返回 UNAVAILABLE。"""

        result = self.bloomberg.fetch_reference(securities, fields)
        self.registry.record_fetch(
            "bloomberg",
            success=result.get("status") == "READY",
            records=len(result.get("items", [])),
            expected=len(securities),
            latency_s=0.0,
        )
        return result

    # ── 各市场采集 ────────────────────────────────────

    def _tracked_cn_symbols(self) -> list[str]:
        rows = self.warehouse.query(
            "SELECT DISTINCT symbol FROM daily_price "
            "WHERE length(symbol) = 6 "
            "AND regexp_matches(symbol, '^[0-9]{6}$') "
            "ORDER BY symbol"
        )
        return [str(row["symbol"]) for row in rows]

    @staticmethod
    def _next_date(value: str) -> str:
        return (
            datetime.strptime(value, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")

    @staticmethod
    def _weekday_open_in_window(start: str, end: str) -> bool:
        current = datetime.strptime(start, "%Y-%m-%d").date()
        finish = datetime.strptime(end, "%Y-%m-%d").date()
        while current <= finish:
            if current.weekday() < 5:
                return True
            current += timedelta(days=1)
        return False

    def _baostock_window_has_open_day(
        self,
        provider: Any,
        start: str,
        end: str,
        cache: dict[tuple[str, str], bool],
    ) -> bool:
        """Use the vendor calendar when available, with a weekday fallback."""

        key = (start, end)
        if key in cache:
            return cache[key]
        try:
            calendar = provider.get_trade_calendar(start, end)
            if calendar:
                cache[key] = any(item.is_open for item in calendar)
                return cache[key]
        except (AttributeError, NotImplementedError):
            pass
        cache[key] = self._weekday_open_in_window(start, end)
        return cache[key]

    def _collect_cn_stocks_baostock(self) -> dict[str, Any]:
        """Refresh only existing tracked A-share/ETF symbols via baostock."""

        provider = self.providers["baostock"]
        symbols = self._tracked_cn_symbols()
        stats: dict[str, Any] = {
            "tracked_symbols": len(symbols),
            "symbols": 0,
            "prices": 0,
            "basic_rows": 0,
            "status_rows": 0,
            "failed_symbols": [],
            "basic_failures": [],
            "expected_empty_windows": 0,
            "suspended_windows": 0,
            "error": "",
        }
        if not symbols:
            stats["error"] = "NO_TRACKED_CN_SYMBOLS"
            return stats

        end = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        # 历史退市股由 backfill_delisted_daily.py 专门补采和对账。nightly
        # 不再依赖 daily_price MAX(date) 追到正式退市日：长期停牌股票的
        # 最后交易日天然早于 out_date，否则会永久重复拉取停牌行。
        delisted_out: dict[str, str] = {}
        try:
            for row in self.warehouse.query(
                "SELECT symbol, out_date::VARCHAR AS out_date "
                "FROM stock_registry WHERE list_status = 'DELISTED'"
            ):
                if row["out_date"]:
                    delisted_out[str(row["symbol"])] = str(row["out_date"])
        except Exception:
            delisted_out = {}  # 注册表缺失时退化为逐票采集（原行为）
        stats["skipped_delisted"] = 0
        calendar_cache: dict[tuple[str, str], bool] = {}
        try:
            with provider.session():
                for symbol in symbols:
                    if symbol in delisted_out and delisted_out[symbol] <= end:
                        stats["skipped_delisted"] += 1
                        continue
                    try:
                        # 主数据刷新不是价格硬门禁；本地已有主数据时，远端
                        # stock_basic 瞬时故障不得阻断行情主线。
                        try:
                            basic = provider.get_security_basic(symbol)
                            if basic is not None:
                                self.warehouse.insert(
                                    "stock_basic",
                                    [{
                                        "code": basic.code,
                                        "name": basic.name,
                                        "market": basic.market,
                                        "asset_type": basic.asset_type,
                                        "industry": basic.industry or None,
                                        "list_date": basic.list_date or None,
                                        "status": basic.status,
                                        "currency": basic.currency,
                                    }],
                                    conflict_strategy="replace",
                                )
                            else:
                                stats["basic_failures"].append(
                                    {"symbol": symbol, "reason": "BASIC_UNAVAILABLE"}
                                )
                        except Exception as exc:
                            stats["basic_failures"].append(
                                {"symbol": symbol, "reason": type(exc).__name__}
                            )

                        if hasattr(provider, "get_daily_bars_with_status"):
                            latest_status = self.warehouse.query(
                                "SELECT MAX(date)::VARCHAR AS latest "
                                "FROM stock_status_daily WHERE symbol = ?",
                                [symbol],
                            )[0]["latest"]
                            missing_trade_price = self.warehouse.query(
                                "SELECT MIN(s.date)::VARCHAR AS first_missing "
                                "FROM stock_status_daily s "
                                "LEFT JOIN daily_price p ON p.symbol = s.symbol "
                                "AND p.date = s.date "
                                "WHERE s.symbol = ? AND s.trade_status = 1 "
                                "AND p.symbol IS NULL",
                                [symbol],
                            )[0]["first_missing"]
                            missing_price_status = self.warehouse.query(
                                "SELECT MIN(p.date)::VARCHAR AS first_missing "
                                "FROM daily_price p "
                                "LEFT JOIN stock_status_daily s "
                                "ON s.symbol = p.symbol AND s.date = p.date "
                                "WHERE p.symbol = ? AND s.symbol IS NULL",
                                [symbol],
                            )[0]["first_missing"]
                            internal_gaps = [
                                value for value in (
                                    missing_trade_price, missing_price_status
                                ) if value
                            ]
                            if internal_gaps:
                                price_start = min(internal_gaps)
                            elif latest_status:
                                price_start = self._next_date(latest_status)
                            else:
                                first_price = self.warehouse.query(
                                    "SELECT MIN(date)::VARCHAR AS first "
                                    "FROM daily_price WHERE symbol = ?",
                                    [symbol],
                                )[0]["first"]
                                price_start = first_price
                        else:
                            latest_price = self.warehouse.query(
                                "SELECT MAX(date)::VARCHAR AS latest "
                                "FROM daily_price WHERE symbol = ?",
                                [symbol],
                            )[0]["latest"]
                            price_start = self._next_date(latest_price)

                        if price_start <= end:
                            if not self._baostock_window_has_open_day(
                                provider, price_start, end, calendar_cache
                            ):
                                stats["expected_empty_windows"] += 1
                            else:
                                status_rows: list[dict[str, Any]] = []
                                if hasattr(provider, "get_daily_bars_with_status"):
                                    bars = provider.get_daily_bars_with_status(
                                        symbol, price_start, end
                                    )
                                    prices: list[DailyPrice] = []
                                    for bar in bars:
                                        status_rows.append({
                                            "date": bar["date"],
                                            "symbol": symbol,
                                            "trade_status": bar["trade_status"],
                                            "is_st": bar["is_st"],
                                            "source": "baostock",
                                        })
                                        if bar["trade_status"] != 1:
                                            continue
                                        ohlc = [bar.get(key) for key in (
                                            "open", "high", "low", "close"
                                        )]
                                        if any(value is None for value in ohlc):
                                            raise ValueError("malformed tradable daily bar")
                                        prices.append(DailyPrice(
                                            date=bar["date"], symbol=symbol,
                                            open=float(bar["open"]),
                                            high=float(bar["high"]),
                                            low=float(bar["low"]),
                                            close=float(bar["close"]),
                                            volume=float(bar.get("volume") or 0),
                                            amount=float(bar.get("amount") or 0),
                                        ))
                                else:
                                    prices = provider.get_daily_price(
                                        symbol, price_start, end
                                    )

                                if prices:
                                    validation = self.validator.validate_daily_prices(
                                        prices, "baostock"
                                    )
                                    self._save_validation(validation)
                                    if validation.status == "error":
                                        stats["failed_symbols"].append({
                                            "symbol": symbol,
                                            "reason": "VALIDATION_ERROR",
                                        })
                                    else:
                                        if status_rows:
                                            self.warehouse.conn.execute("BEGIN")
                                            try:
                                                self._write_prices(prices, "baostock")
                                                self.warehouse.insert(
                                                    "stock_status_daily", status_rows,
                                                    conflict_strategy="replace",
                                                )
                                                self.warehouse.conn.execute("COMMIT")
                                            except Exception:
                                                self.warehouse.conn.execute("ROLLBACK")
                                                raise
                                            stats["status_rows"] += len(status_rows)
                                        else:
                                            self._write_prices(prices, "baostock")
                                        stats["prices"] += len(prices)
                                        stats["symbols"] += 1
                                elif status_rows and all(
                                    row["trade_status"] == 0 for row in status_rows
                                ):
                                    self.warehouse.insert(
                                        "stock_status_daily", status_rows,
                                        conflict_strategy="replace",
                                    )
                                    stats["status_rows"] += len(status_rows)
                                    stats["suspended_windows"] += 1
                                else:
                                    stats["failed_symbols"].append({
                                        "symbol": symbol,
                                        "reason": "PROVIDER_EMPTY",
                                    })

                        # daily_basic 使用自己的游标；价格先成功、估值后失败时，
                        # 下一轮仍会从估值缺口继续补，而不会被价格游标跳过。
                        latest_basic = self.warehouse.query(
                            "SELECT MAX(date)::VARCHAR AS latest "
                            "FROM daily_basic WHERE symbol = ?",
                            [symbol],
                        )[0]["latest"]
                        first_missing_basic = self.warehouse.query(
                            "SELECT MIN(p.date)::VARCHAR AS first_missing "
                            "FROM daily_price p LEFT JOIN daily_basic b "
                            "ON b.symbol = p.symbol AND b.date = p.date "
                            "WHERE p.symbol = ? AND p.date >= ?::DATE "
                            "AND p.date <= ?::DATE AND b.symbol IS NULL",
                            [symbol, BAOSTOCK_BASIC_HISTORY_FLOOR, end],
                        )[0]["first_missing"]
                        basic_start = (
                            first_missing_basic
                            or (self._next_date(latest_basic) if latest_basic else None)
                            or self.warehouse.query(
                                "SELECT GREATEST(MIN(date), ?::DATE)::VARCHAR AS first "
                                "FROM daily_price WHERE symbol = ?",
                                [BAOSTOCK_BASIC_HISTORY_FLOOR, symbol],
                            )[0]["first"]
                        )
                        if (
                            basic_start <= end
                            and self._baostock_window_has_open_day(
                                provider, basic_start, end, calendar_cache
                            )
                        ):
                            try:
                                basic_rows = provider.get_daily_basic(
                                    symbol, basic_start, end
                                )
                                for row in basic_rows:
                                    row["source"] = "baostock"
                                expected_dates = {
                                    str(row["date"])
                                    for row in self.warehouse.query(
                                        "SELECT date::VARCHAR AS date "
                                        "FROM daily_price WHERE symbol = ? "
                                        "AND date BETWEEN ? AND ?",
                                        [symbol, basic_start, end],
                                    )
                                }
                                returned_dates = {
                                    str(row["date"]) for row in basic_rows
                                }
                                if expected_dates - returned_dates:
                                    stats["basic_failures"].append({
                                        "symbol": symbol,
                                        "reason": "INCOMPLETE_DAILY_BASIC",
                                    })
                                    continue
                                if basic_rows:
                                    self.warehouse.insert(
                                        "daily_basic", basic_rows,
                                        conflict_strategy="replace",
                                    )
                                    stats["basic_rows"] += len(basic_rows)
                            except Exception as exc:
                                stats["basic_failures"].append({
                                    "symbol": symbol,
                                    "reason": type(exc).__name__,
                                })
                    except Exception as exc:
                        stats["failed_symbols"].append(
                            {
                                "symbol": symbol,
                                "reason": type(exc).__name__,
                            }
                        )
            if stats["failed_symbols"] and stats["prices"] == 0:
                stats["error"] = "all tracked symbols failed"
            if stats["failed_symbols"] or stats["basic_failures"]:
                provider.record_error()
            else:
                provider.record_success()
        except Exception as exc:
            stats["error"] = str(exc)
            provider.record_error()

        # 基准指数（沪深300）：绩效归因与回归相对收益的基准口径；
        # symbol 带点号不混入 6 位个股池，失败不阻塞个股采集。
        # 与个股同一套 validator 校验，error 时拒绝写入（fail-closed）。
        try:
            latest_index = self.warehouse.query(
                "SELECT MAX(date)::VARCHAR AS latest FROM daily_price "
                "WHERE symbol = 'sh.000300'"
            )[0]["latest"]
            index_start = self._next_date(latest_index) if latest_index else "2020-01-01"
            index_expected = (
                index_start <= end
                and self._baostock_window_has_open_day(
                    provider, index_start, end, calendar_cache
                )
            )
            if index_expected:
                idx_prices = provider.get_index_daily(
                    "sh.000300", index_start, end
                )
            else:
                idx_prices = []
            if idx_prices:
                validation = self.validator.validate_daily_prices(
                    idx_prices, "baostock"
                )
                self._save_validation(validation)
                if validation.status == "error":
                    stats["index_error"] = "VALIDATION_ERROR"
                else:
                    self._write_prices(idx_prices, "baostock")
                    stats["index_prices"] = len(idx_prices)
            elif index_expected:
                stats["index_error"] = "PROVIDER_EMPTY"
        except Exception as exc:
            stats["index_error"] = str(exc)
        return stats

    def _collect_cn_stocks(self) -> dict[str, Any]:
        """增量采集 A 股。"""
        provider = self.providers["tushare"]
        stats: dict[str, Any] = {"symbols": 0, "prices": 0, "error": ""}

        try:
            # 获取股票列表（每季度一次即可，但这里每次都获取以确保新股票入库）
            stocks = provider.get_stock_basic()
            if not stocks:
                return {"symbols": 0, "prices": 0, "error": "空列表"}
            tracked = set(self._tracked_cn_symbols())
            stocks = [stock for stock in stocks if stock.code in tracked]
            if not stocks:
                return {
                    "symbols": 0,
                    "prices": 0,
                    "error": "无已追踪 A 股/ETF 标的",
                }

            # 写入 stock_basic
            for s in stocks:
                self.warehouse.insert("stock_basic", [{
                    "code": s.code, "name": s.name, "market": "A",
                    "industry": s.industry, "list_date": s.list_date,
                    "status": s.status, "currency": s.currency,
                }], conflict_strategy="replace")

            stats["symbols"] = len(stocks)

            # 增量采集日线（只获取最近 365 天）
            end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
            count = 0

            for s in stocks:
                try:
                    prices = provider.get_daily_price(s.code, start, end)
                    if not prices:
                        continue
                    # 验证
                    vr = self.validator.validate_daily_prices(prices, "tushare")
                    self._save_validation(vr)
                    if vr.status == "error":
                        continue
                    # 写入 DuckDB
                    self._write_prices(prices, "tushare")
                    count += len(prices)
                    time.sleep(0.5)  # 频率限制
                except Exception as e:
                    print(f"    ⚠ {s.code}: {e}")
                    continue

            stats["prices"] = count
            provider.record_success()

        except Exception as e:
            stats["error"] = str(e)
            provider.record_error()

        return stats

    def _collect_hk_stocks(self) -> dict[str, Any]:
        """增量采集港股。"""
        provider = self.providers["yfinance"]
        stats: dict[str, Any] = {
            "tracked_symbols": 0,
            "symbols": 0,
            "prices": 0,
            "failed_symbols": [],
        }

        hk_symbols = [
            str(row["symbol"])
            for row in self.warehouse.query(
                "SELECT DISTINCT symbol FROM daily_price "
                "WHERE symbol LIKE '%.HK' ORDER BY symbol"
            )
        ]
        stats["tracked_symbols"] = len(hk_symbols)
        end_exclusive = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        for sym in hk_symbols:
            try:
                latest = self.warehouse.query(
                    "SELECT MAX(date)::VARCHAR AS latest "
                    "FROM daily_price WHERE symbol = ?",
                    [sym],
                )[0]["latest"]
                start = (
                    datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)
                ).strftime("%Y-%m-%d")
                if start >= end_exclusive:
                    continue
                prices = provider.get_daily_price(sym, start, end_exclusive)
                if prices:
                    validation = self.validator.validate_daily_prices(
                        prices, "yfinance"
                    )
                    self._save_validation(validation)
                    if validation.status == "error":
                        continue
                    self._write_prices(prices, "yfinance")
                    stats["prices"] += len(prices)
                    stats["symbols"] += 1
                time.sleep(1)
            except Exception as exc:
                stats["failed_symbols"].append(
                    {"symbol": sym, "reason": type(exc).__name__}
                )
                continue

        return stats

    def _collect_hk_security_master(self) -> dict[str, Any]:
        """Refresh tracked HK security names from the official HKEX list."""

        provider = self.providers["hkex"]
        tracked = [
            str(row["symbol"])
            for row in self.warehouse.query(
                "SELECT DISTINCT symbol FROM daily_price "
                "WHERE symbol LIKE '%.HK' ORDER BY symbol"
            )
        ]
        stats: dict[str, Any] = {
            "tracked_symbols": len(tracked),
            "symbols": 0,
            "missing_symbols": [],
            "error": "",
        }
        if not tracked:
            return stats
        try:
            basics = provider.get_security_basics(tracked)
            by_code = {item.code: item for item in basics}
            for symbol in tracked:
                basic = by_code.get(symbol)
                if basic is None:
                    stats["missing_symbols"].append(symbol)
                    continue
                self.warehouse.insert(
                    "stock_basic",
                    [{
                        "code": basic.code,
                        "name": basic.name,
                        "market": basic.market,
                        "asset_type": basic.asset_type,
                        "industry": basic.industry or None,
                        "list_date": basic.list_date or None,
                        "status": basic.status,
                        "currency": basic.currency,
                    }],
                    conflict_strategy="replace",
                )
                stats["symbols"] += 1
            if stats["missing_symbols"]:
                stats["error"] = "official HKEX list missing tracked symbols"
            else:
                provider.record_success()
        except Exception as exc:
            stats["error"] = str(exc)
            provider.record_error()
        return stats

    def _collect_us_stocks(self) -> dict[str, Any]:
        """增量采集美股。"""
        provider = self.providers["yfinance"]
        stats: dict[str, Any] = {"symbols": 0, "prices": 0}

        us_symbols = [
            str(row["symbol"])
            for row in self.warehouse.query(
                "SELECT DISTINCT dp.symbol FROM daily_price dp "
                "JOIN stock_basic sb ON sb.code = dp.symbol "
                "WHERE sb.market = 'US' ORDER BY dp.symbol"
            )
        ]
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")

        for sym in us_symbols:
            try:
                prices = provider.get_daily_price(sym, start, end)
                if prices:
                    validation = self.validator.validate_daily_prices(
                        prices, "yfinance"
                    )
                    self._save_validation(validation)
                    if validation.status == "error":
                        continue
                    self._write_prices(prices, "yfinance")
                    stats["prices"] += len(prices)
                    stats["symbols"] += 1
                time.sleep(1)
            except Exception:
                continue

        return stats

    def _collect_benchmarks(self) -> dict[str, Any]:
        """采集宏观基准。"""
        provider = self.providers["yfinance"]
        benchmarks = {
            "CSI300": "000300.SS", "SP500": "^GSPC",
            "NASDAQ": "^IXIC", "HSI": "^HSI",
        }
        tracked = {
            str(row["symbol"])
            for row in self.warehouse.query(
                "SELECT DISTINCT symbol FROM daily_price "
                "WHERE symbol IN ('CSI300', 'SP500', 'NASDAQ', 'HSI')"
            )
        }
        benchmarks = {
            name: ticker for name, ticker in benchmarks.items() if name in tracked
        }
        stats: dict[str, Any] = {"symbols": 0, "prices": 0}
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")

        for name, ticker in benchmarks.items():
            try:
                prices = provider.get_daily_price(ticker, start, end)
                if prices:
                    for p in prices:
                        p.symbol = name
                    validation = self.validator.validate_daily_prices(
                        prices, "yfinance"
                    )
                    self._save_validation(validation)
                    if validation.status == "error":
                        continue
                    self._write_prices(prices, "yfinance")
                    stats["prices"] += len(prices)
                    stats["symbols"] += 1
                time.sleep(0.5)
            except Exception:
                continue

        return stats

    # ── 写入与验证 ────────────────────────────────────

    def _write_prices(self, prices: list[DailyPrice], source: str) -> None:
        """将日线数据写入 DuckDB。quality_score 取自 source_registry 实时评分。"""
        src = self.registry._sources.get(source, {})
        quality = src.get("score", 100)
        rows = []
        for p in prices:
            rows.append({
                "symbol": p.symbol, "date": p.date,
                "open": p.open, "high": p.high, "low": p.low,
                "close": p.close, "volume": p.volume, "amount": p.amount,
                "source": source, "quality_score": quality,
            })
        if rows:
            self.warehouse.insert("daily_price", rows, conflict_strategy="replace")

    def _save_validation(self, result: ValidationResult) -> None:
        """保存验证结果（供后续分析）。"""
        if result.status != "healthy":
            print(f"    ⚠ {result.provider_name}: {len(result.warnings)} warnings, "
                  f"{len(result.errors)} errors")

    def health_check_all(self) -> list[ProviderHealth]:
        """检查所有 Provider 的健康状态。"""
        results = []
        for name, provider in self.providers.items():
            try:
                health = provider.health_check()
                results.append(health)
            except Exception as e:
                results.append(ProviderHealth(
                    provider_name=name, status="error", message=str(e),
                ))
        return results

    def close(self) -> None:
        self.warehouse.close()
