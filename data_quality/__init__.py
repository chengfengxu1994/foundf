"""
data_quality — 数据质量系统。

每日运行，输出 reports/data_health/data_health_YYYY-MM-DD.md

检查项：
    price_check.py     — 价格异常（单日涨跌 > 30%）
    missing_check.py   — 数据缺失（连续 N 天无数据）
    duplicate_check.py — 重复数据
    stale_check.py     — 数据时效性检查
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from foundf_db import Warehouse


@dataclass
class DataHealthItem:
    """单条数据健康检查结果。"""
    symbol: str
    name: str = ""
    check_type: str = ""         # 'price_anomaly', 'missing', 'duplicate', 'stale'
    severity: str = "warning"    # 'info', 'warning', 'error'
    message: str = ""


@dataclass
class DataHealthReport:
    """完整数据健康报告。"""
    date: str
    provider: str = ""
    total_symbols: int = 0
    coverage_pct: float = 0.0     # 数据覆盖率
    missing_pct: float = 0.0      # 缺失率
    anomalies: list[DataHealthItem] = field(default_factory=list)
    stale_symbols: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    status: str = "healthy"       # 'healthy', 'warning', 'error'


class DataQualityChecker:
    """数据质量检查器。

    使用方式:
        checker = DataQualityChecker("data/finance.duckdb")
        report = checker.check_all()
        checker.save_report(report)
    """

    MAX_DAILY_CHANGE = 0.30
    MAX_STALE_DAYS = 7
    COVERAGE_THRESHOLD = 0.80

    def __init__(self, duckdb_path: str | Path = "data/finance.duckdb",
                 report_dir: str | Path = "reports/data_health"):
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── 检查入口 ──────────────────────────────────────

    def check_all(self) -> DataHealthReport:
        """执行所有检查，返回聚合报告。"""
        report = DataHealthReport(
            date=self.today,
            provider="Tushare",
        )

        # 1. 获取所有有日线数据的标的
        symbols = self.warehouse.query(
            "SELECT DISTINCT symbol FROM daily_price ORDER BY symbol"
        )
        report.total_symbols = len(symbols)
        sym_list = [r["symbol"] for r in symbols]

        # 2. 价格异常检查
        anomalies = self._check_price_anomalies(sym_list)
        report.anomalies.extend(anomalies)

        # 3. 数据缺失检查
        missing = self._check_missing(sym_list)
        report.anomalies.extend(missing)
        if missing:
            report.missing_pct = len(missing) / max(len(sym_list), 1)

        # 4. 重复数据检查
        duplicates = self._check_duplicates(sym_list)
        report.duplicates = duplicates

        # 5. 时效性检查
        stale = self._check_stale(sym_list)
        report.stale_symbols = stale

        # 6. 覆盖率计算
        if sym_list:
            total_expected = 252  # 约一年的交易日
            actual = self.warehouse.query(
                "SELECT COUNT(*) AS cnt FROM daily_price"
            )
            if actual:
                max_possible = len(sym_list) * total_expected
                report.coverage_pct = min(1.0, actual[0]["cnt"] / max(max_possible, 1))

        # 7. 总体状态
        errors = [a for a in report.anomalies if a.severity == "error"]
        warnings = [a for a in report.anomalies if a.severity == "warning"]
        if errors:
            report.status = "error"
        elif warnings or report.stale_symbols:
            report.status = "warning"

        return report

    # ── 各个检查 ──────────────────────────────────────

    def _check_price_anomalies(self, symbols: list[str]) -> list[DataHealthItem]:
        """检查价格异常（单日涨跌超过 MAX_DAILY_CHANGE）。"""
        items: list[DataHealthItem] = []
        for sym in symbols:  # 全量检查（当前股票池 ~95 只，无性能问题）
            rows = self.warehouse.query(
                "SELECT date, close FROM daily_price WHERE symbol = ? "
                "ORDER BY date DESC LIMIT 252",
                [sym],
            )
            if len(rows) < 2:
                continue
            closes = np.array([r["close"] for r in rows], dtype=float)
            changes = np.abs(np.diff(closes) / closes[:-1])
            for i, change in enumerate(changes):
                if change > self.MAX_DAILY_CHANGE:
                    items.append(DataHealthItem(
                        symbol=sym,
                        check_type="price_anomaly",
                        severity="warning",
                        message=f"{rows[i+1]['date']} 涨跌幅 {change:.1%} > {self.MAX_DAILY_CHANGE:.0%}",
                    ))
        return items

    def _check_missing(self, symbols: list[str]) -> list[DataHealthItem]:
        """检查数据缺失（最近30天内有连续5天缺失）。"""
        items: list[DataHealthItem] = []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        for sym in symbols:
            rows = self.warehouse.query(
                "SELECT date FROM daily_price WHERE symbol = ? "
                "AND date >= ? AND date <= ? ORDER BY date",
                [sym, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
            )
            if len(rows) < 2:
                if rows:
                    continue  # 有少量数据不算缺失
                items.append(DataHealthItem(
                    symbol=sym,
                    check_type="missing",
                    severity="error",
                    message="最近30天无数据",
                ))
                continue
            # 检查连续间隔（DuckDB DATE 列返回 datetime.date，直接做日期算术）
            max_gap = 0
            for i in range(1, len(rows)):
                d1 = self._as_date(rows[i - 1]["date"])
                d2 = self._as_date(rows[i]["date"])
                if d1 is None or d2 is None:
                    continue
                gap = (d2 - d1).days - 1
                if gap > max_gap:
                    max_gap = gap
            if max_gap >= 5:
                items.append(DataHealthItem(
                    symbol=sym,
                    check_type="missing",
                    severity="warning",
                    message=f"最大连续缺失 {max_gap} 天",
                ))
        return items

    def _check_duplicates(self, symbols: list[str]) -> list[str]:
        """检查重复数据（全量 symbol）。"""
        duplicates: list[str] = []
        for sym in symbols:
            rows = self.warehouse.query(
                "SELECT date, COUNT(*) AS cnt FROM daily_price "
                "WHERE symbol = ? GROUP BY date HAVING cnt > 1",
                [sym],
            )
            if rows:
                duplicates.append(f"{sym}: {len(rows)} 个重复日期")
        return duplicates

    @staticmethod
    def _as_date(value: Any) -> date | None:
        """将 DuckDB DATE 列返回值（date/datetime/str）统一为 date。"""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def _check_stale(self, symbols: list[str]) -> list[str]:
        """检查数据时效性（最新数据距今 MAX_STALE_DAYS 天以上）。"""
        stale: list[str] = []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.MAX_STALE_DAYS)).strftime("%Y-%m-%d")
        for sym in symbols:
            rows = self.warehouse.query(
                "SELECT MAX(date) AS last_date FROM daily_price WHERE symbol = ?",
                [sym],
            )
            if rows and rows[0]["last_date"]:
                last = rows[0]["last_date"]
                if hasattr(last, "strftime"):
                    last_str = last.strftime("%Y-%m-%d")
                else:
                    last_str = str(last)[:10]
                if last_str < cutoff:
                    stale.append(sym)
        return stale

    # ── 报告输出 ──────────────────────────────────────

    def to_markdown(self, report: DataHealthReport) -> str:
        """生成 Markdown 格式的健康报告。"""
        emoji = {"healthy": "✅", "warning": "⚠️", "error": "❌"}
        lines = [
            f"# 数据健康报告 — {report.date}",
            f"",
            f"**数据源:** {report.provider}",
            f"**状态:** {emoji.get(report.status, '❓')} {report.status.upper()}",
            f"",
            f"## 概览",
            f"- 覆盖标的: {report.total_symbols} 只",
            f"- 数据覆盖率: {report.coverage_pct:.1%}",
            f"- 缺失率: {report.missing_pct:.1%}",
            f"- 过期标的: {len(report.stale_symbols)} 只",
            f"- 重复数据: {len(report.duplicates)} 处",
            f"",
        ]
        if report.anomalies:
            lines.append(f"## 异常明细")
            errors = [a for a in report.anomalies if a.severity == "error"]
            warnings = [a for a in report.anomalies if a.severity == "warning"]
            if errors:
                lines.append(f"### 错误")
                for a in errors[:10]:
                    lines.append(f"- ❌ {a.symbol}: {a.message}")
            if warnings:
                lines.append(f"### 警告")
                for a in warnings[:10]:
                    lines.append(f"- ⚠ {a.symbol}: {a.message}")
        if report.stale_symbols:
            lines.append(f"### 过期数据 ({len(report.stale_symbols)})")
            for s in report.stale_symbols[:10]:
                lines.append(f"- {s}")
        if report.duplicates:
            lines.append(f"### 重复 ({len(report.duplicates)})")
            for d in report.duplicates[:5]:
                lines.append(f"- {d}")
        lines.extend([
            f"",
            f"---",
            f"_由 FoundF DataQuality 自动生成_",
        ])
        return "\n".join(lines)

    def save_report(self, report: DataHealthReport) -> Path:
        """保存健康报告。"""
        md = self.to_markdown(report)
        path = self.report_dir / f"data_health_{report.date}.md"
        path.write_text(md, encoding="utf-8")
        return path

    def check_and_save(self) -> Path:
        """执行所有检查并保存报告。"""
        report = self.check_all()
        return self.save_report(report)
