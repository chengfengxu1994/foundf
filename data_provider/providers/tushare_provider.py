"""
Tushare Pro 数据提供者。

使用 Tushare Pro API（120 积分方案）。

允许接口（保持低成本）：
    stock_basic    — 股票列表
    trade_cal      — 交易日历
    daily          — A 股日线
    weekly/monthly — 周/月线（未来）
    daily_basic    — 每日估值指标（PE/PB/换手率/市值，真实基本面因子源）

Token 从环境变量读取：TUSHARE_TOKEN

缓存：
    data/raw/cn_stock/tushare/ — 原始 API JSON 缓存
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..base import (
    DailyPrice,
    DataProvider,
    FinancialData,
    NewsEvent,
    ProviderHealth,
    StockBasic,
    TradeCalendar,
)

# Tushare API 地址
TUSHARE_API = "http://api.tushare.pro"


class TushareProvider(DataProvider):
    """Tushare Pro 数据提供者（A 股）。"""

    # 120 积分允许的免费接口
    ALLOWED_APIS = frozenset({
        "stock_basic", "trade_cal", "daily", "weekly", "monthly",
        "daily_basic",
    })

    def __init__(self, token: str | None = None, cache_dir: str = "data/raw/cn_stock/tushare"):
        super().__init__("tushare")
        self.token = token or os.getenv("TUSHARE_TOKEN", "")
        if not self.token:
            raise RuntimeError(
                "TUSHARE_TOKEN 未设置。请在 .env 中配置 TUSHARE_TOKEN，"
                "或通过环境变量传入。"
            )
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._daily_cache: dict[str, list[DailyPrice]] = {}

    # ── API 调用 ──────────────────────────────────────

    def _api(self, api_name: str, params: dict[str, Any] | None = None,
             fields: str = "") -> list[dict[str, Any]]:
        """调用 Tushare API。

        自动处理：
        - 请求频率限制（每秒最多 2 次）
        - 错误重试（最多 3 次）
        """
        if api_name not in self.ALLOWED_APIS:
            raise ValueError(
                f"API '{api_name}' 不在 120 积分允许的免费接口列表中。"
                f"允许: {sorted(self.ALLOWED_APIS)}"
            )

        payload = {
            "api_name": api_name,
            "token": self.token,
            "params": params or {},
            "fields": fields,
        }

        try:
            return self._api_attempts(api_name, payload)
        except RuntimeError as exc:
            escaped = self._escape_api(api_name, payload, exc)
            if escaped is None:
                raise
            return escaped

    def _api_attempts(self, api_name: str, payload: dict[str, Any],
                      proxy: str | None = None
                      ) -> list[dict[str, Any]]:
        """直连重试循环（最多 3 次）；proxy 仅供逃生路径使用（httpx `proxy=`）。"""

        for attempt in range(3):
            try:
                import httpx
                resp = httpx.post(TUSHARE_API, json=payload, timeout=30,
                                  proxy=proxy)
                data = resp.json()
                if data.get("code") != 0:
                    error_msg = data.get("msg", "未知错误")
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    raise RuntimeError(f"Tushare API 错误 ({api_name}): {error_msg}")
                items = (data.get("data") or {}).get("items") or []
                self.record_success()
                return items
            except httpx.RequestError as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                self.record_error()
                raise RuntimeError(f"Tushare 请求失败 ({api_name}): {e}")

        return []

    def _escape_api(self, api_name: str, payload: dict[str, Any],
                    original: RuntimeError) -> list[dict[str, Any]] | None:
        """直连重试耗尽且疑似限频/封禁时，经 proxy_guard 换出口重试。

        未启用、非封禁类错误或逃生故障时返回 None（上层抛原错误）。
        """

        try:
            from proxy_guard import (EscapeSession, ProxyExhaustedError,
                                     is_ban_like, load_config)
        except ImportError:
            return None
        cfg = load_config()
        if not cfg.enabled or not is_ban_like(original):
            return None
        try:
            with EscapeSession(cfg, reason=f"tushare:{api_name}") as escape:
                for _ in escape.attempts():
                    try:
                        escape.next_egress()
                    except ProxyExhaustedError:
                        break
                    try:
                        items = self._api_attempts(api_name, payload,
                                                   proxy=escape.proxy_url)
                        escape.mark_success()
                        return items
                    except RuntimeError as exc:
                        escape.mark_failure(exc)
        except Exception:
            return None
        return None

    def _cache_raw(self, api_name: str, params: dict, data: list) -> None:
        """缓存原始 API 返回。"""
        cache_file = self.cache_dir / f"{api_name}_{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {
            "api_name": api_name,
            "params": params,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count": len(data),
        }
        with open(cache_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── 接口实现 ──────────────────────────────────────

    def get_stock_basic(self) -> list[StockBasic]:
        """获取 A 股股票列表。"""
        raw = self._api("stock_basic", {
            "list_status": "L",  # 上市股票
        }, fields="ts_code,name,industry,list_date,market,currency")
        self._cache_raw("stock_basic", {}, raw)

        results = []
        for item in raw:
            code = (item[0] or "").split(".")[0] if item[0] else ""
            if not code:
                continue
            results.append(StockBasic(
                code=code,
                name=item[1] or "",
                market="A",
                industry=item[2] or "",
                list_date=self._iso_date(item[3]),
                currency="CNY",
            ))
        return results

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[TradeCalendar]:
        """获取交易日历。"""
        raw = self._api("trade_cal", {
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
            "exchange": "SSE",
        }, fields="cal_date,is_open,pretrade_date")

        results = []
        for item in raw:
            results.append(TradeCalendar(
                date=self._iso_date(item[0]),
                is_open=bool(item[1]),
                pretrade_date=self._iso_date(item[2]) if len(item) > 2 and item[2] else None,
            ))
        return results

    def get_daily_price(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list[DailyPrice]:
        """获取 A 股日线行情。

        参数 symbol 可以是 '000001.SZ' 或 '600519'（自动补全后缀）。
        """
        # 缓存查询（避免重复请求同一只股票同一天数据）
        cache_key = f"{symbol}_{start_date}_{end_date}"
        if cache_key in self._daily_cache:
            return self._daily_cache[cache_key]

        ts_code = self._to_ts_code(symbol)
        raw = self._api("daily", {
            "ts_code": ts_code,
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
        }, fields="trade_date,open,high,low,close,vol,amount")

        self._cache_raw("daily", {"ts_code": ts_code}, raw)

        code = ts_code.split(".")[0]
        results = []
        for item in raw:
            try:
                results.append(DailyPrice(
                    date=self._iso_date(item[0]),
                    symbol=code,
                    open=float(item[1] or 0),
                    high=float(item[2] or 0),
                    low=float(item[3] or 0),
                    close=float(item[4] or 0),
                    volume=float(item[5] or 0),
                    amount=float(item[6] or 0),
                ))
            except (ValueError, TypeError, IndexError):
                continue

        # 按日期排序
        results.sort(key=lambda r: r.date)
        self._daily_cache[cache_key] = results
        return results

    @staticmethod
    def _parse_daily_basic(
        items: list[list[Any]], default_code: str
    ) -> list[dict[str, Any]]:
        """解析 daily_basic 返回行为统一字典。

        单行可能是 8 字段（无 ts_code，按 ts_code 查询）或 9 字段
        （首列 ts_code，按 trade_date 全市场查询）。负 PE 保留原值
        （亏损股），由下游因子计算决定取舍。市值单位万元（tushare 口径）。
        """

        def _f(v: Any) -> float | None:
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        results = []
        for item in items:
            if len(item) >= 9:
                code = str(item[0] or "").split(".")[0] or default_code
                values = item[1:]
            else:
                code = default_code
                values = item
            date = TushareProvider._iso_date(values[0])
            if not code or not date:
                continue
            results.append({
                "date": date,
                "symbol": code,
                "turnover_rate": _f(values[1]),
                "volume_ratio": _f(values[2]),
                "pe": _f(values[3]),
                "pe_ttm": _f(values[4]),
                "pb": _f(values[5]),
                "total_mv": _f(values[6]),
                "circ_mv": _f(values[7]),
            })
        results.sort(key=lambda r: (r["date"], r["symbol"]))
        return results

    _DAILY_BASIC_FIELDS = (
        "turnover_rate,volume_ratio,pe,pe_ttm,pb,total_mv,circ_mv"
    )

    def get_daily_basic(
        self, symbol: str, start_date: str, end_date: str,
    ) -> list[dict[str, Any]]:
        """按标的获取每日基本面指标（tushare daily_basic）。

        限频 1 次/分钟，仅适合回填/单票场景；nightly 全池增量请用
        get_daily_basic_by_date（单次调用覆盖全市场）。
        """
        ts_code = self._to_ts_code(symbol)
        raw = self._api("daily_basic", {
            "ts_code": ts_code,
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
        }, fields=f"trade_date,{self._DAILY_BASIC_FIELDS}")
        self._cache_raw("daily_basic", {"ts_code": ts_code}, raw)
        return self._parse_daily_basic(raw, ts_code.split(".")[0])

    def get_daily_basic_by_date(self, trade_date: str) -> list[dict[str, Any]]:
        """按交易日全市场获取 daily_basic（单次调用 ~5000 只）。

        120 积分 daily_basic 限频 1 次/分钟：nightly 增量必须走此口径，
        逐票循环会打满限频。返回字典列表，键同 get_daily_basic。
        """
        raw = self._api("daily_basic", {
            "trade_date": trade_date.replace("-", ""),
        }, fields=f"ts_code,trade_date,{self._DAILY_BASIC_FIELDS}")
        self._cache_raw("daily_basic", {"trade_date": trade_date}, raw)
        return self._parse_daily_basic(raw, "")

    def get_financial_data(
        self, symbol: str, report_types: list[str] | None = None,
    ) -> list[FinancialData]:
        """获取财务数据。

        注意：120 积分禁止 income/balancesheet/cashflow/fina_indicator，
        因此此方法在实际的 120 积分方案下无法获取数据。
        返回空列表，等待积分升级后开启。
        """
        return []

    def health_check(self) -> ProviderHealth:
        """健康检查：调用 stock_basic 验证 API 可用性。"""
        try:
            start = time.time()
            raw = self._api("stock_basic", {"list_status": "L"}, fields="ts_code")
            elapsed = time.time() - start
            count = len(raw)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return ProviderHealth(
                provider_name=self.name,
                status="healthy",
                last_success=self._last_success or today,
                error_count=self._error_count,
                data_latency_days=1,
                message=f"API 正常, {count} 只股票, 响应 {elapsed:.1f}s",
                api_available=True,
            )
        except Exception as e:
            return ProviderHealth(
                provider_name=self.name,
                status="error",
                error_count=self._error_count + 1,
                data_latency_days=99,
                message=str(e),
                api_available=False,
            )

    # ── 帮助方法 ──────────────────────────────────────

    @staticmethod
    def _iso_date(value) -> str:
        """将 Tushare API 返回的 YYYYMMDD 格式转为 YYYY-MM-DD。

        8 位纯数字字符串转 YYYY-MM-DD；已是 YYYY-MM-DD 格式的原样返回；
        空值返回空字符串；无法识别的格式原样返回 str() 结果。
        """
        if not value:
            return ""
        s = str(value).strip()
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if len(s) == 10 and s[4] == '-' and s[7] == '-':
            return s
        return s

    @staticmethod
    def _to_ts_code(symbol: str) -> str:
        """转换 symbol 为 Tushare ts_code 格式。"""
        if "." in symbol:
            return symbol
        if symbol.startswith(("60", "68", "90")):
            return f"{symbol}.SH"
        elif symbol.startswith(("00", "30", "15")):
            return f"{symbol}.SZ"
        elif symbol.startswith(("4", "8", "92")):
            return f"{symbol}.BJ"
        return f"{symbol}.SH"

    @staticmethod
    def incremental_date(existing_prices: list[DailyPrice]) -> str:
        """计算增量更新起始日期（最新数据的后一天）。"""
        if not existing_prices:
            return (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        last = max(p.date for p in existing_prices)
        return (datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
