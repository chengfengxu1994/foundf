"""
factor_engine — 独立因子引擎。

每个因子是一个独立的类，包含：
    - name         因子名称
    - description  因子描述
    - formula      计算公式/逻辑
    - calculate()  计算因子值（返回横截面百分位）
    - validate()   验证计算结果

因子列表：
    Value:   PE, PB, EV/EBITDA, FCF Yield
    Quality: ROE, ROIC, Gross Margin, Cashflow Quality
    Growth:  Revenue Growth, Profit Growth, EPS Growth
    Momentum: 3M Return, 6M Return, 12M Return
    Risk:    Volatility, Max Drawdown, Beta

所有因子值保存到 DuckDB factor_daily 表：
    date, symbol, factor_name, value, source

口径说明（2026-08-13 起）：每个因子类带 ``source`` 属性——
``real`` 表示名实相符（动量/风险类本就以价格计算）；``proxy``
表示名义上是基本面因子（PE/ROE 等）但实际用价格/成交量代理计算，
写入 factor_daily 时随值落库，供下游区分真值与代理
（对标 quant_strategy 的 value_source 诊断口径）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from foundf_db import Warehouse


# ── 因子注册表 ──────────────────────────────────────

_FACTOR_REGISTRY: dict[str, type["Factor"]] = {}


def register_factor(cls: type["Factor"]) -> type["Factor"]:
    """注册因子类到全局注册表。"""
    _FACTOR_REGISTRY[cls.__name__] = cls
    return cls


def get_factor(name: str) -> type["Factor"] | None:
    return _FACTOR_REGISTRY.get(name)


def list_factors() -> list[str]:
    return list(_FACTOR_REGISTRY.keys())


# ── 抽象基类 ───────────────────────────────────────

class Factor(ABC):
    """因子抽象基类。"""

    name: str = ""
    description: str = ""
    formula: str = ""
    # 口径来源：'real' = 名实相符；'proxy' = 基本面因子实为价格/成交量代理
    source: str = "real"

    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse

    @abstractmethod
    def calculate(self, symbol: str) -> float | None:
        """计算单个标的的因子值，返回 0-1 之间的标准分。"""
        ...

    def validate(self, symbol: str, value: float | None) -> bool:
        """验证计算结果。"""
        if value is None:
            return False
        return 0.0 <= value <= 1.0

    def calculate_batch(self, symbols: list[str]) -> dict[str, float | None]:
        """批量计算。"""
        return {s: self.calculate(s) for s in symbols}

    def save(self, symbol: str, value: float | None, date_str: str | None = None) -> None:
        """保存到 factor_daily 表（含口径来源 source）。"""
        if value is None:
            return
        date = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.warehouse.execute(
            "INSERT OR REPLACE INTO factor_daily (date, symbol, factor_name, value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [date, symbol, self.name, float(value), self.source],
        )

    def load_history(self, symbol: str, limit: int = 252) -> list[dict[str, Any]]:
        """加载历史因子值。"""
        return self.warehouse.query(
            "SELECT date, value FROM factor_daily "
            "WHERE symbol = ? AND factor_name = ? "
            "ORDER BY date DESC LIMIT ?",
            [symbol, self.name, limit],
        )


# ═══════════════════════════════════════════════════════════
# 价值因子
# ═══════════════════════════════════════════════════════════

@register_factor
class ValuePE(Factor):
    name = "value_pe"
    description = "市盈率估值因子（价格/60日均线代理口径，非真实 E/P）"
    formula = "代理：价格相对 60 日均线位置，低于均线 = 高分；待接真实估值数据"
    source = "proxy"

    def calculate(self, symbol: str) -> float | None:
        bars = self.warehouse.query(
            "SELECT date, close FROM daily_price WHERE symbol = ? "
            "ORDER BY date DESC LIMIT 1",
            [symbol],
        )
        if not bars:
            return None
        price = bars[0]["close"]
        if price <= 0:
            return None
        # 用价格/60日均线比率作为估值代理（proxy 口径）
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 60",
            [symbol],
        )
        if len(rows) < 60:
            return 0.5
        ma60 = np.mean([r["close"] for r in rows])
        ratio = ma60 / price if price > 0 else 1.0
        # ratio < 1 = 价格高于均线 = 高估
        score = 1.0 - min(1.0, max(0.0, (ratio - 0.5) / 1.5))
        return float(score)


@register_factor
class ValuePB(Factor):
    name = "value_pb"
    description = "市净率因子（价格/252日均线代理口径，非真实 B/P）"
    formula = "代理：价格相对 252 日均线位置"
    source = "proxy"

    def calculate(self, symbol: str) -> float | None:
        return self._price_to_ma_ratio(symbol)

    def _price_to_ma_ratio(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 252",
            [symbol],
        )
        if not rows:
            return None  # 无数据不落库（区别于「历史不足 → 中性 0.5」）
        if len(rows) < 252:
            return 0.5
        prices = np.array([r["close"] for r in rows], dtype=float)
        ma252 = np.mean(prices)
        last = prices[0]
        ratio = ma252 / last if last > 0 else 1.0
        return float(min(1.0, max(0.0, (ratio - 0.3) / 2.0)))


@register_factor
class ValueFCFYield(Factor):
    name = "value_fcf_yield"
    description = "自由现金流收益率（价格稳定性代理口径，非真实 FCF/MarketCap）"
    formula = "代理：价格稳定性 + 趋势质量"
    source = "proxy"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 126",
            [symbol],
        )
        if len(rows) < 126:
            return None
        prices = np.array([r["close"] for r in rows], dtype=float)
        # 用价格稳定性作为 FCF 质量的代理
        returns = np.diff(prices) / prices[:-1]
        stability = 1.0 - min(1.0, float(np.std(returns) * 10))
        trend = 1.0 - min(1.0, abs(prices[-1] / np.mean(prices) - 1) * 2)
        return float((stability + trend) / 2)


# ═══════════════════════════════════════════════════════════
# 质量因子
# ═══════════════════════════════════════════════════════════

@register_factor
class QualityROE(Factor):
    name = "quality_roe"
    description = "净资产收益率代理（价格收益稳定性口径，非真实 ROE）"
    formula = "ROE ≈ 收益稳定性 + 趋势质量"
    source = "proxy"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 252",
            [symbol],
        )
        if len(rows) < 252:
            return None
        prices = np.array([r["close"] for r in rows], dtype=float)
        returns = np.diff(prices) / prices[:-1]
        # 月度正收益比例
        monthly = []
        for i in range(0, len(returns), 21):
            chunk = returns[i:i + 21]
            if len(chunk) > 0:
                monthly.append(float(np.prod(1 + chunk) - 1))
        pos_ratio = np.mean(np.array(monthly) > 0) if monthly else 0.5
        # 最大回撤
        peak = np.maximum.accumulate(prices)
        dd = (prices / peak - 1).min()
        dd_score = 1.0 - min(1.0, abs(dd) * 2)
        return float((pos_ratio * 0.6 + dd_score * 0.4))


@register_factor
class QualityCashflow(Factor):
    name = "quality_cashflow"
    description = "现金流质量（成交量稳定性代理口径，非真实现金流数据）"
    formula = "代理：成交量稳定性"
    source = "proxy"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close, volume FROM daily_price WHERE symbol = ? "
            "ORDER BY date DESC LIMIT 126",
            [symbol],
        )
        if len(rows) < 63:
            return None
        volumes = np.array([r["volume"] for r in rows], dtype=float)
        if np.mean(volumes) <= 0:
            return 0.5
        vol_stability = 1.0 - min(1.0, float(np.std(volumes) / np.mean(volumes)))
        return float(max(0.0, vol_stability))


# ═══════════════════════════════════════════════════════════
# 成长因子
# ═══════════════════════════════════════════════════════════

@register_factor
class GrowthRevenue(Factor):
    name = "growth_revenue"
    description = "收入增长率代理（价格上涨口径，非真实收入增速）"
    formula = "代理：长期价格上涨 ≈ 收入增长"
    source = "proxy"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 252",
            [symbol],
        )
        if len(rows) < 252:
            return None
        prices = np.array([r["close"] for r in rows], dtype=float)
        ret_12m = prices[0] / prices[-1] - 1
        ret_6m = prices[0] / prices[min(len(prices) - 1, 125)] - 1
        score = max(0, ret_12m) * 0.5 + max(0, ret_6m) * 0.5
        return float(min(1.0, score * 2))


@register_factor
class GrowthMomentum(Factor):
    name = "growth_momentum"
    description = "动量成长"
    formula = "3M, 6M, 12M 收益加权"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 252",
            [symbol],
        )
        if len(rows) < 63:
            return None
        prices = np.array([r["close"] for r in rows], dtype=float)
        r3 = prices[0] / prices[min(62, len(prices) - 1)] - 1 if len(prices) >= 63 else 0
        r6 = prices[0] / prices[min(125, len(prices) - 1)] - 1 if len(prices) >= 126 else 0
        r12 = prices[0] / prices[-1] - 1 if len(prices) >= 252 else 0
        score = max(0, r3) * 0.4 + max(0, r6) * 0.35 + max(0, r12) * 0.25
        return float(min(1.0, score * 2))


# ═══════════════════════════════════════════════════════════
# 动量因子
# ═══════════════════════════════════════════════════════════

@register_factor
class Momentum3M(Factor):
    name = "momentum_3m"
    description = "3个月动量"
    formula = "r_63d"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 63",
            [symbol],
        )
        if len(rows) < 63:
            return None
        ret = rows[0]["close"] / rows[-1]["close"] - 1
        return float(min(1.0, max(0.0, (ret + 0.3) / 0.6)))


@register_factor
class Momentum6M(Factor):
    name = "momentum_6m"
    description = "6个月动量"
    formula = "r_126d"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 126",
            [symbol],
        )
        if len(rows) < 126:
            return None
        ret = rows[0]["close"] / rows[-1]["close"] - 1
        return float(min(1.0, max(0.0, (ret + 0.3) / 0.6)))


# ═══════════════════════════════════════════════════════════
# 风险因子
# ═══════════════════════════════════════════════════════════

@register_factor
class RiskVolatility(Factor):
    name = "risk_volatility"
    description = "波动率（越低分越高）"
    formula = "σ_63d * sqrt(252)"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 63",
            [symbol],
        )
        if len(rows) < 63:
            return None
        prices = np.array([r["close"] for r in rows], dtype=float)
        returns = np.diff(prices) / prices[:-1]
        vol = float(np.std(returns) * np.sqrt(252))
        # 低波动 = 高分
        return float(max(0.0, min(1.0, 1.0 - vol * 2)))


@register_factor
class RiskMaxDrawdown(Factor):
    name = "risk_max_drawdown"
    description = "最大回撤（回撤小 = 高分）"
    formula = "MDD_252d"

    def calculate(self, symbol: str) -> float | None:
        rows = self.warehouse.query(
            "SELECT close FROM daily_price WHERE symbol = ? ORDER BY date DESC LIMIT 252",
            [symbol],
        )
        if len(rows) < 126:
            return None
        prices = np.array([r["close"] for r in rows], dtype=float)[::-1]
        peak = np.maximum.accumulate(prices)
        dd = (prices / peak - 1).min()
        return float(max(0.0, min(1.0, 1.0 - abs(dd) * 3)))


# ── 因子注册表管理 ──────────────────────────────────

class FactorRegistry:
    """因子注册表 — 管理的因子实例。"""

    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse
        # 确保 factor_daily 表存在（source 列记录口径来源 real/proxy）
        warehouse.execute("""
            CREATE TABLE IF NOT EXISTS factor_daily (
                date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                factor_name VARCHAR NOT NULL,
                value DOUBLE NOT NULL,
                source VARCHAR,
                UNIQUE (date, symbol, factor_name)
            )
        """)
        # 旧库缺 source 列时补齐（CREATE TABLE IF NOT EXISTS 不会改已有表；
        # 历史行 source 保持 NULL = 口径未知，不回填冒充）
        cols = {r["column_name"] for r in warehouse.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'factor_daily' AND table_schema = 'main'")}
        if "source" not in cols:
            warehouse.execute("ALTER TABLE factor_daily ADD COLUMN source VARCHAR")
        self._factors: dict[str, Factor] = {}
        for name, cls in _FACTOR_REGISTRY.items():
            self._factors[name] = cls(warehouse)
        # 口径诊断：最近一次 compute_all 的逐因子来源 + 累计计数
        self._last_sources: dict[str, dict[str, str]] = {}
        self._source_stats: dict[str, int] = {"real": 0, "proxy": 0}

    def list_factors(self) -> list[str]:
        return list(self._factors.keys())

    def get_factor(self, name: str) -> Factor | None:
        return self._factors.get(name)

    def factor_sources(self) -> dict[str, str]:
        """各因子的口径来源（'real'/'proxy'）。

        键为 ``factor.name``（如 value_pe），与 factor_daily 落库口径一致，
        供下游区分真值与代理。注意 ``compute_all`` 的返回键是类名
        （ValuePE），下游需经 ``get_factor(cls_name).name`` 换算。
        """
        return {f.name: f.source for f in self._factors.values()}

    def last_sources(self, symbol: str) -> dict[str, str]:
        """最近一次 compute_all(symbol) 实际产出因子的口径来源。"""
        return dict(self._last_sources.get(symbol, {}))

    @property
    def source_stats(self) -> dict[str, int]:
        """累计口径诊断计数（真实 vs 代理），对标 quant_strategy 的 value_source。"""
        return dict(self._source_stats)

    def compute_all(self, symbol: str) -> dict[str, float]:
        """计算所有因子，返回 {因子名: 值}。"""
        results = {}
        sources: dict[str, str] = {}
        for name, factor in self._factors.items():
            try:
                value = factor.calculate(symbol)
                if value is not None and factor.validate(symbol, value):
                    results[name] = value
                    sources[name] = factor.source
                    self._source_stats[factor.source] = (
                        self._source_stats.get(factor.source, 0) + 1
                    )
            except Exception:
                continue
        self._last_sources[symbol] = sources
        return results

    def compute_and_save(self, symbol: str, date_str: str | None = None) -> dict[str, float]:
        """计算并保存所有因子值（含口径来源 source）。

        factor_name 落 ``factor.name``（如 value_pe），与 ``Factor.save`` /
        ``load_history`` 口径一致；此前误用类名（ValuePE），导致
        ``load_history`` 永远查不到本方法写入的行。
        """
        results = self.compute_all(symbol)
        date = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for cls_name, value in results.items():
            factor = self._factors[cls_name]
            self.warehouse.execute(
                "INSERT OR REPLACE INTO factor_daily (date, symbol, factor_name, value, source) "
                "VALUES (?, ?, ?, ?, ?)",
                [date, symbol, factor.name, float(value), factor.source],
            )
        return results

    def compute_batch(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        """批量计算所有标的的所有因子。"""
        results = {}
        for sym in symbols:
            results[sym] = self.compute_all(sym)
        return results
