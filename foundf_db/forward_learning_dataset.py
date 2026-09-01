"""前向回归训练数据集构建器（fail-closed）。

目标：把策略候选信号与未来的模拟盘规范化成交 join 成回归训练行——
特征为候选信号属性与生成时点上下文引用，标签为执行偏差、费用、毛/净收益
和相对基准收益。

当前所有券商导出的 ``normalization_status`` 都是 ``RAW_ONLY``，没有任何经
人工验收的字段映射，也没有规范化成交表。因此公开入口
:func:`build_forward_learning_dataset` 默认 fail-closed：返回
``PENDING_REAL_SAMPLE`` 并明确列出缺失项，不生成任何训练行。缺失费用
一律标记 ``MISSING_FEE``，绝不补 0。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .event_store import resolve_data_root
from .warehouse import Warehouse


DATASET_SCHEMA = "foundf.forward_learning_dataset.v1"
MISSING_FEE = "MISSING_FEE"
NORMALIZED_FILL_TABLE = "broker_sim_normalized_fill"

# 方向约定的执行偏差符号：正值表示执行比决策时点更差。
_ADVERSE_SIGN = {"BUY": 1.0, "SELL": -1.0, "HOLD_REDUCE": -1.0}
# 方向约定的持有收益符号：BUY 做多、SELL/HOLD_REDUCE 按减持口径。
_RETURN_SIGN = {"BUY": 1.0, "SELL": -1.0, "HOLD_REDUCE": -1.0}


def _parse_timestamp(value: Any) -> datetime | None:
    """解析必须含时区的时间戳；缺失或无时区返回 None（由调用方拒绝）。"""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_rows(
    candidates: Iterable[Mapping[str, Any]],
    fills: Iterable[Mapping[str, Any]],
    *,
    evaluation_prices: Mapping[str, float] | None = None,
    benchmark_returns: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """纯函数：候选信号 × 规范化成交 → 回归训练行。

    - ``fill_time`` 早于候选 ``generated_at`` 的行拒绝并计数（防未来数据穿越）。
    - 引用未知 ``candidate_id`` 的成交拒绝并计数。
    - 费用缺失时 ``fee_status`` 标 ``MISSING_FEE`` 且 ``fee_amount`` 为 None，不补 0。
    - ``execution_price_deviation`` 为带方向符号的相对偏差，正值表示执行更差。
    """

    candidate_map: dict[str, dict[str, Any]] = {}
    for raw in candidates:
        candidate_id = str(raw.get("candidate_id") or "").strip()
        if candidate_id:
            candidate_map[candidate_id] = dict(raw)

    prices = evaluation_prices or {}
    benchmarks = benchmark_returns or {}
    rows: list[dict[str, Any]] = []
    fills_seen = 0
    lookahead_rejected = 0
    orphan_rejected = 0
    invalid_rejected = 0
    missing_fee_rows = 0

    for fill in fills:
        fills_seen += 1
        candidate_id = str(fill.get("candidate_id") or "").strip()
        candidate = candidate_map.get(candidate_id)
        if candidate is None:
            orphan_rejected += 1
            continue
        fill_time = _parse_timestamp(fill.get("fill_time"))
        generated_at = _parse_timestamp(candidate.get("generated_at"))
        fill_price = _number(fill.get("fill_price"))
        quantity = _number(fill.get("quantity"))
        if (
            fill_time is None
            or generated_at is None
            or fill_price is None
            or fill_price <= 0
            or quantity is None
            or quantity <= 0
        ):
            invalid_rejected += 1
            continue
        if fill_time < generated_at:
            lookahead_rejected += 1
            continue

        side = str(candidate.get("side") or "").upper()
        adverse_sign = _ADVERSE_SIGN.get(side, 1.0)
        return_sign = _RETURN_SIGN.get(side, 1.0)

        decision_price = _number(candidate.get("decision_price"))
        deviation = (
            round(adverse_sign * (fill_price - decision_price) / decision_price, 8)
            if decision_price is not None and decision_price > 0
            else None
        )

        fee_amount = _number(fill.get("fee_amount"))
        fee_status = "OBSERVED" if fee_amount is not None else MISSING_FEE
        if fee_amount is None:
            missing_fee_rows += 1

        evaluation_price = _number(prices.get(candidate.get("symbol")))
        gross_return = (
            round(return_sign * (evaluation_price - fill_price) / fill_price, 8)
            if evaluation_price is not None and evaluation_price > 0
            else None
        )
        net_return = None
        if gross_return is not None and fee_amount is not None:
            fee_rate = fee_amount / (fill_price * quantity)
            net_return = round(gross_return - fee_rate, 8)
        benchmark_return = _number(benchmarks.get(candidate.get("symbol")))
        relative_benchmark_return = (
            round(net_return - benchmark_return, 8)
            if net_return is not None and benchmark_return is not None
            else None
        )

        rows.append(
            {
                "candidate_id": candidate_id,
                "generated_at": generated_at.isoformat(),
                "data_as_of": candidate.get("data_as_of"),
                "strategy_version": candidate.get("strategy_version"),
                "source": candidate.get("source"),
                "symbol": candidate.get("symbol"),
                "side": side,
                "conviction": _number(candidate.get("conviction")),
                "fill_time": fill_time.isoformat(),
                "fill_price": fill_price,
                "quantity": quantity,
                "decision_price": decision_price,
                "execution_price_deviation": deviation,
                "fee_status": fee_status,
                "fee_amount": fee_amount,
                "gross_return": gross_return,
                "net_return": net_return,
                "benchmark_return": benchmark_return,
                "relative_benchmark_return": relative_benchmark_return,
            }
        )

    return {
        "schema_version": DATASET_SCHEMA,
        "status": "ROWS_BUILT",
        "candidates_seen": len(candidate_map),
        "fills_seen": fills_seen,
        "rows_built": len(rows),
        "lookahead_rejected": lookahead_rejected,
        "orphan_fills_rejected": orphan_rejected,
        "invalid_fills_rejected": invalid_rejected,
        "missing_fee_rows": missing_fee_rows,
        "rows": rows,
    }


def build_forward_learning_dataset(
    *,
    data_root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """构建前向回归数据集；任一前置条件缺失即 fail-closed。

    前置条件（全部缺失时返回 ``PENDING_REAL_SAMPLE`` 且不生成训练行）：

    - 经人工批准的券商导出字段映射（``mapping_status`` 离开 ``PENDING_REAL_SAMPLE``）。
    - 规范化成交表 ``broker_sim_normalized_fill``（由批准映射驱动的导入器创建）。
    """

    root = resolve_data_root(data_root)
    db = Path(db_path or os.getenv("DUCKDB_PATH", "") or root / "finance.duckdb")
    missing: list[str] = []

    mapping_approved = False
    if db.is_file():
        with Warehouse(db) as warehouse:
            if warehouse.table_exists("broker_sim_mapping_approval"):
                rows = warehouse.query(
                    "SELECT COUNT(*) AS count FROM broker_sim_mapping_approval "
                    "WHERE decision = 'APPROVED'"
                )
                mapping_approved = bool(rows and rows[0]["count"] > 0)
            if not warehouse.table_exists(NORMALIZED_FILL_TABLE):
                missing.append("NORMALIZED_SIM_FILL_TABLE")
    else:
        missing.append("NORMALIZED_SIM_FILL_TABLE")
    if not mapping_approved:
        missing.insert(0, "APPROVED_FIELD_MAPPING")

    if missing:
        return {
            "schema_version": DATASET_SCHEMA,
            "status": "PENDING_REAL_SAMPLE",
            "missing": missing,
            "reason": (
                "没有经人工批准的国信模拟盘字段映射和规范化成交表；"
                "在真实样本验收前不生成任何回归训练行。"
            ),
            "rows": [],
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    with Warehouse(db) as warehouse:
        fills = warehouse.query(f"SELECT * FROM {NORMALIZED_FILL_TABLE}")
        candidates = warehouse.query("SELECT * FROM strategy_candidate")
    result = _build_rows(candidates, fills)
    return {
        **result,
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI：构建并落盘 reports/forward_learning/dataset_<date>.json。

    用法：python3 -m foundf_db.forward_learning_dataset [--data-root data]
    15:52 系统 cron 每日串联在 normalize 之后自动执行（2026-08-14 起）。
    """
    import argparse
    import json
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="data")
    parser.add_argument(
        "--out-dir", default="reports/forward_learning",
        help="产物目录（相对项目根）",
    )
    args = parser.parse_args(argv)

    result = build_forward_learning_dataset(data_root=args.data_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = out_dir / f"dataset_{today}.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    summary = {k: v for k, v in result.items() if k != "rows"}
    print(json.dumps(summary, ensure_ascii=False))
    print(f"已落盘: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
