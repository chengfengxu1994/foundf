#!/usr/bin/env python3
"""一次性 Walk-Forward 输入 bundle 生成器（daily_price 现状 → v1 manifest 归档）。

从 ``daily_price`` 现状构造 ``foundf.walk_forward_inputs.v1`` manifest 并经
``WalkForwardInputStore.archive_bundle`` 归档（immutable raw layer + DuckDB 索引）。
之后 ``readiness()`` 才能脱离 WALK_FORWARD_INPUT_BUNDLE_MISSING。

口径决策（2026-08-06 数据现状）：

- 只纳入 6 位数字 A 股代码且 ``source`` 全历史单一的 symbol；港股（.HK，
  parquet_migration/yfinance 混源）与混源 symbol 一律跳过——readiness 的
  WAREHOUSE_SOURCE_MISMATCH 检查要求单一系列 source 完全一致。
- ``adj_factor`` 全 NULL → 只能 ``price_field="close"`` +
  ``adjustment_method="VENDOR_TOTAL_RETURN_SERIES"``（baostock 前复权 close
  作 total-return 代理，sources.basis_note 里注明）。
- research 区间 = [benchmark(sh.000300) 首个交易日, 全体纳入 symbol 最晚
  last_session 的最小值]；v1 合同要求每条序列 data_start<=research_start 且
  data_end>=research_end，晚上市/早终止的 symbol 会被排除并记录跳过原因。
- warehouse_sha256 用 walk_forward_input_store.canonical_price_hash（与
  readiness 对账同一口径）。哈希覆盖 symbol 全历史；每日 collect 追加新行
  不影响（区间封闭在 data_end），但 reanchor_qfq 改写历史收盘价后哈希会失配，
  届时需重建 bundle。

本脚本不执行 approve-basis（人工门禁留给用户）。

用法::

    python3 deploy/build_walk_forward_bundle.py [--db data/finance.duckdb] \
        [--data-root data] [--dry-run] [--staging-dir <dir>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foundf_db.walk_forward_input_store import (  # noqa: E402
    SCHEMA_VERSION,
    TOTAL_RETURN_BASIS,
    WalkForwardInputStore,
    canonical_price_hash,
)
from foundf_db.warehouse import Warehouse  # noqa: E402

BENCHMARK_SYMBOL = "sh.000300"
BENCHMARK_ID = "CSI300"
PRICE_FIELD = "close"
ADJUSTMENT_METHOD = "VENDOR_TOTAL_RETURN_SERIES"
SOURCE_ID = "baostock-qfq-daily-v1"
DATASET_ID = "walk-forward-inputs-cn-a"
UNIVERSE_ID = "cn_a_share_baostock_pool_v1"
BASIS_NOTE = (
    "baostock 前复权(qfq) close 作为 total-return 代理：adj_factor 全 NULL，"
    "无法走 RAW_PRICE_PLUS_VENDOR_ADJ_FACTOR；除息接缝由每周 reanchor 修复，"
    "改写历史 close 后 warehouse_sha256 失配需重建 bundle。"
)
_CN_SYMBOL = re.compile(r"^\d{6}$")


def summarize_daily_price(db_path: str | Path) -> list[dict[str, Any]]:
    """逐 symbol 统计 daily_price 覆盖范围与 source 一致性。"""
    with Warehouse(db_path) as warehouse:
        warehouse.init()
        rows = warehouse.query(
            "SELECT symbol, MIN(date) AS first_session, MAX(date) AS last_session, "
            "COUNT(*) AS row_count, COUNT(DISTINCT source) AS source_count, "
            "MIN(source) AS source "
            "FROM daily_price GROUP BY symbol ORDER BY symbol"
        )
    return rows


def select_universe(
    stats: list[dict[str, Any]],
    *,
    benchmark_symbol: str = BENCHMARK_SYMBOL,
) -> dict[str, Any]:
    """按 v1 合同的全覆盖要求挑选纳入 symbol 并定 research 区间。"""
    benchmark = next(
        (row for row in stats if row["symbol"] == benchmark_symbol), None
    )
    if benchmark is None:
        raise SystemExit(f"benchmark {benchmark_symbol} 不在 daily_price 中")
    research_start = benchmark["first_session"]
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in stats:
        symbol = row["symbol"]
        if symbol == benchmark_symbol:
            continue
        if not _CN_SYMBOL.match(symbol):
            skipped.append({"symbol": symbol, "reason": "NON_CN_A_SHARE_SYMBOL"})
            continue
        if row["source_count"] != 1:
            skipped.append({"symbol": symbol, "reason": "MIXED_WAREHOUSE_SOURCE"})
            continue
        if row["first_session"] > research_start:
            skipped.append(
                {"symbol": symbol, "reason": "LISTED_AFTER_RESEARCH_START"}
            )
            continue
        eligible.append(row)
    if not eligible:
        raise SystemExit("没有满足全覆盖要求的 symbol，无法构造 bundle")
    research_end = min(
        [benchmark["last_session"]]
        + [row["last_session"] for row in eligible]
    )
    final: list[dict[str, Any]] = []
    for row in eligible:
        if row["last_session"] < research_end:
            skipped.append(
                {"symbol": row["symbol"], "reason": "DATA_ENDS_BEFORE_RESEARCH_END"}
            )
            continue
        final.append(row)
    return {
        "benchmark": benchmark,
        "eligible": sorted(final, key=lambda item: item["symbol"]),
        "skipped": skipped,
        "research_start": research_start,
        "research_end": research_end,
    }


def build_plan(
    db_path: str | Path, *, benchmark_symbol: str = BENCHMARK_SYMBOL
) -> dict[str, Any]:
    """只读统计 + universe 选择；不写任何文件，供 --dry-run 与测试使用。"""
    stats = summarize_daily_price(db_path)
    selection = select_universe(stats, benchmark_symbol=benchmark_symbol)
    return {
        "db_path": str(db_path),
        "benchmark_symbol": benchmark_symbol,
        "research_start": selection["research_start"].isoformat(),
        "research_end": selection["research_end"].isoformat(),
        "eligible_symbols": [row["symbol"] for row in selection["eligible"]],
        "eligible_count": len(selection["eligible"]),
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
    """在 staging 目录生成 manifest.json + 全部 artifact，返回 manifest 路径。"""
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    stats = summarize_daily_price(db_path)
    selection = select_universe(stats, benchmark_symbol=benchmark_symbol)
    now = datetime.now(timezone.utc)
    today = date.today()
    as_of = max(today, selection["research_end"])

    memberships = [
        {
            "symbol": row["symbol"],
            "effective_from": row["first_session"].isoformat(),
            "effective_to": None,
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
        "schema_version": SCHEMA_VERSION,
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
                    "warehouse 表 daily_price（UNIQUE(symbol,date) 单源口径）"
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
        description="从 daily_price 现状生成并归档 Walk-Forward 输入 bundle"
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
        args.staging_dir or tempfile.mkdtemp(prefix="walk_forward_bundle_")
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
