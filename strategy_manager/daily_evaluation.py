"""成熟多因子基准的每日证据、费用、价值与收益评估。

本模块不计算交易指令，也不自动修正生产权重。它只把当天能证明和不能证明的
事项写成结构化报告，为后续 Walk-Forward、模拟观察和人工审批提供依据。
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from foundf_db import Warehouse

from .evolution import (
    EvolutionPolicy,
    StrategyEvolutionGovernor,
    _latest_walk_forward,
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def load_baseline(path: str | Path = "config/multifactor_baseline.json") -> dict[str, Any]:
    """加载并严格校验基准策略，避免配置漂移被静默接受。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    weights = raw.get("factor_weights", {})
    required = {"value", "quality", "growth", "momentum", "risk"}
    if set(weights) != required:
        raise ValueError("基准策略必须且只能包含 Value/Quality/Growth/Momentum/Risk")
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
        raise ValueError("基准策略因子权重之和必须为 1")
    for name in ("value", "quality", "growth"):
        requirement = raw["factor_requirements"][name]
        if requirement.get("price_proxy_allowed") is not False:
            raise ValueError(f"{name} 不得使用价格代理")
    return raw


def inspect_fundamental_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    universe_size: int,
    as_of: date,
) -> dict[str, Any]:
    """按公告时点与有效字段评估基本面覆盖；0 值不冒充已知值。"""
    categories = {
        "value": ("pe", "pb", "free_cash_flow"),
        "quality": ("roe", "operating_cf", "debt_ratio", "net_margin"),
        "growth": ("revenue_growth", "profit_growth", "r_and_d_expense"),
    }
    valid_by_category: dict[str, set[str]] = {key: set() for key in categories}
    eligible_symbols: set[str] = set()
    rejected_future = 0
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        filed_at = str(row.get("filed_at") or "")[:10]
        if not symbol or not filed_at:
            continue
        try:
            filed_date = date.fromisoformat(filed_at)
        except ValueError:
            continue
        if filed_date > as_of:
            rejected_future += 1
            continue
        eligible_symbols.add(symbol)
        for category, fields in categories.items():
            # 零值在当前迁移数据中代表缺失；负增长、负现金流仍是有效观测。
            if any(
                (value := _number(row.get(field))) is not None and value != 0
                for field in fields
            ):
                valid_by_category[category].add(symbol)
    denominator = max(int(universe_size), 0)
    coverage = {
        category: (
            round(len(symbols) / denominator, 4) if denominator else None
        )
        for category, symbols in valid_by_category.items()
    }
    complete = set.intersection(*valid_by_category.values()) if valid_by_category else set()
    return {
        "universe_size": denominator,
        "rows_with_valid_filed_at": len(eligible_symbols),
        "future_rows_rejected": rejected_future,
        "category_valid_symbols": {
            key: len(value) for key, value in valid_by_category.items()
        },
        "category_coverage": coverage,
        "complete_symbols": len(complete),
        "complete_coverage": (
            round(len(complete) / denominator, 4) if denominator else None
        ),
        "missing_values_are_not_zero_filled": True,
    }


def evaluate_costs(
    *,
    turnover: float | None,
    scenarios_bps: Mapping[str, Any],
    historical_traded_amount: float | None = None,
    historical_fees: float | None = None,
) -> dict[str, Any]:
    """把成本假设和历史实付成本分开，缺失时返回 null 而非 0。"""
    scenario_drag = {
        name: (
            round(turnover * float(bps) / 10000, 6)
            if turnover is not None
            else None
        )
        for name, bps in scenarios_bps.items()
    }
    actual_rate = (
        historical_fees / historical_traded_amount
        if historical_fees is not None
        and historical_traded_amount is not None
        and historical_traded_amount > 0
        else None
    )
    return {
        "turnover": turnover,
        "turnover_definition": "年度单边成交额/平均净资产",
        "estimated_annual_cost_drag": scenario_drag,
        "historical": {
            "traded_amount": historical_traded_amount,
            "fees": historical_fees,
            "fee_rate": round(actual_rate, 8) if actual_rate is not None else None,
            "scope": "已导入成交记录；不含无法观测的市场冲击成本",
        },
        "assumption_warning": "成本情景不是券商实际报价，市场流动性变化会改变滑点与冲击成本。",
    }


