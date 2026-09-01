"""
investment_gate.py — Phase S2: Investment Gate（投资流程控制层）。

定位升级：Discipline Engine（Phase S）是"报告与提醒"，
Investment Gate 是"流程控制"——所有主动交易建议（无论来自 USER /
AI_AGENT / FACTOR_MODEL）在执行前必须过 Gate，且 Gate 结果写入
decision_memory，形成：

    交易前(Gate) → 交易后(记录) → 结果反馈(多周期评估) → 影响下次Gate

八项检查：
    1. 近期交易频率      — 冷静期/冲动状态（discipline_engine）
    2. 重复亏损标的      — 失败记忆库（failure_feedback）
    3. 追涨检测          — 该标的历史失败类型 + 近期短时往返
    4. 估值位置          — 价值因子分（factor_engine，数据缺失则 SKIP）
    5. 因子排名          — 综合因子分（factor_engine，数据缺失则 SKIP）
    6. 风险评分          — risk_engine（不可用时 SKIP，不阻塞）
    7. 历史类似决策结果  — decision_memory 中该标的过往决策的评估正确率
    8. 集中度预算        — 单一标的/行业穿透上限（股票、基金、ETF 合并）

输出：ALLOW / CAUTION / BLOCK + 调整后信心 + 逐项证据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .discipline_engine import DisciplineEngine

if TYPE_CHECKING:
    from .decision_memory import DecisionMemory

#  verdict 阈值
BLOCK_PENALTY = 0.40        # 累计扣分 ≥ 此值 → BLOCK
CAUTION_PENALTY = 0.10      # 累计扣分 ≥ 此值 → CAUTION


def _resolve_market_symbol(symbol: str) -> str:
    """将账本代码映射到 daily_price 的代码格式。

    00700 (HK 账本) → 0700.HK；000333 (A股) → 000333 原样。
    """
    if symbol.endswith(".HK") or symbol.isalpha():
        return symbol
    if symbol.startswith("0") and len(symbol) == 5:
        return f"{symbol[1:]}.HK"
    return symbol


class InvestmentGate:
    """投资交易 Gate — 所有主动交易的统一入口。

    使用方式:
        gate = InvestmentGate()
        result = gate.evaluate("03431", action="BUY", confidence=0.7, source="USER")
        # result["verdict"] in ("ALLOW", "CAUTION", "BLOCK")
        # 结果已自动写入 decision_memory（decision_source=source）
    """

    def __init__(self, duckdb_path: str | Path = "data/finance.duckdb",
                 record_decisions: bool = True):
        from .decision_memory import DecisionMemory

        self.discipline = DisciplineEngine()
        self.memory = DecisionMemory()
        self.duckdb_path = duckdb_path
        self.record_decisions = record_decisions
        self._factor_registry = None  # 惰性加载

    def _get_factor_registry(self):
        if self._factor_registry is None:
            try:
                from foundf_db import Warehouse
                from factor_engine import FactorRegistry
                wh = Warehouse(self.duckdb_path)
                wh.init()
                self._factor_registry = FactorRegistry(wh)
            except Exception:
                self._factor_registry = False  # 不可用
        return self._factor_registry or None

    # ── 单项检查 ─────────────────────────────────────

    def _check_trade_frequency(self) -> dict[str, Any]:
        """检查 1: 近期交易频率（冷静期 + 冲动分）。"""
        cooldown = self.discipline.cooling_off_check()
        impulse = self.discipline.trading_impulse_score()
        if cooldown["cooling_off"]:
            return {"check": "TRADE_FREQUENCY", "result": "WARN", "penalty": 0.15,
                    "detail": f"冷静期激活（冲动分 {impulse['score']}/100）: "
                              f"{cooldown['triggers'][0]}"}
        if impulse["score"] >= 30:
            return {"check": "TRADE_FREQUENCY", "result": "WARN", "penalty": 0.05,
                    "detail": f"冲动分 {impulse['score']}/100 ({impulse['level']})"}
        return {"check": "TRADE_FREQUENCY", "result": "OK", "penalty": 0.0,
                "detail": f"冲动分 {impulse['score']}/100，无冷静期信号"}

    def _check_failure_memory(self, symbol: str) -> dict[str, Any]:
        """检查 2: 重复亏损标的。"""
        from investment_agent.failure_feedback import query_failure_cases
        ev = query_failure_cases(symbol, include_similar=False)
        exact = ev["exact"]
        if exact:
            penalty = {"HIGH": 0.30, "MEDIUM": 0.20, "LOW": 0.10}.get(
                exact["severity"], 0.10)
            return {"check": "REPEAT_LOSER", "result": "WARN", "penalty": penalty,
                    "detail": f"历史净亏损 {exact['loss_amount']:+,.0f}，"
                              f"胜率 {exact['win_rate_pct']:.0f}%，"
                              f"类型 {exact['failure_type']} ({exact['severity']})",
                    "_failure_type": exact["failure_type"]}
        return {"check": "REPEAT_LOSER", "result": "OK", "penalty": 0.0,
                "detail": "无历史亏损记录"}

    def _check_chasing(self, symbol: str,
                       failure_type: str | None) -> dict[str, Any]:
        """检查 3: 追涨检测。"""
        if failure_type == "CHASING_HIGH":
            return {"check": "CHASING", "result": "WARN", "penalty": 0.10,
                    "detail": "该标的历史失败模式正是追涨（CHASING_HIGH），"
                              "本次买入需证明不是同一模式"}
        # 近期是否对该标的有短时往返
        recent = [t for t in self.discipline._txns
                  if t["symbol"] == symbol
                  and (date.today() - t["date"]).days <= 10]
        if len(recent) >= 3:
            return {"check": "CHASING", "result": "WARN", "penalty": 0.10,
                    "detail": f"最近 10 天该标的已交易 {len(recent)} 笔，"
                              "高频进出通常伴随追涨杀跌"}
        return {"check": "CHASING", "result": "OK", "penalty": 0.0,
                "detail": "无追涨信号"}

    def _check_factors(self, symbol: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """检查 4+5: 估值位置 + 因子排名（数据缺失时 SKIP）。"""
        registry = self._get_factor_registry()
        if registry is None:
            skip = {"check": "VALUATION", "result": "SKIP", "penalty": 0.0,
                    "detail": "因子引擎不可用"}
            skip2 = {"check": "FACTOR_RANK", "result": "SKIP", "penalty": 0.0,
                     "detail": "因子引擎不可用"}
            return skip, skip2

        msym = _resolve_market_symbol(symbol)
        try:
            scores = registry.compute_all(msym)
        except Exception:
            scores = {}

        if not scores:
            valuation = {"check": "VALUATION", "result": "SKIP", "penalty": 0.0,
                         "detail": f"无 {msym} 因子数据（数据资产缺口，见 Phase U）"}
            rank = {"check": "FACTOR_RANK", "result": "SKIP", "penalty": 0.0,
                    "detail": "无因子数据"}
            return valuation, rank

        # 口径透明化（2026-08-13 review 任务4）：factor_engine 的
        # value_/quality_ 等因子目前多为价格代理（source='proxy'）。
        # compute_all 以类名为键（ValuePE），需经 get_factor(cls).name
        # 换算为因子名（value_pe）后才能按类别筛选与查口径。
        sources = registry.factor_sources()  # {factor.name: 'real'/'proxy'}
        named_scores: dict[str, float] = {}
        src_of: dict[str, str] = {}
        for cls_name, value in scores.items():
            f = registry.get_factor(cls_name)
            if f is None or not f.name:
                continue
            named_scores[f.name] = value
            src_of[f.name] = sources.get(f.name)

        # 估值位置：价值类因子均值（2026-08-13 根治：此前直接拿类名键
        # 匹配 value_ 前缀恒为空，本检查历史上恒 SKIP）
        value_keys = [k for k in named_scores if k.startswith("value_")]
        if value_keys:
            v = sum(named_scores[k] for k in value_keys) / len(value_keys)
            v_proxy = sum(1 for k in value_keys if src_of.get(k) == "proxy")
            v_note = (f"（{v_proxy}/{len(value_keys)} 为价格代理口径）"
                      if v_proxy else "")
            if v < 0.3:
                valuation = {"check": "VALUATION", "result": "WARN", "penalty": 0.10,
                             "detail": f"估值因子分 {v:.2f}（<0.3），估值位置偏高{v_note}"}
            else:
                valuation = {"check": "VALUATION", "result": "OK", "penalty": 0.0,
                             "detail": f"估值因子分 {v:.2f}{v_note}"}
        else:
            valuation = {"check": "VALUATION", "result": "SKIP", "penalty": 0.0,
                         "detail": "无价值类因子数据（价值因子全部计算失败）"}

        # 因子综合排名
        avg = sum(scores.values()) / len(scores)
        proxy_count = sum(1 for k in named_scores if src_of.get(k) == "proxy")
        rank_note = (f"（含 {proxy_count}/{len(scores)} 个价格代理口径因子）"
                     if proxy_count else "")
        if avg < 0.35:
            rank = {"check": "FACTOR_RANK", "result": "WARN", "penalty": 0.10,
                    "detail": f"综合因子分 {avg:.2f}（<0.35），横截面排名靠后{rank_note}"}
        else:
            rank = {"check": "FACTOR_RANK", "result": "OK", "penalty": 0.0,
                    "detail": f"综合因子分 {avg:.2f}{rank_note}"}
        return valuation, rank

    def _check_risk(self, symbol: str) -> dict[str, Any]:
        """检查 6: 风险评分（risk_engine 不可用时 SKIP，不阻塞流程）。"""
        try:
            from foundf_db import Warehouse, DataProvider
            from risk_engine import RiskEngine
            wh = Warehouse(self.duckdb_path)
            wh.init()
            dp = DataProvider(warehouse=wh)
            engine = RiskEngine(dp)
            msym = _resolve_market_symbol(symbol)
            report = engine.assess_portfolio(
                [{"symbol": msym, "name": "", "market": ""}])
            score = getattr(report, "market_risk", None)
            if score is None:
                raise ValueError("no score")
            if score >= 70:
                return {"check": "RISK_SCORE", "result": "WARN", "penalty": 0.10,
                        "detail": f"风险评分 {score:.0f}/100，处于高位"}
            return {"check": "RISK_SCORE", "result": "OK", "penalty": 0.0,
                    "detail": f"风险评分 {score:.0f}/100"}
        except Exception as e:
            return {"check": "RISK_SCORE", "result": "SKIP", "penalty": 0.0,
                    "detail": f"风险引擎不可用（{type(e).__name__}），不阻塞"}

    def _check_similar_decisions(self, symbol: str) -> dict[str, Any]:
        """检查 7: 历史类似决策结果（decision_memory 中该标的的评估记录）。"""
        entries = [e for e in self.memory._log if e.get("symbol") == symbol]
        evaluated = []
        for e in entries:
            for h in ("30d", "90d", "180d", "365d"):
                v = e.get(f"eval_{h}_correct")
                if v in ("TRUE", "FALSE"):
                    evaluated.append(v == "TRUE")
        if not evaluated:
            return {"check": "SIMILAR_DECISIONS", "result": "SKIP", "penalty": 0.0,
                    "detail": "该标的无已评估的历史决策"}
        acc = sum(evaluated) / len(evaluated)
        if acc < 0.4:
            return {"check": "SIMILAR_DECISIONS", "result": "WARN", "penalty": 0.15,
                    "detail": f"该标的历史 {len(evaluated)} 次已评估决策，"
                              f"正确率仅 {acc:.0%}"}
        return {"check": "SIMILAR_DECISIONS", "result": "OK", "penalty": 0.0,
                "detail": f"该标的历史决策正确率 {acc:.0%}（{len(evaluated)} 次评估）"}

    def _check_concentration(
        self,
        symbol: str,
        action: str,
        proposed_weight: float = 0.0,
        name: str = "",
        sector: str = "",
    ) -> dict[str, Any]:
        """检查 8: 买入后的单一标的和行业集中度。"""
        if action not in ("BUY", "ADD"):
            return {
                "check": "CONCENTRATION_BUDGET",
                "result": "SKIP",
                "penalty": 0.0,
                "detail": "非新增风险交易",
            }
        try:
            from .concentration_guard import ConcentrationGuard, ConcentrationPolicy

            holdings = list(self.discipline._holdings.values())
            check = ConcentrationGuard(ConcentrationPolicy.load()).pre_trade_check(
                holdings=holdings,
                symbol=symbol,
                action=action,
                proposed_weight=proposed_weight,
                name=name,
                sector=sector,
            )
            if check["verdict"] == "BLOCK":
                limits = ", ".join(
                    f"{item['type']} {item['projected']:.1%}>{item['limit']:.1%}"
                    for item in check["violations"]
                )
                return {
                    "check": "CONCENTRATION_BUDGET",
                    "result": "WARN",
                    "penalty": BLOCK_PENALTY,
                    "detail": (
                        f"行业 {check['sector']}，交易后集中度越限：{limits}。"
                        "禁止继续增加该风险桶。"
                    ),
                }
            if check["verdict"] == "CAUTION":
                return {
                    "check": "CONCENTRATION_BUDGET",
                    "result": "WARN",
                    "penalty": CAUTION_PENALTY,
                    "detail": (
                        f"行业 {check['sector']} 接近上限；"
                        f"预计单一标的 {check['projected_single_weight']:.1%}，"
                        f"行业 {check['projected_sector_weight']:.1%}"
                    ),
                }
            return {
                "check": "CONCENTRATION_BUDGET",
                "result": "OK",
                "penalty": 0.0,
                "detail": (
                    f"行业 {check['sector']}；"
                    f"预计单一标的 {check['projected_single_weight']:.1%}，"
                    f"行业 {check['projected_sector_weight']:.1%}"
                ),
            }
        except Exception as exc:
            return {
                "check": "CONCENTRATION_BUDGET",
                "result": "SKIP",
                "penalty": 0.0,
                "detail": f"集中度数据不可用（{type(exc).__name__}）",
            }

    # ── Gate 主流程 ──────────────────────────────────

    def evaluate(self, symbol: str, action: str = "BUY",
                 confidence: float = 0.5, source: str = "USER",
                 name: str = "", proposed_weight: float = 0.0,
                 sector: str = "") -> dict[str, Any]:
        """交易前 Gate 评估，结果写入 decision_memory 形成闭环。

        Args:
            source: USER | AI_AGENT | FACTOR_MODEL | COMBINED（谁提出的交易）
        """
        action = action.upper()
        checks: list[dict[str, Any]] = []

        c1 = self._check_trade_frequency()
        checks.append(c1)

        c2 = self._check_failure_memory(symbol)
        checks.append(c2)

        c3 = self._check_chasing(symbol, c2.get("_failure_type"))
        checks.append(c3)

        if action in ("BUY", "ADD"):
            c4, c5 = self._check_factors(symbol)
            checks.extend([c4, c5])
            checks.append(self._check_risk(symbol))

        c7 = self._check_similar_decisions(symbol)
        checks.append(c7)

        checks.append(
            self._check_concentration(
                symbol, action, proposed_weight, name=name, sector=sector
            )
        )

        # 卖出走纪律引擎的 SELL 检查补充
        if action in ("SELL", "REDUCE"):
            sell_check = self.discipline.pre_trade_check(symbol, action, confidence)
            for r in sell_check["reasons"]:
                if r["result"] == "WARN":
                    checks.append({"check": f"SELL_{r['check']}", "result": "WARN",
                                   "penalty": 0.10, "detail": r["detail"]})

        total_penalty = round(sum(c["penalty"] for c in checks), 2)
        adjusted = max(0.0, round(confidence - total_penalty, 2))
        warn_count = sum(1 for c in checks if c["result"] == "WARN")
        # fail-open 防护：基础设施故障型 SKIP（引擎/数据「不可用」）≥2 时
        # 不得 ALLOW——八项检查多项失效仍放行与手机执行器 fail-closed 口径矛盾
        infra_skips = [
            c for c in checks
            if c["result"] == "SKIP" and "不可用" in c["detail"]
        ]

        if total_penalty >= BLOCK_PENALTY or warn_count >= 3:
            verdict = "BLOCK"
        elif total_penalty >= CAUTION_PENALTY or len(infra_skips) >= 2:
            verdict = "CAUTION"
        else:
            verdict = "ALLOW"

        result = {
            "symbol": symbol,
            "action": action,
            "source": source,
            "verdict": verdict,
            "original_confidence": confidence,
            "adjusted_confidence": adjusted,
            "total_penalty": total_penalty,
            "infra_skip_count": len(infra_skips),
            "checks": [{k: v for k, v in c.items() if not k.startswith("_")}
                       for c in checks],
            "date": date.today().isoformat(),
        }

        # 闭环：写入 decision_memory（BLOCK 也记录——被拒决策同样值得跟踪）
        if self.record_decisions:
            warn_summary = "; ".join(
                f"{c['check']}" for c in checks if c["result"] == "WARN") or "无警告"
            try:
                self.memory.record(
                    date_str=result["date"],
                    symbol=symbol,
                    name=name,
                    action=action,
                    reason=f"GATE:{verdict} | {warn_summary}",
                    confidence=adjusted,
                    decision_source=source,
                )
                result["recorded"] = True
            except Exception as e:
                result["recorded"] = False
                result["record_error"] = str(e)

        return result


# ── CLI ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m portfolio_manager.investment_gate "
              "<symbol> [BUY|SELL] [confidence] [source] [proposed_weight]")
        print("示例: python -m portfolio_manager.investment_gate "
              "03431 BUY 0.7 USER 0.02")
        sys.exit(1)

    symbol = sys.argv[1]
    action = sys.argv[2] if len(sys.argv) > 2 else "BUY"
    conf = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    source = sys.argv[4] if len(sys.argv) > 4 else "USER"
    proposed_weight = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

    gate = InvestmentGate()
    r = gate.evaluate(
        symbol, action, conf, source, proposed_weight=proposed_weight
    )

    icon = {"ALLOW": "✅", "CAUTION": "🟡", "BLOCK": "⛔"}[r["verdict"]]
    print(f"\n{icon} Investment Gate: {symbol} {action} → {r['verdict']}")
    print(f"   信心: {r['original_confidence']} → {r['adjusted_confidence']} "
          f"(扣分 -{r['total_penalty']})")
    print(f"   决策来源: {r['source']} | 已写入 decision_memory: {r.get('recorded')}")
    print()
    for c in r["checks"]:
        mark = {"OK": "✅", "WARN": "⚠️", "SKIP": "➖"}[c["result"]]
        print(f"  {mark} [{c['check']}] {c['detail']}")

    sys.exit(0 if r["verdict"] != "BLOCK" else 2)
