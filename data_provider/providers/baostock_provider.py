"""baostock A-share provider with raw-response preservation.

This provider is deliberately scoped to explicitly requested, six-digit
Shanghai/Shenzhen symbols.  It does not discover or silently expand the
investment universe.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..base import (
    DailyPrice,
    DataProvider,
    FinancialData,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)


class BaoStockProvider(DataProvider):
    """No-key fallback for tracked A-share and ETF reference data."""

    def __init__(self, cache_dir: str | Path = "data/raw/cn_stock/baostock"):
        super().__init__("baostock")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client: Any | None = None

    @staticmethod
    def _provider_code(symbol: str) -> str:
        value = symbol.strip()
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"Unsupported baostock symbol: {symbol!r}")
        if value[0] in {"5", "6", "9"}:
            return f"sh.{value}"
        if value[0] in {"0", "1", "2", "3"}:
            return f"sz.{value}"
        raise ValueError(f"Unsupported Shanghai/Shenzhen prefix: {symbol!r}")

    @contextmanager
    def session(self) -> Iterator["BaoStockProvider"]:
        """Open one authenticated transport session for a collection batch."""

        if self._client is not None:
            yield self
            return
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError(
                "baostock 未安装；请安装 requirements.txt 中的固定版本"
            ) from exc
        try:
            login = bs.login()
            error: BaseException | None = None
            if str(login.error_code) != "0":
                error = RuntimeError(f"baostock 登录失败: {login.error_msg}")
        except Exception as exc:  # 连接层故障（疑似封禁）：login 直接抛错
            login, error = None, exc
        if error is not None:
            login = self._escape_login(bs, error)
        self._client = bs
        try:
            yield self
        finally:
            try:
                bs.logout()
            finally:
                self._client = None

    @staticmethod
    def _escape_login(bs: Any, original: BaseException) -> Any:
        """直连登录失败时，经 proxy_guard 轮换出口重试 login。

        proxy_guard 未启用或逃生本身故障时，按原语义抛出原登录错误，
        绝不静默改变行为（fail-closed）。login 失败本就罕见，
        逃生重试是串行 login/logout，不违反单会话约束。
        """

        try:
            from proxy_guard import (EscapeSession, ProxyExhaustedError,
                                     load_config)
            from proxy_guard.baostock_socks import socks_egress
        except ImportError:
            raise original
        cfg = load_config()
        if not cfg.enabled:
            raise original
        try:
            with EscapeSession(cfg, reason="baostock-login") as escape:
                for _ in escape.attempts():
                    try:
                        node = escape.next_egress()
                    except ProxyExhaustedError:
                        break
                    try:
                        with socks_egress(*escape.socks_addr):
                            login = bs.login()
                        if str(login.error_code) == "0":
                            escape.mark_success()
                            return login
                        escape.mark_failure(
                            RuntimeError(f"login error: {login.error_msg}"))
                    except Exception as exc:  # 节点级故障，换下一个
                        escape.mark_failure(exc)
        except Exception:
            raise original
        raise original

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("baostock query requires an active session")
        return self._client

    def _archive_raw(
        self,
        operation: str,
        request: dict[str, Any],
        fields: list[str],
        rows: list[list[str]],
    ) -> None:
        now = datetime.now(timezone.utc)
        day_dir = self.cache_dir / now.strftime("%Y/%m/%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / (
            f"{now.strftime('%H%M%S.%fZ')}_{operation}_{uuid4().hex}.json"
        )
        payload = {
            "schema_version": "foundf.raw.baostock.v1",
            "provider": "baostock",
            "operation": operation,
            "request": request,
            "fields": fields,
            "rows": rows,
            "fetched_at": now.isoformat(),
        }
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _rows(result: Any) -> list[list[str]]:
        if str(result.error_code) != "0":
            raise RuntimeError(f"baostock 查询失败: {result.error_msg}")
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        return rows

    def get_security_basic(self, symbol: str) -> StockBasic | None:
        """Return verified basic data for one explicitly tracked symbol."""

        client = self._require_client()
        provider_code = self._provider_code(symbol)
        result = client.query_stock_basic(code=provider_code)
        rows = self._rows(result)
        self._archive_raw(
            "stock_basic",
            {"code": provider_code},
            list(result.fields),
            rows,
        )
        if len(rows) != 1:
            return None
        record = dict(zip(result.fields, rows[0]))
        name = str(record.get("code_name") or "").strip()
        security_type = str(record.get("type") or "")
        if not name or security_type not in {"1", "5"}:
            return None
        return StockBasic(
            code=symbol,
            name=name,
            market="ETF_CN" if security_type == "5" else "A",
            list_date=str(record.get("ipoDate") or ""),
            status="active" if str(record.get("status")) == "1" else "delisted",
            currency="CNY",
            asset_type="ETF" if security_type == "5" else "STOCK",
        )

    def get_stock_basic(self) -> list[StockBasic]:
        """Universe-wide discovery is intentionally disabled."""

        return []

    def get_trade_calendar(
        self, start_date: str, end_date: str
    ) -> list[TradeCalendar]:
        def _fetch() -> list[TradeCalendar]:
            client = self._require_client()
            result = client.query_trade_dates(
                start_date=start_date, end_date=end_date
            )
            rows = self._rows(result)
            self._archive_raw(
                "trade_calendar",
                {"start_date": start_date, "end_date": end_date},
                list(result.fields),
                rows,
            )
            calendars: list[TradeCalendar] = []
            previous_open: str | None = None
            for values in rows:
                record = dict(zip(result.fields, values))
                calendar_date = str(record.get("calendar_date") or "")
                if not calendar_date:
                    continue
                is_open = str(record.get("is_trading_day")) == "1"
                calendars.append(
                    TradeCalendar(
                        date=calendar_date,
                        is_open=is_open,
                        pretrade_date=previous_open,
                    )
                )
                if is_open:
                    previous_open = calendar_date
            return calendars

        if self._client is not None:
            return _fetch()
        with self.session():
            return _fetch()

    # 个股日线查询字段。isST 自 2026-08-14（PIT Phase 2）起纳入查询：
    # 只进 raw 归档与可选返回，不改变默认 DailyPrice 输出结构。
    _DAILY_FIELDS = (
        "date,code,open,high,low,close,volume,amount,tradestatus,isST"
    )

    def _get_daily_price_in_session(
        self, symbol: str, start_date: str, end_date: str,
        include_suspended: bool = False,
    ) -> list[DailyPrice]:
        client = self._require_client()
        provider_code = self._provider_code(symbol)
        result = client.query_history_k_data_plus(
            provider_code,
            self._DAILY_FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",
        )
        rows = self._rows(result)
        self._archive_raw(
            "daily_price",
            {
                "code": provider_code,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": "d",
                "adjustflag": "2",
            },
            list(result.fields),
            rows,
        )
        prices: list[DailyPrice] = []
        for values in rows:
            record = dict(zip(result.fields, values))
            # 默认丢弃停牌行（tradestatus!=1），保持 nightly 既有行为；
            # include_suspended=True 时停牌行也返回（OHLC=前收盘 volume=0）。
            if str(record.get("tradestatus")) != "1" and not include_suspended:
                continue
            try:
                prices.append(
                    DailyPrice(
                        date=str(record["date"]),
                        symbol=symbol,
                        open=float(record["open"]),
                        high=float(record["high"]),
                        low=float(record["low"]),
                        close=float(record["close"]),
                        volume=float(record.get("volume") or 0),
                        amount=float(record.get("amount") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        prices.sort(key=lambda item: item.date)
        return prices

    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
        include_suspended: bool = False,
    ) -> list[DailyPrice]:
        """日线行情。include_suspended=False（默认）丢弃停牌行，行为与既有
        nightly 完全一致；True 时停牌行一并返回。"""
        if self._client is not None:
            return self._get_daily_price_in_session(
                symbol, start_date, end_date, include_suspended)
        with self.session():
            return self._get_daily_price_in_session(
                symbol, start_date, end_date, include_suspended)

    def get_daily_bars_with_status(
        self, symbol: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """个股日线全行（含停牌行）+ tradestatus/isST 状态字段。

        退市股行情补采（deploy/backfill_delisted_daily.py）专用：
        不做 tradestatus 过滤，每行返回
        ``{date, open, high, low, close, volume, amount, trade_status, is_st}``
        （trade_status/is_st 为 int；isST 缺失或空串时 is_st=-1 未知）。
        raw 响应照常经 ``_archive_raw`` 归档。
        """
        def _fetch() -> list[dict[str, Any]]:
            client = self._require_client()
            provider_code = self._provider_code(symbol)
            result = client.query_history_k_data_plus(
                provider_code,
                self._DAILY_FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            rows = self._rows(result)
            self._archive_raw(
                "daily_price_with_status",
                {
                    "code": provider_code,
                    "start_date": start_date,
                    "end_date": end_date,
                    "frequency": "d",
                    "adjustflag": "2",
                },
                list(result.fields),
                rows,
            )

            def _f(v: Any) -> float | None:
                try:
                    return float(v) if v not in (None, "") else None
                except (ValueError, TypeError):
                    return None

            def _i(v: Any, default: int) -> int:
                try:
                    return int(v) if v not in (None, "") else default
                except (ValueError, TypeError):
                    return default

            out: list[dict[str, Any]] = []
            for values in rows:
                record = dict(zip(result.fields, values))
                out.append({
                    "date": str(record["date"]),
                    "open": _f(record.get("open")),
                    "high": _f(record.get("high")),
                    "low": _f(record.get("low")),
                    "close": _f(record.get("close")),
                    "volume": _f(record.get("volume")) or 0.0,
                    "amount": _f(record.get("amount")) or 0.0,
                    "trade_status": _i(record.get("tradestatus"), 0),
                    "is_st": _i(record.get("isST"), -1),
                })
            out.sort(key=lambda item: item["date"])
            return out

        if self._client is not None:
            return _fetch()
        with self.session():
            return _fetch()

    def get_index_daily(
        self, index_code: str, start_date: str, end_date: str
    ) -> list[DailyPrice]:
        """指数日线（不复权，adjustflag=3）。

        ``index_code`` 形如 ``sh.000300``（沪深300）。指数无 tradestatus
        字段，不做个股 tradestatus 过滤；symbol 原样保留带点号形式，
        不会混入 6 位数字个股池。
        """
        def _fetch() -> list[DailyPrice]:
            client = self._require_client()
            result = client.query_history_k_data_plus(
                index_code,
                "date,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",
            )
            rows = self._rows(result)
            self._archive_raw(
                "index_daily",
                {
                    "code": index_code,
                    "start_date": start_date,
                    "end_date": end_date,
                    "frequency": "d",
                    "adjustflag": "3",
                },
                list(result.fields),
                rows,
            )
            prices: list[DailyPrice] = []
            for values in rows:
                record = dict(zip(result.fields, values))
                try:
                    prices.append(
                        DailyPrice(
                            date=str(record["date"]),
                            symbol=index_code,
                            open=float(record["open"]),
                            high=float(record["high"]),
                            low=float(record["low"]),
                            close=float(record["close"]),
                            volume=float(record.get("volume") or 0),
                            amount=float(record.get("amount") or 0),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            return prices

        if self._client is not None:
            return _fetch()
        with self.session():
            return _fetch()

    def get_daily_basic(
        self, symbol: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """每日估值指标（换手率/PE_TTM/PB），随价格同会话拉取、无限频。

        与 tushare daily_basic 同构返回（pe/total_mv/circ_mv/volume_ratio
        baostock 不提供，置 None）。实测 turn/peTTM/pbMRQ 在 adjustflag=2
        与 3 下取值一致，且历史覆盖至 2007 年（2026-08-06 验证）。
        """
        def _fetch() -> list[dict[str, Any]]:
            client = self._require_client()
            provider_code = self._provider_code(symbol)
            fields = "date,turn,peTTM,pbMRQ,tradestatus"
            result = client.query_history_k_data_plus(
                provider_code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            rows = self._rows(result)
            self._archive_raw(
                "daily_basic",
                {
                    "code": provider_code,
                    "start_date": start_date,
                    "end_date": end_date,
                    "frequency": "d",
                    "adjustflag": "2",
                },
                list(result.fields),
                rows,
            )

            def _f(v: Any) -> float | None:
                try:
                    return float(v) if v not in (None, "") else None
                except (ValueError, TypeError):
                    return None

            out: list[dict[str, Any]] = []
            for values in rows:
                record = dict(zip(result.fields, values))
                if str(record.get("tradestatus")) != "1":
                    continue
                out.append({
                    "date": str(record["date"]),
                    "symbol": symbol,
                    "turnover_rate": _f(record.get("turn")),
                    "volume_ratio": None,
                    "pe": None,
                    "pe_ttm": _f(record.get("peTTM")),
                    "pb": _f(record.get("pbMRQ")),
                    "total_mv": None,
                    "circ_mv": None,
                })
            return out

        if self._client is not None:
            return _fetch()
        with self.session():
            return _fetch()

    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None
    ) -> list[FinancialData]:
        return []

    def health_check(self) -> ProviderHealth:
        try:
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            start = (today - timedelta(days=14)).isoformat()
            with self.session():
                calendar = self.get_trade_calendar(start, today.isoformat())
                open_dates = [item.date for item in calendar if item.is_open]
                prices = self._get_daily_price_in_session(
                    "000333", start, today.isoformat()
                )
            ok = bool(prices)
            actual = max((item.date for item in prices), default="")
            expected = max(open_dates, default=today.isoformat())
            latency = (
                datetime.strptime(expected, "%Y-%m-%d").date()
                - datetime.strptime(actual, "%Y-%m-%d").date()
            ).days if actual else 14
            return ProviderHealth(
                provider_name=self.name,
                status="healthy" if ok and latency <= 1 else "degraded",
                api_available=ok,
                data_latency_days=max(latency, 0),
                message=(
                    f"000333 data_as_of={actual}, expected={expected}"
                    if ok else "无近期行情"
                ),
            )
        except Exception as exc:
            return ProviderHealth(
                provider_name=self.name,
                status="error",
                api_available=False,
                message=str(exc),
            )
