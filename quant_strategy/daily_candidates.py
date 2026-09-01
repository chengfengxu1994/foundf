"""每日收盘后多因子候选信号生成器（模拟观察期专用）。

定位：把 ``FactorEngine`` 的「股票池 → 打分 → top_n 权重」接到
``StrategyCandidateStore`` 的候选合同上，补上系统缺失的"最后一公里"。

边界（与系统治理一致，不可突破）：

- 只产 BUY 候选，供手机同花顺**模拟盘**观察期执行；不写交易金额、不算数量
  （数量是执行侧策略，见 ``deploy/phone/place_sim_order.py``）。
- 数据陈旧（最新 ``daily_price`` 日期距今天超过 ``MAX_DATA_AGE_DAYS``）时
  fail-closed，不生成任何候选。
- ``production_change_allowed`` / ``automatic_trade_allowed`` 恒为 False；
  治理门禁（``strategy_manager/evolution.py``）状态不受本模块影响。
- 价值因子已接 ``daily_basic`` 真 EP/BP（2026-08-06 起，数据源缺失自动回退
  均线代理并在诊断中计数）；质量/成长因子仍是价格代理
  （``financial_statement`` 表为空），因子裁决无 KEEP；
  因此候选只用于模拟观察与回归训练，不构成任何实盘依据。

T 日收盘补齐（2026-08-06 新增）：baostock 当日收盘 T+1 傍晚才入库，
导致信号到执行隔两个交易日。tushare ``daily`` 当日约 15:40 发布，
因此生成候选时若 ``daily_price`` 尚未含当日，会用 tushare 当日收盘
**纯内存**注入因子序列与决策价表（``volume`` 手→股、``amount`` 千元→元
对齐 baostock 口径），绝不写入 ``daily_price``（canonical 价格源仍是
baostock 前复权，见 ``data_provider/scheduler.py`` 注释）。除权日原始价
与前复权历史存在分红量级偏差，次日 baostock 入库后口径恢复一致；
tushare 不可用/盘前/非交易日时自动回退 T-1 路径。

no_trade_band 缓冲带（2026-08-13，v3_sim.5 新增）：``FactorConfig``
早已声明 ``no_trade_band=0.02`` 但全链路无人读取，候选名单日度换手
过高（一日游，每趟磨损约 0.2%）。现接入：新候选与上一批
``reports/sim_targets/`` 快照权重对比，权重变化绝对值 < 缓冲带的
标的沿用上一批权重（即不发出调仓信号），只有变化超带才进出；
首批无上一批快照时原样输出。``turnover_blend`` 本版仍不接线。

预注册冻结戳记（2026-08-13 新增）：targets 快照顶层与候选 evidence
负载带 ``freeze_id`` / ``freeze_date``（来自 strategy_freeze 活跃冻结，
样本外观察起点见 ``foundf_db/strategy_freeze_store.py``）；前冻结时代
（表不存在/无冻结）写 None，fail-open 不影响生成。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from foundf_db.strategy_candidate_store import StrategyCandidateStore
from foundf_db.strategy_freeze_store import StrategyFreezeStore
from foundf_db.warehouse import Warehouse

from . import FactorEngine
# apply_no_trade_band 已搬入 quant_strategy.kernel（阶段 0 统一内核，
# 函数体一字未动）；此处 re-export 保持既有 import 方（含
# tests/test_daily_candidates.py）不断裂。
from .kernel import (
    VALVE_MIN_POOL,
    apply_no_trade_band,
    apply_regime_valve,
    pool_ep_ts_budget,
)

STRATEGY_VERSION = "multifactor_v5_valve.1"  # v5 = v3_sim.5 + 池级EP便宜度阀门(信号驱动自由仓位, 预注册冻结 sf_674cc7bd2cf24f73443d)
MAX_DATA_AGE_DAYS = 4  # 周末/小长假容忍度；超过即判陈旧
SYMBOL_STALE_GAP_DAYS = 5  # 逐标的新鲜度: 自身最新日期落后全库 MAX 超此天数即剔除(停牌/断采)


def select_candidate_universe(symbol_rows, max_date) -> list[str]:
    """可交易候选宇宙: 仅 6 位 A 股 + 逐标的新鲜度。

    剔除港股(.HK 等)、指数(sh.000300)与自身最新日期落后全库 MAX 超
    SYMBOL_STALE_GAP_DAYS 的停牌/断采标的。
    """

    return sorted(
        r["symbol"] for r in symbol_rows
        if re.fullmatch(r"\d{6}", r["symbol"])
        and (max_date - r["latest"]).days <= SYMBOL_STALE_GAP_DAYS
    )


class DuckDBPriceAdapter:
    """FactorEngine 需要的鸭型 DataProvider（直读 daily_price）。

    实现 ``daily_bars`` / ``stock_basic`` / ``latest_daily_basic`` 三个方法；
    估值快照缺失时价值因子回退均线代理。
    ``extra_bars`` 用于 T 日收盘补齐：仅追加在 DB 序列末尾之后。
    """

    def __init__(self, db_path: str | Path,
                 extra_bars: dict[str, dict[str, Any]] | None = None) -> None:
        self.db_path = Path(db_path)
        self.extra_bars = extra_bars or {}

    def daily_bars(self, symbols: list[str]) -> list[dict[str, Any]]:
        symbol = symbols[0]
        with Warehouse(self.db_path) as warehouse:
            rows = warehouse.query(
                "SELECT date, close, volume, amount FROM daily_price "
                "WHERE symbol = ? ORDER BY date",
                [symbol],
            )
        bars = [
            {"date": str(r["date"]), "close": r["close"],
             "volume": r["volume"] or 0, "amount": r["amount"] or 0}
            for r in rows
        ]
        extra = self.extra_bars.get(symbol)
        if extra and (not bars or extra["date"] > bars[-1]["date"]):
            bars.append(dict(extra))
        return bars

    def stock_basic(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol}

    def latest_daily_basic(self, symbols: list[str]) -> list[dict[str, Any]]:
        """每个标的最新一条 daily_basic 估值快照（pe_ttm/pb/turnover_rate）。"""
        holders = ", ".join("?" for _ in symbols)
        with Warehouse(self.db_path) as warehouse:
            return warehouse.query(
                f"SELECT symbol, date, pe_ttm, pb, turnover_rate FROM daily_basic "
                f"WHERE symbol IN ({holders}) QUALIFY ROW_NUMBER() OVER ("
                f"PARTITION BY symbol ORDER BY date DESC) = 1",
                list(symbols),
            )

    def pool_ep_series(self, symbols: list[str], days: int = 1900) -> list[float]:
        """池级 EP(1/PE_TTM) 中位水平的升序日频序列（regime 阀门输入）。

        逐日取候选池中 pe_ttm>0 标的的 EP 中位值；当日有效标的不足
        ``VALVE_MIN_POOL`` 时该日记 NaN（阀门函数会跳过无效窗口）。
        ``days`` 为日历日回溯上限（1900 天 ≈ 1250 交易日窗口 + 余量）。
        """
        holders = ", ".join("?" for _ in symbols)
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with Warehouse(self.db_path) as warehouse:
            rows = warehouse.query(
                f"SELECT date, MEDIAN(1.0 / pe_ttm) AS med_ep, "
                f"COUNT(*) AS valid_n FROM daily_basic "
                f"WHERE symbol IN ({holders}) AND pe_ttm > 0 AND date >= ? "
                f"GROUP BY date ORDER BY date",
                [*symbols, cutoff],
            )
        return [
            float(r["med_ep"]) if r["valid_n"] >= VALVE_MIN_POOL else float("nan")
            for r in rows
        ]


def _load_tushare_token() -> str:
    """Token 取自环境变量，缺失时回退解析项目根目录 .env。"""
    token = os.getenv("TUSHARE_TOKEN", "")
    if token:
        return token
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("TUSHARE_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


TUSHARE_API = "http://api.tushare.pro"


def _tushare_daily_by_date(trade_date: str, token: str) -> list[list]:
    """按 trade_date 取全市场日线（一次调用，避免逐票请求触发限频）。"""
    import httpx  # 局部导入：host 与容器均有，手机侧不经过此路径
    resp = httpx.post(TUSHARE_API, json={
        "api_name": "daily",
        "token": token,
        "params": {"trade_date": trade_date},
        "fields": "ts_code,close,vol,amount",
    }, timeout=60)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tushare daily({trade_date}): {data.get('msg')}")
    return (data.get("data") or {}).get("items") or []


def fetch_today_bars(
    symbols: list[str],
    today: date,
    *,
    token: str | None = None,
    retries: int = 2,
) -> dict[str, dict[str, Any]]:
    """tushare 当日收盘补齐（纯内存，不写 daily_price）。

    返回 ``{symbol: {"date","close","volume","amount"}}``，volume 已从手
    换算为股、amount 从千元换算为元（对齐 baostock 口径）。token 缺失、
    接口失败、盘前或非交易日（全市场无当日行）时返回 {}，调用方回退
    T-1 路径。
    """
    token = token or _load_tushare_token()
    if not token:
        return {}
    items: list[list] = []
    for attempt in range(retries + 1):
        try:
            items = _tushare_daily_by_date(
                today.isoformat().replace("-", ""), token)
            break
        except Exception:
            if attempt >= retries:
                return {}
            time.sleep(2)
    wanted = set(symbols)
    bars: dict[str, dict[str, Any]] = {}
    for item in items:
        ts_code, close, vol, amount = (list(item) + [None] * 4)[:4]
        code = (ts_code or "").split(".")[0]
        if code in wanted and close:
            bars[code] = {
                "date": today.isoformat(),
                "close": float(close),
                "volume": float(vol or 0) * 100,    # 手 → 股
                "amount": float(amount or 0) * 1000,  # 千元 → 元
            }
    return bars


def filter_exdiv_jumps(
    today_bars: dict[str, dict[str, Any]],
    prev_closes: dict[str, float],
    threshold: float = 0.11,
) -> dict[str, dict[str, Any]]:
    """除权跳变防护：剔除与前一日前复权收盘偏离超阈值的当日补齐 bar。

    tushare 原始价与 baostock 前复权序列口径不同，除权除息日会出现
    分红量级假跳变，直接污染当日动量/波动/回撤因子。主板涨跌停 ±10%，
    偏离 >11% 即判口径错配，该标的回退 T-1 序列（不注入当日）。
    """
    kept: dict[str, dict[str, Any]] = {}
    for symbol, bar in today_bars.items():
        prev = prev_closes.get(symbol)
        if prev and prev > 0 and abs(bar["close"] / prev - 1) > threshold:
            continue
        kept[symbol] = bar
    return kept


def load_previous_targets(
    targets_dir: str | Path,
    data_as_of: str,
) -> dict[str, float] | None:
    """读取上一批 sim_targets 快照的权重（严格早于 data_as_of 的最新一份）。

    目录不存在、无更早快照或快照损坏时返回 None，调用方按首批处理。
    返回权重不含 CASH（CASH 由 ``apply_no_trade_band`` 重新计算）。
    """
    directory = Path(targets_dir)
    if not directory.is_dir():
        return None
    prior = sorted(
        p for p in directory.glob("*.json")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem < data_as_of
    )
    if not prior:
        return None
    try:
        payload = json.loads(prior[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    weights = payload.get("weights")
    if not isinstance(weights, dict):
        return None
    return {s: w for s, w in weights.items() if s != "CASH"}


def weights_to_candidates(
    weights: dict[str, float],
    *,
    data_as_of: str,
    generated_at: str,
    evidence_hash: str,
    prices: dict[str, float] | None = None,
    strategy_version: str = STRATEGY_VERSION,
) -> list[dict[str, Any]]:
    """纯函数：目标权重 → BUY 候选参数列表（conviction = 相对权重归一）。"""

    picks = {s: w for s, w in weights.items() if s != "CASH" and w > 0}
    if not picks:
        return []
    top = max(picks.values())
    prices = prices or {}
    return [
        {
            "generated_at": generated_at,
            "data_as_of": data_as_of,
            "strategy_version": strategy_version,
            "symbol": symbol,
            "side": "BUY",
            "conviction": round(weight / top, 4),
            "evidence_hash": evidence_hash,
            "source": "FACTOR_MODEL",
            "decision_price": prices.get(symbol),
        }
        for symbol, weight in sorted(picks.items(), key=lambda kv: -kv[1])
    ]


def freeze_stamp(db_path: str | Path) -> dict[str, Any]:
    """预注册冻结戳记（2026-08-13 新增，只加不改）。

    返回活跃冻结的 ``freeze_id`` / ``freeze_date``；表不存在或无冻结
    （前冻结时代）返回 None 值，fail-open 不报错，不影响候选生成。
    """

    freeze = StrategyFreezeStore(db_path=db_path).get_active_freeze()
    if not freeze:
        return {"freeze_id": None, "freeze_date": None}
    raw = freeze["freeze_date"]
    return {
        "freeze_id": str(freeze["freeze_id"]),
        "freeze_date": raw.isoformat() if hasattr(raw, "isoformat") else str(raw),
    }


def generate_daily_candidates(
    *,
    db_path: str | Path | None = None,
    today: date | None = None,
    force: bool = False,
    targets_dir: str | Path = "reports/sim_targets",
) -> dict[str, Any]:
    """收盘后运行：校验数据新鲜度 → 因子打分 → 写入候选存储。

    幂等: 同一 data_as_of + strategy_version 已生成过且未传 force 时,
    返回 ALREADY_GENERATED 不重复写(generated_at 参与内容哈希,
    不拦截会产生同批多份候选)。
    """

    db = Path(db_path or os.getenv("DUCKDB_PATH", "") or "data/finance.duckdb")
    today = today or datetime.now(timezone.utc).date()

    with Warehouse(db) as warehouse:
        row = warehouse.query(
            "SELECT MAX(date) AS max_date FROM daily_price"
        )[0]
        symbol_rows = warehouse.query(
            "SELECT symbol, MAX(date) AS latest FROM daily_price GROUP BY symbol"
        )
    max_date = row["max_date"]
    if max_date is None:
        return {"status": "NO_DATA", "candidates": [],
                "production_change_allowed": False,
                "automatic_trade_allowed": False}
    # 可交易候选宇宙: 仅 6 位 A 股代码(剔除港股/指数等不可模拟标的),
    # 且逐标的新鲜度(2026-08-11 审查发现: 旧口径全库 MAX 门禁下,
    # 陈旧港股与指数仍参与横截面排名, 污染候选百分位)。
    symbols = select_candidate_universe(symbol_rows, max_date)
    data_as_of = max_date.isoformat() if hasattr(max_date, "isoformat") else str(max_date)

    # T 日收盘补齐：DB 尚无当日数据时尝试 tushare（纯内存注入，不写库）；
    # 补齐成功则 data_as_of 前移到今天，信号-执行间隔从两个交易日缩到隔夜。
    today_bars: dict[str, dict[str, Any]] = {}
    if data_as_of < today.isoformat():
        today_bars = fetch_today_bars(symbols, today)
        if today_bars:
            # 除权跳变防护：剔除与 T-1 前复权收盘口径错配的当日 bar
            with Warehouse(db) as warehouse:
                prev_closes = {
                    r["symbol"]: r["close"]
                    for r in warehouse.query(
                        "SELECT symbol, close FROM daily_price QUALIFY "
                        "ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) = 1"
                    )
                }
            dropped = sorted(set(today_bars) - set(
                filter_exdiv_jumps(today_bars, prev_closes)))
            if dropped:
                today_bars = filter_exdiv_jumps(today_bars, prev_closes)
                print(f"[daily_candidates] 除权跳变防护剔除 {len(dropped)} 只: "
                      f"{dropped}")
            if today_bars:
                data_as_of = today.isoformat()

    if not force:
        with Warehouse(db) as warehouse:
            existing = warehouse.query(
                "SELECT COUNT(*) AS count FROM strategy_candidate "
                "WHERE data_as_of = ? AND strategy_version = ?",
                [data_as_of, STRATEGY_VERSION],
            )[0]["count"]
        if existing:
            return {
                "status": "ALREADY_GENERATED",
                "data_as_of": data_as_of,
                "existing_candidates": existing,
                "candidates": [],
                "production_change_allowed": False,
                "automatic_trade_allowed": False,
            }

    age_days = (today - date.fromisoformat(data_as_of)).days
    if age_days > MAX_DATA_AGE_DAYS:
        return {
            "status": "STALE_DATA",
            "data_as_of": data_as_of,
            "age_days": age_days,
            "candidates": [],
            "reason": f"daily_price 最新日期 {data_as_of}，距今天 {age_days} 天，"
                      f"超过容忍度 {MAX_DATA_AGE_DAYS} 天，fail-closed 不生成候选",
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    adapter = DuckDBPriceAdapter(db, extra_bars=today_bars)
    engine = FactorEngine(adapter)
    engine.compute_all(symbols)
    result = engine.generate_weights()
    weights = result.get("weights", {})

    # no_trade_band 缓冲带(v3_sim.5): 与上一批 targets 快照对比,带内
    # 标的沿用上一批权重(不发调仓信号),出带才进出,抑制日度换手磨损。
    prev_weights = load_previous_targets(targets_dir, data_as_of)
    weights, band_kept = apply_no_trade_band(
        weights, prev_weights, engine.config.no_trade_band)
    result.setdefault("diagnostics", {})["no_trade_band_kept"] = band_kept

    # 池级 EP 便宜度阀门(multifactor_v5_valve.1, 预注册冻结
    # sf_674cc7bd2cf24f73443d): 候选池 EP 中位水平对自身 1250 交易日
    # 窗口的时序分位 → 股票预算(EP 高=便宜→加仓, 贵→减仓至 floor)。
    # 观测不足时跳过阀门、沿用内核原始权重(fail-open 但记诊断不静默)。
    if STRATEGY_VERSION.startswith("multifactor_v5_valve"):
        budget, ep_pct = pool_ep_ts_budget(adapter.pool_ep_series(symbols))
        if budget is not None:
            weights = apply_regime_valve(weights, budget)
            result["diagnostics"]["regime_valve"] = {
                "budget": round(budget, 4),
                "ep_pct": round(ep_pct, 4),
            }
        else:
            result["diagnostics"]["regime_valve"] = {
                "status": "VALVE_INACTIVE_INSUFFICIENT_HISTORY",
            }

    # 决策价表：模拟盘调仓(sim_rebalance.py)的唯一价格依据，
    # 取每只入选票因子序列的最后一根收盘（当日补齐或 T-1）。
    prices: dict[str, float] = {}
    for sym in weights:
        if sym == "CASH":
            continue
        bars = adapter.daily_bars([sym])
        if bars and bars[-1]["close"]:
            prices[sym] = bars[-1]["close"]

    # 预注册冻结戳记：写进候选 evidence 负载与 targets 顶层元数据，
    # 标记本批候选归属的冻结口径；无冻结时 None（前冻结时代，fail-open）。
    stamp = freeze_stamp(db)

    evidence_payload = {
        "data_as_of": data_as_of,
        "weights": weights,
        "prices": prices,
        "today_fill": len(today_bars),
        "diagnostics": result.get("diagnostics", {}),
        "universe_size": len(symbols),
        "freeze_id": stamp["freeze_id"],
        "freeze_date": stamp["freeze_date"],
    }
    evidence_hash = hashlib.sha256(
        json.dumps(evidence_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat()

    candidates = weights_to_candidates(
        weights,
        data_as_of=data_as_of,
        generated_at=generated_at,
        evidence_hash=evidence_hash,
        prices=prices,
    )
    store = StrategyCandidateStore(db_path=db)
    recorded = []
    for cand in candidates:
        res = store.record_candidate(**cand)
        recorded.append({
            "symbol": cand["symbol"],
            "conviction": cand["conviction"],
            "record_status": res.get("status"),
            "candidate_id": res.get("candidate_id"),
        })

    # 目标权重快照: 模拟盘调仓模块(sim_rebalance.py)的唯一权重来源
    targets_dir = Path(targets_dir)
    targets_dir.mkdir(parents=True, exist_ok=True)
    (targets_dir / f"{data_as_of}.json").write_text(
        json.dumps({
            "data_as_of": data_as_of,
            "generated_at": generated_at,
            "strategy_version": STRATEGY_VERSION,
            "weights": weights,
            "prices": prices,
            "today_fill": len(today_bars),
            "evidence_hash": evidence_hash,
            "freeze_id": stamp["freeze_id"],
            "freeze_date": stamp["freeze_date"],
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    return {
        "status": "OK",
        "data_as_of": data_as_of,
        "generated_at": generated_at,
        "strategy_version": STRATEGY_VERSION,
        "universe_size": len(symbols),
        "today_fill": len(today_bars),
        "evidence_hash": evidence_hash,
        "candidates": recorded,
        "note": "仅供模拟观察期执行与回归训练；基本面因子为价格代理，无 KEEP 裁决",
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
    }


if __name__ == "__main__":
    from foundf_db.runtime_scheduler import runtime_write_lock

    data_root = Path(os.getenv("FOUNDF_DATA_ROOT", "data"))
    with runtime_write_lock(data_root) as acquired:
        if not acquired:
            print(json.dumps({"status": "LOCK_BUSY"}, ensure_ascii=False))
            raise SystemExit(1)
        out = generate_daily_candidates()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["status"] == "OK" else 1)
