"""可追溯的逐持仓估值与多源回退。

本模块只负责选择和标记估值证据，不负责下单。任何来源都必须返回带时间戳、
币种和状态的观测；缺失值保持 ``None``，禁止用 0 冒充价格。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable, Iterable


class FreshnessLevel(str, Enum):
    REALTIME = "REALTIME"
    T_CLOSE = "T_CLOSE"
    VALUATION_MISSING = "VALUATION_MISSING"


class InstrumentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    NAV_PENDING = "NAV_PENDING"


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    price: float | None
    currency: str
    price_date: str | None
    observed_at: str | None
    source: str
    realtime: bool = False
    instrument_status: str = InstrumentStatus.ACTIVE.value

    def valid(self) -> bool:
        return (
            self.price is not None
            and not isinstance(self.price, bool)
            and float(self.price) > 0
            and bool(self.currency)
            and bool(self.price_date)
            and bool(self.source)
        )


QuoteFetcher = Callable[[str], QuoteObservation | None]


def _iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def resolve_valuation(
    symbol: str,
    primary: QuoteFetcher,
    backups: Iterable[QuoteFetcher] = (),
    snapshot: QuoteFetcher | None = None,
    *,
    latest_trading_date: date | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """按主源、备用源、上次快照顺序选择第一条有效观测。

    ``latest_trading_date`` 由市场交易日历提供；不传时只做保守日期判断。
    """

    now = now or datetime.now(timezone.utc)
    attempts: list[dict[str, object]] = []
    chain = [("PRIMARY", primary)]
    chain.extend(("BACKUP", fetcher) for fetcher in backups)
    if snapshot is not None:
        chain.append(("SNAPSHOT", snapshot))

    selected: QuoteObservation | None = None
    selected_tier: str | None = None
    for tier, fetcher in chain:
        try:
            observation = fetcher(symbol)
            valid = observation is not None and observation.valid()
            attempts.append(
                {
                    "tier": tier,
                    "source": observation.source if observation else None,
                    "valid": valid,
                    "reason": None if valid else "EMPTY_OR_INVALID_QUOTE",
                }
            )
            if valid:
                selected = observation
                selected_tier = tier
                break
        except Exception as exc:  # 数据源异常必须降级并留痕
            attempts.append(
                {
                    "tier": tier,
                    "source": getattr(fetcher, "__name__", "unknown"),
                    "valid": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    if selected is None:
        return {
            "symbol": symbol,
            "price": None,
            "currency": None,
            "price_date": None,
            "observed_at": None,
            "source": None,
            "source_tier": None,
            "freshness": FreshnessLevel.VALUATION_MISSING.value,
            "stale": True,
            "instrument_status": InstrumentStatus.ACTIVE.value,
            "degraded": True,
            "degradation_reason": "ALL_QUOTE_SOURCES_FAILED",
            "attempts": attempts,
        }

    price_day = _iso_date(selected.price_date)
    status = str(selected.instrument_status).upper()
    realtime = (
        selected.realtime
        and selected_tier != "SNAPSHOT"
        and status == InstrumentStatus.ACTIVE.value
    )
    freshness = (
        FreshnessLevel.REALTIME.value
        if realtime
        else FreshnessLevel.T_CLOSE.value
    )
    stale = bool(
        price_day is None
        or (
            latest_trading_date is not None
            and price_day < latest_trading_date
        )
    )
    reasons: list[str] = []
    if selected_tier != "PRIMARY":
        reasons.append(f"USED_{selected_tier}")
    if stale:
        reasons.append("PRICE_DATE_BEFORE_LATEST_TRADING_DATE")
    if status == InstrumentStatus.SUSPENDED.value:
        reasons.append("SUSPENDED_LAST_CLOSE")
    elif status == InstrumentStatus.NAV_PENDING.value:
        reasons.append("FUND_NAV_NOT_UPDATED")
    elif status == InstrumentStatus.DELISTED.value:
        reasons.append("DELISTED_REFERENCE_ONLY")

    result = asdict(selected)
    result.update(
        {
            "source_tier": selected_tier,
            "freshness": freshness,
            "stale": stale,
            "degraded": bool(reasons),
            "degradation_reason": ",".join(reasons) if reasons else None,
            "attempts": attempts,
            "resolved_at": now.isoformat(),
        }
    )
    return result

