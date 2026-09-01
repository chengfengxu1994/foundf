"""生产 runner：最新 Walk-Forward bundle → WalkForwardEngine → 治理证据报告。

读取 ``walk_forward_input_bundle`` 最新归档（由
``deploy/build_walk_forward_bundle.py`` 生成）的 universe membership 与
price evidence 索引表，构造 point-in-time 多因子打分策略，跑
``WalkForwardEngine`` 并产出 ``strategy_report/walk_forward_*.json``
（schema ``foundf.walk_forward_evidence.v1``），即 evolution.py
``backtest_gate`` 经 evidence_adapter 消费的格式。

打分口径（v2.0.0 起）：策略回调是 ``quant_strategy/kernel.py`` 纯函数
内核的薄适配器，数值语义 = 生产 ``FactorEngine`` multifactor_v3_sim.5
五桶打分（value 真 EP/BP 缺省回退 MA60 代理 / quality / growth /
momentum / risk 含 Beta），parity 由 ``tests/strategy/test_kernel_parity.py``
锁定。与 v1.0.0（本地 z-score 合成、满仓归一）相比：

- 权重含隐含现金腿：内核按 vol_budget × breadth_budget 计算
  risk_budget∈[min_budget, 1.0]，Σw = risk_budget ≤ 1（引擎允许 Σw<1，
  CASH 腿由引擎 ``_turnover`` 自行补全，回调返回前剔除 CASH 键——
  CASH 不在 universe 内会被 ``_weights`` 拒绝）。gross/net return 口径
  随之变化（现金腿零收益摊薄），属预期变化。
- no_trade_band 不接：生产 buffer 带是与"上一批日度 targets"对比的
  换手抑制，Walk-Forward 窗口频率为 6 个月，相邻窗口权重几乎必然全部
  出带，缓冲带恒不触发，接入只会增加噪音，故显式声明不接。
- 估值注入：main() 按每个窗口 train_end 预查 ``daily_basic``
  point-in-time 快照闭包注入；某窗口查不到任何估值 → 全标的回退
  MA60 代理（与生产 source="proxy" 路径一致），不新增 fail-closed 错误码。
- 基准注入：闭包注入基准收盘价序列，按内核样板提取末尾 63 日收益
  （保留生产 Beta 不按日期对齐的已知缺陷语义，原样复刻不"顺手修复"）。

运行注意：

- DuckDB 单写者锁：引擎 run 期间本进程持有 finance.duckdb 读写连接，
  必须避开 collector 容器每日 17:15（Asia/Shanghai）前后的采集写库窗口，
  否则连库即失败；也不要与 quote_daemon 写库时刻重叠。
- 本 runner 只产出研究证据，不改任何生产权重、不代表券商成交。
- bundle 版本自适应（真 PIT 宇宙重建 Phase 3）：归档 manifest 为
  ``foundf/walk_forward_inputs/v2`` 时，引擎开启 pit_v2_mode（成员级
  历史不足剔除 / 退市强制退出 / 停牌结转）并注入 ``stock_status_daily``
  预载的 ``DictPitStatusView``；v1 bundle 走原路径，行为不变。

CLI::

    python3 -m backtest_engine.v2.runner [--db data/finance.duckdb] \
        [--output-dir strategy_report] [--minimum-universe-size 30] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from foundf_db import Warehouse
from foundf_db.walk_forward_input_store import (
    SCHEMA_VERSION as WFI_SCHEMA_V1,
    SCHEMA_VERSION_V2 as WFI_SCHEMA_V2,
)
from quant_strategy import FactorConfig
from quant_strategy import kernel

from . import (
    CostModel,
    PriceSeriesEvidence,
    UniverseMembership,
    WalkForwardDataError,
    WalkForwardEngine,
    WindowContext,
)

STRATEGY_ID = "pit-multifactor-top6"
STRATEGY_VERSION = "2.0.0"
DEFAULT_COST_MODEL = CostModel(
    model_id="cn-a-share-sim-cost-v1",
    commission_bps=2.5,
    slippage_bps=15,
)
# 生产默认配置：打分口径 = multifactor_v3_sim.5（top_n=6 / max_weight=0.25
# / min_history=130 / min_budget=0.25）。策略回调全部数值经内核读取本配置。
FACTOR_CONFIG = FactorConfig()


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _load_bundle_schema_version(
    warehouse: Warehouse, archive_relpath: Any
) -> str:
    """从归档 manifest 读 schema_version，决定引擎走 v1 还是 v2 PIT 语义。

    归档文件缺失时按 v1 处理（单元测试只种索引表、不落归档目录的场景）；
    文件存在但损坏或版本未知则 fail-closed。
    """
    manifest_path = (
        warehouse.db_path.parent / str(archive_relpath) / "manifest.json"
    )
    if not manifest_path.is_file():
        return WFI_SCHEMA_V1
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WalkForwardDataError(
            "ARCHIVE_MANIFEST_INVALID", str(manifest_path)
        ) from exc
    schema = raw.get("schema_version")
    if schema not in (WFI_SCHEMA_V1, WFI_SCHEMA_V2):
        raise WalkForwardDataError("INPUT_SCHEMA_UNSUPPORTED", str(schema))
    return schema


class DictPitStatusView:
    """dict 后端的 PitStatusView：runner 预载 stock_status_daily 后注入引擎。

    键为 (symbol, date ISO 字符串)，值为 trade_status（1 正常 / 0 停牌）；
    查不到返回 None（无状态行），引擎据此 fail-closed 数据缺口。
    """

    def __init__(self, status: Mapping[tuple[str, str], int]) -> None:
        self._status = dict(status)

    def trade_status(self, symbol: str, day: date) -> int | None:
        return self._status.get((symbol, day.isoformat()))


def load_pit_status_view(
    warehouse: Warehouse,
    symbols: tuple[str, ...],
    start: Any,
    end: Any,
) -> DictPitStatusView:
    """预载 stock_status_daily 全区间状态行，构造引擎的停牌判定视图。"""
    if not symbols:
        return DictPitStatusView({})
    placeholders = ", ".join("?" for _ in symbols)
    rows = warehouse.query(
        "SELECT symbol, date, trade_status FROM stock_status_daily "
        f"WHERE symbol IN ({placeholders}) AND date >= ? AND date <= ?",
        [*symbols, _iso(start), _iso(end)],
    )
    return DictPitStatusView(
        {
            (str(row["symbol"]), _iso(row["date"])): int(row["trade_status"])
            for row in rows
        }
    )


def load_latest_bundle(warehouse: Warehouse) -> dict[str, Any]:
    """读最新 bundle 的两张索引表并构造引擎 dataclass。无 bundle 时 fail-closed。"""
    bundles = warehouse.query(
        "SELECT bundle_id, research_start, research_end, archive_relpath "
        "FROM walk_forward_input_bundle "
        "ORDER BY received_at DESC, bundle_id DESC LIMIT 1"
    )
    if not bundles:
        raise WalkForwardDataError("WALK_FORWARD_INPUT_BUNDLE_MISSING")
    bundle = bundles[0]
    bundle_id = bundle["bundle_id"]
    membership_rows = warehouse.query(
        "SELECT symbol, effective_from, effective_to, source_id "
        "FROM walk_forward_universe_membership WHERE bundle_id = ? "
        "ORDER BY symbol, effective_from",
        [bundle_id],
    )
    evidence_rows = warehouse.query(
        "SELECT symbol, is_benchmark, benchmark_id, basis, price_field, source_id "
        "FROM walk_forward_price_evidence WHERE bundle_id = ? ORDER BY symbol",
        [bundle_id],
    )
    benchmark = next(
        (row for row in evidence_rows if row["is_benchmark"]), None
    )
    if benchmark is None:
        raise WalkForwardDataError("BENCHMARK_ID_MISSING", bundle_id)
    return {
        "bundle_id": bundle_id,
        "research_start": bundle["research_start"],
        "research_end": bundle["research_end"],
        "archive_relpath": bundle["archive_relpath"],
        "schema_version": _load_bundle_schema_version(
            warehouse, bundle["archive_relpath"]
        ),
        "memberships": [
            UniverseMembership(
                symbol=row["symbol"],
                effective_from=_iso(row["effective_from"]),
                effective_to=(
                    _iso(row["effective_to"])
                    if row["effective_to"] is not None
                    else None
                ),
                source_id=row["source_id"],
            )
            for row in membership_rows
        ],
        "price_evidence": [
            PriceSeriesEvidence(
                symbol=row["symbol"],
                basis=row["basis"],
                price_field=row["price_field"],
                source_id=row["source_id"],
            )
            for row in evidence_rows
        ],
        "benchmark_symbol": benchmark["symbol"],
        "benchmark_id": benchmark["benchmark_id"],
    }


def load_valuation_snapshots(
    warehouse: Warehouse,
    symbols: tuple[str, ...],
    train_ends: tuple[date, ...],
) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    """按每个窗口 train_end 预查 daily_basic point-in-time 估值快照。

    对每个 train_end 取每标的 ``date <= train_end`` 的最新一行
    （QUALIFY ROW_NUMBER 窗口函数），返回
    ``{train_end_iso: {symbol: (pe_ttm, pb)}}``。某窗口无任何估值行时
    该键缺失，回调内回退 MA60 代理（与生产 source="proxy" 一致），
    不新增 fail-closed 错误码。
    """
    if not symbols or not train_ends:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    snapshots: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for train_end in sorted(train_ends):
        rows = warehouse.query(
            "SELECT symbol, pe_ttm, pb FROM daily_basic "
            f"WHERE symbol IN ({placeholders}) AND date <= ? "
            "QUALIFY ROW_NUMBER() OVER ("
            "PARTITION BY symbol ORDER BY date DESC) = 1",
            [*symbols, train_end.isoformat()],
        )
        if not rows:
            continue
        snapshots[train_end.isoformat()] = {
            str(row["symbol"]): (row["pe_ttm"], row["pb"]) for row in rows
        }
    return snapshots


def load_benchmark_closes(
    warehouse: Warehouse,
    benchmark_symbol: str,
    start: Any,
    end: Any,
) -> tuple[tuple[str, float], ...]:
    """读基准（沪深300）全区间收盘价，供回调按 train_end 切片提取 63 日收益。"""
    rows = warehouse.query(
        "SELECT date, close FROM daily_price "
        "WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
        [benchmark_symbol, _iso(start), _iso(end)],
    )
    return tuple(
        (_iso(row["date"]), float(row["close"]))
        for row in rows
        if row["close"] is not None and float(row["close"]) > 0
    )


def _benchmark_returns63(
    benchmark_closes: tuple[tuple[str, float], ...],
    context: WindowContext,
) -> np.ndarray | None:
    """按内核样板提取基准末尾 63 日收益（≥64 根 → diff/前序 → [-63:]）。

    保留生产 Beta 不按日期对齐的已知缺陷语义：基准与股票各取末尾
    63 日收益直接 cov，原样复刻不"修复"。不足 64 根返回 None
    （内核 Beta 分量退化中性 0.5，与生产一致）。
    """
    closes = np.array(
        [close for day, close in benchmark_closes if day <= context.train_end],
        dtype=float,
    )
    if closes.size < 64:
        return None
    return (np.diff(closes) / closes[:-1])[-63:]


def multifactor_strategy(
    training: Mapping[str, tuple[dict[str, Any], ...]],
    context: WindowContext,
    valuation: Mapping[str, tuple[float | None, float | None]] | None = None,
    benchmark_returns63: np.ndarray | None = None,
) -> dict[str, float]:
    """Point-in-time 多因子打分：quant_strategy/kernel.py 纯函数内核的薄适配器。

    打分口径 = 生产 FactorEngine multifactor_v3_sim.5 五桶语义（value 真
    EP/BP 缺省回退 MA60 代理 / quality / growth / momentum / risk 含 Beta，
    权重 0.25/0.25/0.20/0.15/0.15 横截面百分位合成）。只消费引擎传入的
    training 行（train 窗口截断，T+1 开盘价执行）与闭包注入的估值/基准
    快照；编排顺序与 ``tests/strategy/test_kernel_parity.py`` 的
    run_kernel() 样板一致：min_history 剔除 → 逐标的 score_symbol →
    cross_sectional_scores → weights_from_scores。

    返回内核 weights 剔除 CASH 键与零权重后的资产权重：Σw =
    risk_budget ∈ [min_budget, 1.0]，隐含现金腿由引擎补全（引擎允许
    Σw<1；CASH 键不在 universe 内，原样返回会被拒）。
    """
    config = FACTOR_CONFIG
    valuation = valuation or {}
    scored: dict[str, tuple[dict[str, float], str]] = {}
    factors: dict[str, dict[str, float]] = {}
    returns63: dict[str, np.ndarray] = {}
    for symbol in sorted(training):
        rows = training[symbol]
        # min_history 剔除（生产在 compute_all 循环里按 bars 行数做，
        # 内核按纯函数不入核，由调用方负责）
        if len(rows) < config.min_history:
            continue
        ordered = sorted(rows, key=lambda row: row["date"])
        closes = np.array([float(row["close"]) for row in ordered], dtype=float)
        scored[symbol] = kernel.score_symbol(
            closes, valuation.get(symbol), benchmark_returns63, config
        )
        factors[symbol] = scored[symbol][0]
        # 相关性惩罚内存 returns 视图：最后 63 根收盘 → 62 长度日收益
        closes63 = closes[-63:]
        returns63[symbol] = np.diff(closes63) / closes63[:-1]
    if not factors:
        raise WalkForwardDataError("STRATEGY_WEIGHTS_MISSING", context.train_end)
    raw_scores = kernel.cross_sectional_scores(factors, config)
    result = kernel.weights_from_scores(
        raw_scores,
        factors,
        returns63,
        config,
        value_source=kernel.build_value_source_counter(scored),
    )
    weights = result["weights"]
    weights.pop("CASH", None)  # 现金腿隐含：CASH 不在 universe，引擎会拒
    return {symbol: weight for symbol, weight in weights.items() if weight > 0}


def build_strategy(
    valuation_by_train_end: Mapping[str, Mapping[str, tuple[float | None, float | None]]],
    benchmark_closes: tuple[tuple[str, float], ...],
) -> Callable[[Mapping[str, tuple[dict[str, Any], ...]], WindowContext], dict[str, float]]:
    """构造引擎回调闭包：按 context.train_end 注入估值快照与基准 63 日收益。

    某窗口查不到任何估值 → valuation=None → 全标的 proxy 回退（不报错）；
    基准不足 64 根 → benchmark_returns63=None → Beta 分量中性 0.5。
    """

    def strategy(
        training: Mapping[str, tuple[dict[str, Any], ...]],
        context: WindowContext,
    ) -> dict[str, float]:
        return multifactor_strategy(
            training,
            context,
            valuation=valuation_by_train_end.get(context.train_end),
            benchmark_returns63=_benchmark_returns63(benchmark_closes, context),
        )

    return strategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="最新 Walk-Forward bundle → 引擎回测 → 治理证据报告"
    )
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--output-dir", default="strategy_report")
    parser.add_argument("--minimum-universe-size", type=int, default=30)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 bundle 与窗口计划，不执行回测",
    )
    args = parser.parse_args(argv)

    warehouse = Warehouse(args.db)
    warehouse.init()
    try:
        bundle = load_latest_bundle(warehouse)
        # v2 bundle（membership 区间 + 退市/停牌语义）开启引擎 PIT v2 模式，
        # 并注入 stock_status_daily 状态视图；v1 bundle 行为逐位不变。
        is_v2 = bundle["schema_version"] == WFI_SCHEMA_V2
        membership_symbols = tuple(
            sorted({item.symbol for item in bundle["memberships"]})
        )
        pit_status_view = (
            load_pit_status_view(
                warehouse,
                membership_symbols,
                bundle["research_start"],
                bundle["research_end"],
            )
            if is_v2
            else None
        )
        engine = WalkForwardEngine(
            warehouse,
            minimum_universe_size=args.minimum_universe_size,
            pit_v2_mode=is_v2,
            pit_status_view=pit_status_view,
        )
        windows = engine.build_windows(
            bundle["research_start"], bundle["research_end"]
        )
        plan = {
            "bundle_id": bundle["bundle_id"],
            "input_schema_version": bundle["schema_version"],
            "pit_v2_mode": is_v2,
            "research_start": _iso(bundle["research_start"]),
            "research_end": _iso(bundle["research_end"]),
            "membership_count": len(bundle["memberships"]),
            "price_evidence_count": len(bundle["price_evidence"]),
            "benchmark_symbol": bundle["benchmark_symbol"],
            "window_count": len(windows),
            "first_window": {
                key: value.isoformat() for key, value in windows[0].items()
            },
            "last_window": {
                key: value.isoformat() for key, value in windows[-1].items()
            },
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "cost_model_total_bps": DEFAULT_COST_MODEL.total_bps,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if args.dry_run:
            print("DRY-RUN：未执行回测。")
            return 0

        # 闭包注入：逐窗口 point-in-time 估值快照 + 基准收盘价序列
        valuation_by_train_end = load_valuation_snapshots(
            warehouse,
            membership_symbols,
            tuple(window["train_end"] for window in windows),
        )
        benchmark_closes = load_benchmark_closes(
            warehouse,
            bundle["benchmark_symbol"],
            bundle["research_start"],
            bundle["research_end"],
        )
        evidence = engine.run(
            build_strategy(valuation_by_train_end, benchmark_closes),
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            memberships=bundle["memberships"],
            price_evidence=bundle["price_evidence"],
            benchmark_symbol=bundle["benchmark_symbol"],
            benchmark_id=bundle["benchmark_id"],
            cost_model=DEFAULT_COST_MODEL,
            start_date=bundle["research_start"],
            end_date=bundle["research_end"],
        )
        report_path = WalkForwardEngine.generate_report(
            evidence, Path(args.output_dir)
        )
    finally:
        warehouse.close()
    print(f"report: {report_path}")
    print(json.dumps(evidence["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
