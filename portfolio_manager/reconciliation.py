"""
reconciliation.py — 账户核验引擎。

运行全量核验并生成 reconciliation 报告。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import Warehouse
from .importer import StatementImporter
from .ledger import PortfolioLedger
from .fee_model import FeeModel
from .event_classifier import EventClassifier


class ReconciliationEngine:
    """核验引擎。

    使用方式:
        engine = ReconciliationEngine("data/finance.duckdb")
        result = engine.run()
        engine.generate_reports(result)
    """

    def __init__(self, duckdb_path: str | Path = "data/finance.duckdb",
                 sqlite_path: str | Path = "finance_intel/data/finance_intel.db",
                 report_dir: str | Path = "reports/reconciliation"):
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        self.sqlite_path = Path(sqlite_path)
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.importer = StatementImporter(self.warehouse)
        self.classifier = EventClassifier()
        self.fee_model = FeeModel()
        self.date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def run(self) -> dict[str, Any]:
        """执行完整核验流程。"""
        # 1. 导入原始数据
        import_stats = self.importer.import_from_sqlite(self.sqlite_path)

        # 2. 读取规范化交易
        txns = self.importer.list_transactions(2000)

        # 3. 运行账本
        ledger = PortfolioLedger(self.warehouse)
        ledger_result = ledger.run(txns)

        # 4. 费用统计
        fee_stats = self._fee_statistics(txns)

        # 5. 类别字典（从原始流水）
        raw_categories = []
        for t in txns:
            raw_categories.append(t.get("event_type", ""))
        # 同时从 broker_statement_raw 取原始类别
        raw_rows = self.warehouse.query(
            "SELECT DISTINCT raw_text FROM broker_statement_raw LIMIT 200"
        )
        category_dict = self.classifier.generate_dictionary(
            list(set(raw_categories))
        )

        return {
            "report_date": self.date,
            "import": import_stats,
            "unknown_count": self.importer.count_unknown(),
            "ledger": ledger_result,
            "fee_summary": fee_stats,
            "category_dictionary": category_dict,
            "total_transactions": len(txns),
        }

    def _fee_statistics(self, txns: list[dict[str, Any]]) -> dict[str, float]:
        total_comm = sum(t.get("commission", 0) or 0 for t in txns)
        total_stamp = sum(t.get("stamp_duty", 0) or 0 for t in txns)
        total_transfer = sum(t.get("transfer_fee", 0) or 0 for t in txns)
        total_other = sum(t.get("other_fee", 0) or 0 for t in txns)
        total_fees = sum(t.get("total_fee", 0) or 0 for t in txns)
        return {
            "total_commission": round(total_comm, 2),
            "total_stamp_duty": round(total_stamp, 2),
            "total_transfer_fee": round(total_transfer, 2),
            "total_other_fee": round(total_other, 2),
            "total_fees": round(total_fees, 2),
            "fee_drag_pct": 0.0,  # 需结合总收益计算
        }

    # ── 报告生成 ──────────────────────────────────

    def generate_reports(self, result: dict[str, Any]) -> list[Path]:
        paths = []
        paths.append(self._save_markdown(result))
        paths.append(self._save_category_csv(result))
        return paths

    def _save_markdown(self, result: dict[str, Any]) -> Path:
        r = result
        li = r["ledger"]
        cash = li["cash"]
        wavg = li["positions_weighted_average"]
        fifo = li["positions_fifo"]
        fee = r["fee_summary"]

        lines = [
            f"# 账户核验报告 — {r['report_date']}",
            f"",
            f"## 一、数据导入",
            f"- 原始记录: {r['import']['raw']} 条",
            f"- 规范化交易: {r['import']['normalized']} 条",
            f"- UNKNOWN: {r['unknown_count']} 条",
            f"",
            f"## 二、现金余额回溯",
            f"- 期初余额: {cash['opening_balance']:,.2f}",
            f"- 期末余额: {cash['ending_balance']:,.2f}",
            f"- 闭合率: {cash['pass_rate']:.1%}",
            f"- 最大误差: {cash['max_error']:.2f}",
            f"- 失败行数: {cash['failed_rows']}/{cash['total_rows']}",
        ]
        if cash.get("failed_details"):
            lines.extend(["", "### 现金余额失败明细"])
            for d in cash["failed_details"][:10]:
                lines.append(f"- 第{d['seq']}行 ({d['date']}): 预期{d['expected']}, "
                            f"券商{d['broker']}, 误差{d['error']}")

        lines.extend([
            f"",
            f"## 三、持仓重建（加权平均成本）",
        ])
        for sym, state in wavg.get("final_positions", {}).items():
            lines.append(f"- {sym}: {state['shares']:.0f}股 @ {state['avg_cost']:.4f} = {state['total_cost']:,.2f}")
        if wavg.get("hard_errors"):
            lines.extend(["", "### ⚠ 硬错误（负持仓）"])
            for e in wavg["hard_errors"]:
                lines.append(f"- {e['symbol']} {e['date']}: {e['message']} qty={e['qty_after']}")

        lines.extend([
            f"",
            f"## 四、持仓重建（FIFO）",
        ])
        for sym, state in fifo.get("final_positions", {}).items():
            lines.append(f"- {sym}: {state['shares']:.0f}股 @ {state['avg_cost']:.4f} = {state['total_cost']:,.2f}")

        lines.extend([
            f"",
            f"## 五、费用汇总",
            f"- 佣金: {fee['total_commission']:,.2f}",
            f"- 印花税: {fee['total_stamp_duty']:,.2f}",
            f"- 过户费: {fee['total_transfer_fee']:,.2f}",
            f"- 其他费用: {fee['total_other_fee']:,.2f}",
            f"- 总费用: {fee['total_fees']:,.2f}",
            f"",
            f"## 六、类别映射",
        ])
        for cat in r.get("category_dictionary", []):
            lines.append(f"- {cat['raw_category']:20s} → {cat['normalized_event_type']:20s} "
                        f"(置信度:{cat['confidence']:.0%})")

        lines.extend([
            f"",
            f"---",
            f"_由 FoundF Reconciliation Engine 自动生成_",
            f"_时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        ])

        path = self.report_dir / f"account_reconciliation_{self.date}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _save_category_csv(self, result: dict[str, Any]) -> Path:
        import csv
        path = self.report_dir / "trade_category_dictionary.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "raw_category", "normalized_event_type", "record_count",
                "confidence", "reason", "example_rows",
                "requires_manual_confirmation",
            ])
            writer.writeheader()
            for cat in result.get("category_dictionary", []):
                writer.writerow(cat)
        return path
