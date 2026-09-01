"""
数据验证器 — 验证每个 Provider 返回的数据质量。

检查项：
    1. 价格异常（单日涨跌超过30%）
    2. 数据缺失（连续5天无数据）
    3. 重复数据
    4. 未来日期数据
    5. 成交量异常
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .base import DailyPrice, ProviderHealth


class DataValidationError(Exception):
    """数据验证失败。"""
    pass


@dataclass
class ValidationResult:
    """验证结果。"""
    provider_name: str
    total_checks: int = 0
    passed: int = 0
    failed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "healthy"  # 'healthy', 'warning', 'error'

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)
        self.failed += 1
        if self.status == "healthy":
            self.status = "warning"

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.failed += 1
        self.status = "error"

    def passed_check(self) -> None:
        self.passed += 1

    @property
    def total(self) -> int:
        return self.total_checks or (self.passed + self.failed)


class DataValidator:
    """数据验证器。

    使用方式:
        validator = DataValidator()
        result = validator.validate_daily_prices(prices, "tushare")
    """

    MAX_DAILY_CHANGE = 0.30        # 单日涨跌幅上限 30%
    MAX_MISSING_DAYS = 5           # 连续缺失天数上限
    FUTURE_GRACE_DAYS = 1          # "未来数据" 容忍天数

    # ── 日线验证 ──────────────────────────────────────

    def validate_daily_prices(
        self, prices: list[DailyPrice], provider: str,
    ) -> ValidationResult:
        result = ValidationResult(provider_name=provider)

        if not prices:
            result.add_error("日线数据为空")
            return result

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. 价格异常检查
        for i in range(1, len(prices)):
            result.total_checks += 1
            prev_close = prices[i - 1].close
            curr_close = prices[i].close
            if prev_close <= 0:
                continue
            change = abs(curr_close - prev_close) / prev_close
            if change > self.MAX_DAILY_CHANGE:
                result.add_warning(
                    f"{prices[i].symbol} {prices[i].date} "
                    f"单日涨跌幅 {change:.1%} 超过 {self.MAX_DAILY_CHANGE:.0%}"
                )

        # 2. 未来日期检查
        for p in prices:
            result.total_checks += 1
            if p.date > today:
                result.add_error(f"未来日期数据: {p.symbol} {p.date} > {today}")

        # 3. 重复日期检查
        dates = [p.date for p in prices]
        if len(dates) != len(set(dates)):
            dupes = {d for d in dates if dates.count(d) > 1}
            for d in sorted(dupes):
                result.add_warning(f"重复日期 {d}: {prices[0].symbol}")

        # 4. 价格负值检查
        for p in prices:
            result.total_checks += 1
            if p.close <= 0:
                result.add_error(f"价格为负/零: {p.symbol} {p.date} close={p.close}")
            if p.high < p.low:
                result.add_warning(f"最高价<最低价: {p.symbol} {p.date}")

        # 5. 成交量异常
        volumes = [p.volume for p in prices if p.volume > 0]
        if len(volumes) >= 10:
            mean_vol = sum(volumes) / len(volumes)
            std_vol = (sum((v - mean_vol) ** 2 for v in volumes) / len(volumes)) ** 0.5
            for p in prices:
                result.total_checks += 1
                if p.volume > mean_vol + 5 * std_vol and std_vol > 0:
                    result.add_warning(f"成交量异常: {p.symbol} {p.date} vol={p.volume:.0f}")

        # 6. 连续缺失检查（如果传入的日期不连续）
        if len(prices) >= 2:
            result.total_checks += 1
            gaps = self._count_missing_days(prices)
            if gaps > self.MAX_MISSING_DAYS:
                result.add_warning(
                    f"连续缺失 {gaps} 天 > {self.MAX_MISSING_DAYS} (最近30天)"
                )

        # 7. OHLC 关系检查
        for p in prices:
            result.total_checks += 1
            if not (p.low <= p.close <= p.high and p.low <= p.open <= p.high):
                result.add_warning(f"OHLC 逻辑异常: {p.symbol} {p.date}")

        return result

    # ── 健康检查结果转换 ─────────────────────────────

    def validate_health(self, health: ProviderHealth) -> ValidationResult:
        """将 ProviderHealth 转换为 ValidationResult。"""
        result = ValidationResult(provider_name=health.provider_name)
        if health.status == "error":
            result.add_error(health.message)
        elif health.status == "degraded":
            result.add_warning(health.message)
        else:
            result.passed_check()
        return result

    # ── 工具方法 ──────────────────────────────────────

    @staticmethod
    def _count_missing_days(prices: list[DailyPrice]) -> int:
        """统计最近的连续缺失天数。"""
        if len(prices) < 2:
            return 0
        from datetime import datetime, timedelta
        max_gap = 0
        current_gap = 0
        for i in range(len(prices) - 1, 0, -1):
            try:
                d1 = datetime.strptime(prices[i].date, "%Y-%m-%d")
                d2 = datetime.strptime(prices[i - 1].date, "%Y-%m-%d")
                gap = (d1 - d2).days - 1
                if gap > 0:
                    current_gap += gap
                else:
                    current_gap = 0
                max_gap = max(max_gap, current_gap)
            except (ValueError, IndexError):
                continue
            if len(prices) - i > 30:
                break
        return max_gap

    @staticmethod
    def format_report(results: list[ValidationResult]) -> str:
        """生成可读的报告。"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# 数据质量报告 — {now}",
            f"",
        ]
        for r in results:
            emoji = {"healthy": "✅", "warning": "⚠️", "error": "❌"}
            icon = emoji.get(r.status, "❓")
            lines.append(f"## {icon} {r.provider_name}")
            lines.append(f"- 状态: {r.status.upper()}")
            lines.append(f"- 检查: {r.total} 项 (通过 {r.passed}, 失败 {r.failed})")
            if r.warnings:
                lines.append("- 警告:")
                for w in r.warnings[:5]:
                    lines.append(f"  - {w}")
            if r.errors:
                lines.append("- 错误:")
                for e in r.errors[:5]:
                    lines.append(f"  - {e}")
            lines.append("")
        return "\n".join(lines)


# 需要 dataclass
from dataclasses import dataclass, field  # noqa: E402