def _historical_costs(path: Path) -> tuple[float | None, float | None]:
    if not path.exists():
        return None, None
    traded = 0.0
    fees = 0.0
    valid_rows = 0
    fee_fields = (
        "raw_commission", "raw_stamp_duty", "raw_levy", "raw_trading_fee",
        "raw_system_fee", "raw_settlement_fee", "raw_other_fee", "raw_transfer_fee",
    )
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("normalized_event_type") not in {"BUY", "SELL"}:
                    continue
                amount = _number(row.get("raw_total_amount"))
                if amount is None:
                    continue
                valid_rows += 1
                traded += abs(amount)
                fees += sum(abs(_number(row.get(field)) or 0.0) for field in fee_fields)
    except (OSError, csv.Error):
        return None, None
    return (
        round(traded, 2) if valid_rows else None,
        round(fees, 2) if valid_rows else None,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _fundamental_rows(db_path: Path, as_of: date) -> tuple[list[dict[str, Any]], int]:
    if not db_path.exists():
        return [], 0
    warehouse = Warehouse(db_path)
    try:
        rows = warehouse.query(
            "SELECT * FROM financial_statement WHERE filed_at IS NOT NULL "
            "AND filed_at <= ?",
            [as_of.isoformat()],
        )
        basic = warehouse.query("SELECT COUNT(*) AS count FROM stock_basic")
        universe = int(basic[0]["count"]) if basic else 0
        return rows, universe
    except Exception:
        return [], 0
    finally:
        warehouse.close()


def evaluate_daily(
    *,
    baseline: Mapping[str, Any],
    research: Mapping[str, Any],
    walk_forward: Mapping[str, Any] | None,
    fundamental_rows: Iterable[Mapping[str, Any]],
    universe_size: int,
    as_of: date,
    historical_traded_amount: float | None = None,
    historical_fees: float | None = None,
    evolution_policy: EvolutionPolicy | None = None,
) -> dict[str, Any]:
    """生成一次纯计算评估，便于黄金数据测试与独立复算。"""
    policy = evolution_policy or EvolutionPolicy()
    governor = StrategyEvolutionGovernor(policy)
    governance = governor.evaluate(
        factor_research=research,
        walk_forward=walk_forward,
        candidate_config={"factor_weights": baseline["factor_weights"]},
        as_of=as_of,
    )
    fundamentals = inspect_fundamental_rows(
        fundamental_rows, universe_size=universe_size, as_of=as_of
    )
    minimum_coverage = float(
        baseline["data_gates"]["minimum_fundamental_coverage"]
    )
    fundamental_ready = (
        fundamentals["complete_coverage"] is not None
        and fundamentals["complete_coverage"] >= minimum_coverage
    )
    backtest = governance["backtest_gate"]
    costs = evaluate_costs(
        turnover=_number(backtest.get("turnover")),
        scenarios_bps=baseline["cost_policy"]["round_trip_bps_scenarios"],
        historical_traded_amount=historical_traded_amount,
        historical_fees=historical_fees,
    )
    returns = {
        "annual_return": _number(
            (walk_forward or {}).get("summary", {}).get("avg_annual_return")
        ),
        "excess_return": backtest.get("avg_excess_return"),
        "sharpe": backtest.get("avg_sharpe"),
        "max_drawdown": backtest.get("max_drawdown"),
        "positive_window_ratio": backtest.get("positive_window_ratio"),
        "source": "Walk-Forward 样本外汇总",
        "available": bool((walk_forward or {}).get("windows")),
    }
    repairs: list[dict[str, Any]] = []
    if not fundamental_ready:
        repairs.append({
            "priority": "P0",
            "code": "REPAIR_POINT_IN_TIME_FUNDAMENTALS",
            "action": "补齐带 filed_at 的真实估值、质量和成长字段，并重跑因子 IC",
            "reason": "基本面完整覆盖不足，禁止用价格代理替代",
            "automatic_change": False,
        })
    if not governance["data_gate"]["passed"]:
        repairs.append({
            "priority": "P0",
            "code": "REPAIR_RESEARCH_DATA",
            "action": "刷新行情并扩大研究股票池、样本期和有效因子期数",
            "reason": "因子研究数据门未通过",
            "automatic_change": False,
        })
    if not governance["backtest_gate"]["passed"]:
        repairs.append({
            "priority": "P1",
            "code": "REPAIR_WALK_FORWARD",
            "action": "用真实成交成本、基准和样本外窗口运行 Walk-Forward",
            "reason": "当前没有合格的样本外收益与成本证据",
            "automatic_change": False,
        })
    status = governance["stage"]
    if not fundamental_ready and status not in {"BLOCKED_DATA", "BLOCKED_FACTORS"}:
        status = "BLOCKED_FACTORS"
    return {
        "schema_version": "foundf.multifactor_daily_evaluation.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "status": status,
        "production_change_allowed": False,
        "automatic_trade_allowed": False,
        "baseline": {
            "strategy_id": baseline["strategy_id"],
            "version": baseline["version"],
            "factor_weights": dict(baseline["factor_weights"]),
            "config_is_versioned": True,
        },
        "market_regime_note": (
            "历史关系不被假定为未来规律；每日检查数据时效、因子稳定性、"
            "样本外表现、换手成本和回撤，市场结构变化只触发复核。"
        ),
        "governance": governance,
        "fundamentals": {
            **fundamentals,
            "minimum_required_coverage": minimum_coverage,
            "passed": fundamental_ready,
        },
        "costs": costs,
        "returns": returns,
        "repairs": repairs,
        "disclaimer": "仅供投资决策支持，不构成自动交易指令，不承诺收益。",
    }


def _to_markdown(result: Mapping[str, Any]) -> str:
    weights = result["baseline"]["factor_weights"]
    f = result["fundamentals"]
    c = result["costs"]
    r = result["returns"]
    repairs = result["repairs"]
    lines = [
        f"# 多因子策略每日评估 — {result['as_of']}",
        "",
        f"- 状态：`{result['status']}`",
        f"- 基准版本：`{result['baseline']['version']}`",
        "- 生产权重自动变更：禁止",
        "- 自动交易：禁止",
        "",
        "## 基准权重",
        "",
        "| Value | Quality | Growth | Momentum | Risk |",
        "|---:|---:|---:|---:|---:|",
        "| " + " | ".join(f"{weights[key]:.0%}" for key in (
            "value", "quality", "growth", "momentum", "risk"
        )) + " |",
        "",
        "## 每日证据",
        "",
        f"- 基本面完整覆盖：{f['complete_coverage'] if f['complete_coverage'] is not None else '缺失'}"
        f"（门槛 {f['minimum_required_coverage']:.0%}）",
        f"- 年度换手：{c['turnover'] if c['turnover'] is not None else '缺失'}",
        f"- 基准情景年度成本拖累：{c['estimated_annual_cost_drag'].get('base') if c['estimated_annual_cost_drag'].get('base') is not None else '缺失'}",
        f"- 样本外年化收益：{r['annual_return'] if r['annual_return'] is not None else '缺失'}",
        f"- 样本外夏普：{r['sharpe'] if r['sharpe'] is not None else '缺失'}",
        f"- 样本外最大回撤：{r['max_drawdown'] if r['max_drawdown'] is not None else '缺失'}",
        "",
        "## 修复候选",
        "",
    ]
    lines.extend(
        f"- [{item['priority']}] {item['action']}（{item['reason']}）"
        for item in repairs
    )
    lines += ["", f"> {result['disclaimer']}", ""]
    return "\n".join(lines)


def run_daily_evaluation(
    *,
    root: str | Path = ".",
    as_of: date | None = None,
) -> dict[str, Any]:
    """读取当前证据并原子写入每日报告与本地治理状态。"""
    root_path = Path(root)
    day = as_of or date.today()
    baseline = load_baseline(root_path / "config/multifactor_baseline.json")
    policy = EvolutionPolicy.load(root_path / "config/strategy_evolution.json")
    research = _read_json(
        root_path / "reports/factor_research/factor_research.json"
    )
    walk_forward = _latest_walk_forward(root_path / "strategy_report")
    rows, stock_universe = _fundamental_rows(
        root_path / "data/finance.duckdb", day
    )
    research_universe = int(research.get("universe_size", 0) or 0)
    universe_size = max(stock_universe, research_universe)
    traded, fees = _historical_costs(
        root_path / baseline["cost_policy"]["historical_fee_file"]
    )
    result = evaluate_daily(
        baseline=baseline,
        research=research,
        walk_forward=walk_forward,
        fundamental_rows=rows,
        universe_size=universe_size,
        as_of=day,
        historical_traded_amount=traded,
        historical_fees=fees,
        evolution_policy=policy,
    )
    report_dir = root_path / "reports/strategy_daily"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"multifactor_evaluation_{day.isoformat()}.json"
    md_path = report_dir / f"multifactor_evaluation_{day.isoformat()}.md"
    status_path = root_path / "data/governance/multifactor_daily_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    for path, content in (
        (json_path, payload),
        (md_path, _to_markdown(result)),
        (status_path, payload),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return result


if __name__ == "__main__":
    print(json.dumps(run_daily_evaluation(), ensure_ascii=False, indent=2))
