#!/usr/bin/env python3
"""Walk-Forward 输入 bundle v2 生成器（真 PIT 宇宙重建 Phase 3）。

与 v1（deploy/build_walk_forward_bundle.py）的差异：

- manifest schema = ``foundf/walk_forward_inputs/v2``：价格序列覆盖要求为
  「该 symbol 自身 membership 区间 ∩ research 区间」，从而装得下中途退市
  的标的（v1 的全覆盖要求只能容纳全程有行情的幸存者）。
- memberships 由 ``stock_registry``（Phase 1）生成：**可交易池语义**，
  不是指数成分——每只股票自上市首个交易日（daily_price first_session）
  起可交易；退市股 effective_to = min(out_date, 最后可得交易日)（可交易
  讫日：长期停牌至退市的标的以最后实际交易日出池，与行情覆盖严格一致），
  现役股 effective_to = NULL。universe_id = ``cn_a_share_pit_v2``。
- research 区间 = 基准（sh.000300）的 [first_session, last_session]；
  现役标的 data_end 必须覆盖到 research_end，退市标的按构造覆盖到自身
  effective_to（last_session）；不满足者跳过并记录原因。
- 停牌/退市段的逐日语义不在 manifest 层表达，由引擎 v2 模式经
  stock_status_daily（Phase 2）在回测时判定。

归档仍走既有 ``WalkForwardInputStore.archive_bundle``（immutable raw layer +
DuckDB 索引，哈希校验），本脚本**不执行 approve-basis**（人工门禁留给用户，
readiness 在批准前为 NOT_READY / TOTAL_RETURN_BASIS_APPROVAL_MISSING）。

用法::

    python3 deploy/build_walk_forward_bundle_v2.py [--db data/finance.duckdb] \
        [--data-root data] [--dry-run] [--staging-dir <dir>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foundf_db.walk_forward_input_store import (  # noqa: E402
    SCHEMA_VERSION_V2,
    TOTAL_RETURN_BASIS,
    WalkForwardInputStore,
    canonical_price_hash,
)
from foundf_db.warehouse import Warehouse  # noqa: E402

BENCHMARK_SYMBOL = "sh.000300"
BENCHMARK_ID = "CSI300"
PRICE_FIELD = "close"
ADJUSTMENT_METHOD = "VENDOR_TOTAL_RETURN_SERIES"
SOURCE_ID = "baostock-qfq-daily-v2-pit"
DATASET_ID = "walk-forward-inputs-cn-a-pit"
UNIVERSE_ID = "cn_a_share_pit_v2"
BASIS_NOTE = (
    "真 PIT 可交易池（非指数成分）：membership 起 = 上市首个交易日 "
    "(daily_price first_session)；退市股讫 = min(stock_registry.out_date, "
    "最后可得交易日)（可交易讫日口径：停牌至退市段无行情行，pool 在最后 "
    "实际交易日终止，与 v2 区间覆盖校验严格一致）；现役股讫 = NULL；"
    "价格口径同 v1（baostock 前复权 close 作 total-return 代理，"
    "adj_factor 全 NULL）。窗口内停牌/退市语义由引擎 v2 模式按 "
    "stock_status_daily 与 effective_to 判定（结转/强制退出）。"
    "reanchor 改写历史 close 后 warehouse_sha256 失配需重建 bundle。"
)


def summarize_pool(
    db_path: str | Path, *, benchmark_symbol: str = BENCHMARK_SYMBOL
) -> list[dict[str, Any]]:
    """stock_registry ⋈ daily_price：逐 symbol 覆盖范围、source 一致性与退市日。

    基准（指数，不在 stock_registry）单独从 daily_price 统计后并入。
    """
    with Warehouse(db_path) as warehouse:
        warehouse.init()
        rows = warehouse.query(
            "SELECT r.symbol AS symbol, r.ipo_date AS ipo_date, "
            "r.out_date AS out_date, r.list_status AS list_status, "
            "MIN(d.date) AS first_session, MAX(d.date) AS last_session, "
            "COUNT(*) AS row_count, COUNT(DISTINCT d.source) AS source_count, "
            "MIN(d.source) AS source "
            "FROM stock_registry r JOIN daily_price d ON d.symbol = r.symbol "
            "GROUP BY r.symbol, r.ipo_date, r.out_date, r.list_status "
            "ORDER BY r.symbol"
        )
        benchmark = warehouse.query(
            "SELECT symbol, MIN(date) AS first_session, "
            "MAX(date) AS last_session, COUNT(*) AS row_count, "
            "COUNT(DISTINCT source) AS source_count, MIN(source) AS source "
            "FROM daily_price WHERE symbol = ? GROUP BY symbol",
            [benchmark_symbol],
        )
    if benchmark:
        rows.append(
            {
                **benchmark[0],
                "ipo_date": None,
                "out_date": None,
                "list_status": "INDEX",
            }
        )
    return rows


def select_universe(
    stats: list[dict[str, Any]],
    *,
    benchmark_symbol: str = BENCHMARK_SYMBOL,
) -> dict[str, Any]:
    """按 v2 合同的区间覆盖要求挑选纳入 symbol 并定 research 区间。"""
    benchmark = next(
        (row for row in stats if row["symbol"] == benchmark_symbol), None
    )
    if benchmark is None:
        raise SystemExit(f"benchmark {benchmark_symbol} 不在 daily_price 中")
    research_start = benchmark["first_session"]
    research_end = benchmark["last_session"]
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in stats:
        symbol = row["symbol"]
        if symbol == benchmark_symbol:
            continue
        if row["source_count"] != 1:
            skipped.append({"symbol": symbol, "reason": "MIXED_WAREHOUSE_SOURCE"})
            continue
        out_date = row["out_date"]
        if out_date is not None:
            # 退市标的：可交易讫日 = min(out_date, 最后可得交易日)。
            # 长期停牌至退市的标的 out_date 晚于最后交易日，pool 在最后
            # 实际交易日终止，行情覆盖与 membership 讫日按构造严格一致。
            row["effective_to"] = min(out_date, row["last_session"])
            if row["effective_to"] < research_start:
                skipped.append(
                    {
                        "symbol": symbol,
                        "reason": "DELISTED_BEFORE_RESEARCH_START",
                    }
                )
                continue
        else:
            # 现役标的：行情必须覆盖到 research_end
            row["effective_to"] = None
            if row["last_session"] < research_end:
                skipped.append(
                    {
                        "symbol": symbol,
                        "reason": "DATA_ENDS_BEFORE_RESEARCH_END",
                    }
                )
                continue
        eligible.append(row)
    if not eligible:
        raise SystemExit("没有满足 v2 覆盖要求的 symbol，无法构造 bundle")
    return {
        "benchmark": benchmark,
        "eligible": sorted(eligible, key=lambda item: item["symbol"]),
        "skipped": skipped,
        "research_start": research_start,
        "research_end": research_end,
    }


def build_plan(
    db_path: str | Path, *, benchmark_symbol: str = BENCHMARK_SYMBOL
) -> dict[str, Any]:
    """只读统计 + universe 选择；不写任何文件，供 --dry-run 与测试使用。"""
    stats = summarize_pool(db_path, benchmark_symbol=benchmark_symbol)
    selection = select_universe(stats, benchmark_symbol=benchmark_symbol)
    return {
        "db_path": str(db_path),
        "benchmark_symbol": benchmark_symbol,
        "universe_id": UNIVERSE_ID,
        "schema_version": SCHEMA_VERSION_V2,
        "research_start": selection["research_start"].isoformat(),
        "research_end": selection["research_end"].isoformat(),
        "eligible_symbols": [row["symbol"] for row in selection["eligible"]],
        "eligible_count": len(selection["eligible"]),
        "delisted_count": sum(
            1 for row in selection["eligible"] if row["out_date"] is not None
        ),
        "total_rows": sum(row["row_count"] for row in selection["eligible"])
        + selection["benchmark"]["row_count"],
        "skipped": selection["skipped"],
    }


def _price_entry(
    warehouse: Warehouse,
    row: dict[str, Any],
    *,
    staging_dir: Path,
    benchmark: bool,
) -> dict[str, Any]:
    """对一条序列取全历史行、算 canonical 哈希并写口径证明 artifact。"""
    symbol = row["symbol"]
    rows = warehouse.query(
        "SELECT date, symbol, open, close, adj_factor, source FROM daily_price "
        "WHERE symbol = ? ORDER BY date",
        [symbol],
    )
    if len(rows) != row["row_count"]:
        raise SystemExit(f"{symbol} 行数漂移：{len(rows)} != {row['row_count']}")
    digest = canonical_price_hash(rows)
    artifact_rel = f"prices/{symbol}.json"
    artifact_path = staging_dir / artifact_rel
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_payload = {
        "symbol": symbol,
        "basis": TOTAL_RETURN_BASIS,
        "price_field": PRICE_FIELD,
        "adjustment_method": ADJUSTMENT_METHOD,
        "warehouse_source": row["source"],
        "data_start": row["first_session"].isoformat(),
        "data_end": row["last_session"].isoformat(),
        "expected_row_count": int(row["row_count"]),
        "warehouse_sha256": digest,
        "basis_note": BASIS_NOTE,
    }
    artifact_bytes = (
        json.dumps(artifact_payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    artifact_path.write_bytes(artifact_bytes)
    entry = {
        "symbol": symbol,
        "source_id": SOURCE_ID,
        "basis": TOTAL_RETURN_BASIS,
        "price_field": PRICE_FIELD,
        "adjustment_method": ADJUSTMENT_METHOD,
        "warehouse_source": row["source"],
        "data_start": row["first_session"].isoformat(),
        "data_end": row["last_session"].isoformat(),
        "first_session": row["first_session"].isoformat(),
        "last_session": row["last_session"].isoformat(),
        "expected_row_count": int(row["row_count"]),
        "warehouse_sha256": digest,
        "artifact": {
            "path": artifact_rel,
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
        },
    }
    if benchmark:
        entry["benchmark_id"] = BENCHMARK_ID
    return entry


def write_bundle(
    db_path: str | Path,
    staging_dir: str | Path,
    *,
    benchmark_symbol: str = BENCHMARK_SYMBOL,
) -> Path:
    """在 staging 目录生成 v2 manifest.json + 全部 artifact，返回 manifest 路径。"""
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    stats = summarize_pool(db_path, benchmark_symbol=benchmark_symbol)
    selection = select_universe(stats, benchmark_symbol=benchmark_symbol)
    now = datetime.now(timezone.utc)
    today = date.today()
    as_of = max(today, selection["research_end"])

    memberships = [
        {
            "symbol": row["symbol"],
            # 可交易起点 = 上市首个交易日（与行情数据起点同口径）
            "effective_from": row["first_session"].isoformat(),
            # 可交易讫日：退市股 = min(out_date, 最后交易日)，现役 = NULL
            "effective_to": (
                row["effective_to"].isoformat()
                if row["effective_to"] is not None
                else None
            ),
            "source_id": SOURCE_ID,
        }
        for row in selection["eligible"]
    ]
    membership_bytes = (
        json.dumps(
            {"universe_id": UNIVERSE_ID, "memberships": memberships},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    (staging / "memberships.json").write_bytes(membership_bytes)

    with Warehouse(db_path) as warehouse:
        price_series = [
            _price_entry(warehouse, row, staging_dir=staging, benchmark=False)
            for row in selection["eligible"]
        ]
        benchmark = _price_entry(
            warehouse, selection["benchmark"], staging_dir=staging, benchmark=True
        )

    manifest = {
        "schema_version": SCHEMA_VERSION_V2,
        "dataset_id": DATASET_ID,
        "universe_id": UNIVERSE_ID,
        "generated_at": now.isoformat(),
        "as_of": as_of.isoformat(),
        "research_start": selection["research_start"].isoformat(),
        "research_end": selection["research_end"].isoformat(),
        "sources": [
            {
                "source_id": SOURCE_ID,
                "provider": "baostock",
                "dataset_version": f"daily_price qfq snapshot {as_of.isoformat()}",
                "retrieved_at": now.isoformat(),
                "license_reference": (
                    "baostock 公开证券行情接口，个人研究用途；"
                    "warehouse 表 daily_price（UNIQUE(symbol,date) 单源口径）；"
                    "membership 来自 stock_registry（Phase 1）"
                ),
                "storage_permitted": True,
                "basis_note": BASIS_NOTE,
            }
        ],
        "membership_artifact": {
            "path": "memberships.json",
            "sha256": hashlib.sha256(membership_bytes).hexdigest(),
            "size_bytes": len(membership_bytes),
        },
        "memberships": memberships,
        "price_series": price_series,
        "benchmark": benchmark,
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="stock_registry + daily_price 现状 → v2 PIT bundle 归档"
    )
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--data-root", default="data")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 universe 选择与统计，不写文件不归档",
    )
    parser.add_argument(
        "--staging-dir",
        help="manifest/artifact staging 目录（默认临时目录，归档成功后清理）",
    )
    args = parser.parse_args(argv)

    plan = build_plan(args.db)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("DRY-RUN：未写任何文件，未归档。")
        return 0

    own_staging = args.staging_dir is None
    staging = Path(
        args.staging_dir or tempfile.mkdtemp(prefix="walk_forward_bundle_v2_")
    )
    try:
        manifest_path = write_bundle(args.db, staging)
        store = WalkForwardInputStore(
            data_root=args.data_root, db_path=args.db
        )
        result = store.archive_bundle(manifest_path)
    finally:
        if own_staging:
            shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(
        "注意：basis 人工批准（approve-basis）未执行，readiness 在批准前仍为 "
        "NOT_READY（TOTAL_RETURN_BASIS_APPROVAL_MISSING）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
