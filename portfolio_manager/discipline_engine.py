"""
discipline_engine.py — Phase S: Investment Discipline Engine.

将个人行为分析结果（Phase Q/R）反馈为投资决策约束。

核心发现驱动：历史行为（55/56 标的短线、平均持仓 6.9 天、短线 P&L -68,264）
与长期目标（长期投资、多因子、稳定收益）不一致。
本模块不"劝说"，而是把行为偏差变成交易前的硬性检查。

功能：
1. Trading Impulse Score  — 检测交易冲动（频率突增、短时往返、同标的反复）
2. pre_trade_check()      — 买入前的长期模型守门（失败记忆 + 冲动状态 + 持仓周期）
3. cooling_off_check()    — 交易冷静期（识别可能的情绪交易）

输出: reports/discipline_report.md

数据源（全部为本地已沉淀资产，无外部 API 依赖）:
  - reports/reconciliation/full_parsed_transactions_v4.csv  逐笔交易
  - data/failure_memory/failure_cases.csv                   失败案例库
  - reports/reconciliation/portfolio_holdings_latest.csv    当前持仓
  - reports/reconciliation/behavior_analysis.json           行为画像
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPORTS = Path("reports/reconciliation")
FAILURE_DB = Path("data/failure_memory")
OUT_DIR = Path("reports")

# ── 纪律阈值（基于个人历史数据校准，勿随意放宽）────────
IMPULSE_WINDOW_DAYS = 30        # 冲动检测窗口
FREQ_SURGE_RATIO = 1.5          # 交易频率超过生涯基线倍数 → 冲动信号
QUICK_ROUNDTRIP_DAYS = 5        # 买卖间隔 ≤ N 天 → 短时往返
REPEAT_SYMBOL_TRADES = 3        # 窗口内同标的交易 ≥ N 笔 → 反复操作
COOLDOWN_TRADES_5D = 4          # 最近 5 天交易 ≥ N 笔 → 冷静期
COOLDOWN_SYMBOL_10D = 3         # 最近 10 天同标的 ≥ N 笔 → 冷静期


class DisciplineEngine:
    """投资纪律引擎 — 把历史行为偏差变成交易前约束。"""

    def __init__(self):
        self._txns = self._load_transactions()
        self._failures = self._load_failure_cases()
        self._holdings = self._load_holdings()
        self._behavior = self._load_behavior_profile()

    # ── 数据加载 ─────────────────────────────────────

    def _load_transactions(self) -> list[dict[str, Any]]:
        p = REPORTS / "full_parsed_transactions_v4.csv"
        if not p.exists():
            return []
        with open(p, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        txns = []
        for r in rows:
            if r.get("normalized_event_type") not in ("BUY", "SELL"):
                continue
            raw_date = r.get("trade_date", "")
            try:
                d = datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                continue
            txns.append({
                "date": d,
                "symbol": r.get("raw_symbol", ""),
                "name": r.get("raw_name", ""),
                "side": r["normalized_event_type"],
                "qty": float(r.get("raw_quantity", 0) or 0),
                "price": float(r.get("raw_price", 0) or 0),
                "amount": abs(float(r.get("raw_total_amount", 0) or 0)),
                "market": r.get("market_type", ""),
            })
        txns.sort(key=lambda t: t["date"])
        return txns

    def _load_failure_cases(self) -> dict[str, dict[str, Any]]:
        p = FAILURE_DB / "failure_cases.csv"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8-sig") as f:
            return {r["symbol"]: r for r in csv.DictReader(f)}

    def _load_holdings(self) -> dict[str, dict[str, Any]]:
        p = REPORTS / "portfolio_holdings_latest.csv"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8-sig") as f:
            return {r["symbol"]: r for r in csv.DictReader(f)}

    def _load_behavior_profile(self) -> dict[str, Any]:
        p = REPORTS / "behavior_analysis.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    # ── 1. 交易冲动评分 ──────────────────────────────

    def trading_impulse_score(self, window_days: int = IMPULSE_WINDOW_DAYS,
                              as_of: date | None = None) -> dict[str, Any]:
        """计算 Trading Impulse Score (0-100，越高越冲动)。"""
        if not self._txns:
            return {"score": 0, "signals": [], "note": "无交易数据"}

        as_of = as_of or max(t["date"] for t in self._txns)
        window_start = as_of - timedelta(days=window_days)

        window_txns = [t for t in self._txns if window_start <= t["date"] <= as_of]
        signals: list[dict[str, Any]] = []
        score = 0

        # 信号 1：交易频率突增（窗口内频率 vs 生涯基线）
        first_date = self._txns[0]["date"]
        lifetime_days = max((as_of - first_date).days, 1)
        baseline_per_week = len(self._txns) / lifetime_days * 7
        window_per_week = len(window_txns) / window_days * 7
        freq_ratio = window_per_week / baseline_per_week if baseline_per_week > 0 else 0

        if freq_ratio >= FREQ_SURGE_RATIO * 1.33:  # ≥2x
            pts = 30
        elif freq_ratio >= FREQ_SURGE_RATIO:
            pts = 20
        elif freq_ratio >= 1.2:
            pts = 10
        else:
            pts = 0
        if pts:
            signals.append({
                "signal": "FREQUENCY_SURGE",
                "points": pts,
                "detail": f"近{window_days}天交易 {len(window_txns)} 笔 "
                          f"({window_per_week:.1f}/周)，生涯基线 {baseline_per_week:.1f}/周 "
                          f"(倍数 {freq_ratio:.1f}x)",
            })
            score += pts

        # 信号 2：短时往返（窗口内同标的买后 N 天内卖出）
        roundtrips = self._detect_quick_roundtrips(window_txns)
        if roundtrips:
            pts = min(len(roundtrips) * 10, 30)
            signals.append({
                "signal": "QUICK_ROUNDTRIP",
                "points": pts,
                "detail": f"{len(roundtrips)} 次 ≤{QUICK_ROUNDTRIP_DAYS} 天往返: "
                          + ", ".join(f"{r['symbol']}({r['hold_days']}d)" for r in roundtrips[:5]),
            })
            score += pts

        # 信号 3：同标的反复操作
        by_symbol: dict[str, int] = defaultdict(int)
        for t in window_txns:
            by_symbol[t["symbol"]] += 1
        repeated = {s: n for s, n in by_symbol.items() if n >= REPEAT_SYMBOL_TRADES}
        if repeated:
            pts = min(len(repeated) * 5, 20)
            signals.append({
                "signal": "REPEAT_TRADING",
                "points": pts,
                "detail": f"{len(repeated)} 个标的窗口内交易 ≥{REPEAT_SYMBOL_TRADES} 笔: "
                          + ", ".join(f"{s}({n}笔)" for s, n in list(repeated.items())[:5]),
            })
            score += pts

        # 信号 4：窗口内买入失败记忆库中的标的（重复历史错误）
        repeat_failures = []
        for t in window_txns:
            if t["side"] == "BUY" and t["symbol"] in self._failures:
                fc = self._failures[t["symbol"]]
                repeat_failures.append(
                    f"{t['symbol']}(曾亏损{float(fc['loss_amount']):+,.0f}, "
                    f"类型 {fc['failure_type']})"
                )
        if repeat_failures:
            pts = min(len(repeat_failures) * 10, 20)
            signals.append({
                "signal": "REPEATING_PAST_FAILURE",
                "points": pts,
                "detail": f"窗口内买入了 {len(repeat_failures)} 个失败记忆库标的: "
                          + ", ".join(repeat_failures[:5]),
            })
            score += pts

        score = min(score, 100)
        level = ("HIGH" if score >= 60 else "ELEVATED" if score >= 30
                 else "NORMAL" if score > 0 else "CALM")

        return {
            "score": score,
            "level": level,
            "window_days": window_days,
            "as_of": as_of.isoformat(),
            "window_trades": len(window_txns),
            "baseline_trades_per_week": round(baseline_per_week, 2),
            "window_trades_per_week": round(window_per_week, 2),
            "freq_ratio": round(freq_ratio, 2),
            "signals": signals,
        }

    def _detect_quick_roundtrips(self, txns: list[dict]) -> list[dict[str, Any]]:
        """检测窗口内的短时往返（买后 ≤N 天卖出）。"""
        roundtrips = []
        open_buys: dict[str, list[dict]] = defaultdict(list)
        for t in txns:
            if t["side"] == "BUY":
                open_buys[t["symbol"]].append(t)
            elif t["side"] == "SELL" and open_buys.get(t["symbol"]):
                buy = open_buys[t["symbol"]].pop(0)
                hold_days = (t["date"] - buy["date"]).days
                if hold_days <= QUICK_ROUNDTRIP_DAYS:
                    roundtrips.append({
                        "symbol": t["symbol"],
                        "buy_date": buy["date"].isoformat(),
                        "sell_date": t["date"].isoformat(),
                        "hold_days": hold_days,
                    })
        return roundtrips

    # ── 2. 交易冷静期 ────────────────────────────────

    def cooling_off_check(self, as_of: date | None = None) -> dict[str, Any]:
        """检测当前是否处于需要冷静的状态（可能的情绪交易）。"""
        if not self._txns:
            return {"cooling_off": False, "triggers": []}

        as_of = as_of or max(t["date"] for t in self._txns)
        triggers = []

        # 触发 1：最近 5 天交易笔数
        recent_5d = [t for t in self._txns if (as_of - t["date"]).days <= 5]
        if len(recent_5d) >= COOLDOWN_TRADES_5D:
            triggers.append(
                f"最近 5 天交易 {len(recent_5d)} 笔（阈值 {COOLDOWN_TRADES_5D}）— "
                f"短期频繁交易是历史亏损主因之一"
            )

        # 触发 2：最近 10 天同标的反复
        recent_10d = [t for t in self._txns if (as_of - t["date"]).days <= 10]
        by_symbol: dict[str, int] = defaultdict(int)
        for t in recent_10d:
            by_symbol[t["symbol"]] += 1
        for sym, n in by_symbol.items():
            if n >= COOLDOWN_SYMBOL_10D:
                triggers.append(
                    f"{sym} 最近 10 天交易 {n} 笔 — 反复操作同一标的通常意味着情绪化决策"
                )

        # 触发 3：行为画像中的高风险弱点被再次触发
        weaknesses = self._behavior.get("weaknesses", [])
        if weaknesses and len(recent_5d) >= 2:
            triggers.append(
                f"行为画像已识别弱点: {'; '.join(weaknesses[:2])} — 当前交易活跃，需警惕重复"
            )

        return {
            "cooling_off": bool(triggers),
            "as_of": as_of.isoformat(),
            "trades_last_5d": len(recent_5d),
            "triggers": triggers,
            "advice": (
                "建议暂停新开仓 48 小时，仅复核现有持仓。"
                if triggers else "当前无情绪交易信号。"
            ),
        }

    # ── 3. 交易前守门（Pre-trade Discipline Gate）─────

    def pre_trade_check(self, symbol: str, action: str = "BUY",
                        confidence: float = 0.5,
                        as_of: date | None = None) -> dict[str, Any]:
        """买入/卖出前的纪律检查。

        这是长期模型保护机制：任何 BUY 建议（无论来自 AI、量化模型还是人工）
        在执行前都应通过此门。

        Returns:
            verdict: PASS | CAUTION | BLOCK
            adjusted_confidence: 纪律调整后的信心值
            reasons: 每条检查的结果
        """
        as_of = as_of or (max(t["date"] for t in self._txns) if self._txns else date.today())
        reasons: list[dict[str, str]] = []
        penalty = 0.0
        action = action.upper()

        # 检查 1：失败记忆（该标的是否亏过钱、怎么亏的）
        fc = self._failures.get(symbol)
        if fc:
            sev = fc.get("severity", "LOW")
            loss = float(fc.get("loss_amount", 0))
            win_rate = float(fc.get("win_rate", 0))
            ftype = fc.get("failure_type", "UNKNOWN")
            if sev == "HIGH":
                penalty += 0.30
            elif sev == "MEDIUM":
                penalty += 0.20
            else:
                penalty += 0.10
            reasons.append({
                "check": "FAILURE_MEMORY",
                "result": "WARN",
                "detail": f"该标的历史净亏损 {loss:+,.0f}，胜率 {win_rate:.0f}%，"
                          f"失败类型 {ftype}（{fc.get('failure_description', '')}）。"
                          f"本次买入是否与历史错误模式相同？",
            })
        else:
            reasons.append({
                "check": "FAILURE_MEMORY",
                "result": "OK",
                "detail": "该标的无历史失败记录。",
            })

        # 检查 2：近期是否已频繁交易该标的
        sym_txns = [t for t in self._txns
                    if t["symbol"] == symbol and (as_of - t["date"]).days <= 10]
        if action in ("BUY", "ADD") and len(sym_txns) >= COOLDOWN_SYMBOL_10D:
            penalty += 0.15
            reasons.append({
                "check": "RECENT_ACTIVITY",
                "result": "WARN",
                "detail": f"最近 10 天已交易该标的 {len(sym_txns)} 笔 — "
                          f"反复操作同一标的是冲动交易的典型特征。",
            })
        else:
            reasons.append({
                "check": "RECENT_ACTIVITY",
                "result": "OK",
                "detail": f"最近 10 天该标的交易 {len(sym_txns)} 笔。",
            })

        # 检查 3：全局冷静期状态
        cooldown = self.cooling_off_check(as_of)
        if cooldown["cooling_off"]:
            penalty += 0.10
            reasons.append({
                "check": "COOLING_OFF",
                "result": "WARN",
                "detail": f"当前处于冷静期: {cooldown['triggers'][0]}",
            })
        else:
            reasons.append({
                "check": "COOLING_OFF",
                "result": "OK",
                "detail": "无情绪交易信号。",
            })

        # 检查 4：买入时的持仓周期承诺（针对历史平均持仓 6.9 天的纠偏）
        if action in ("BUY", "ADD"):
            avg_hold = self._behavior.get("holding_behavior", {}).get("avg_holding_days", 0)
            reasons.append({
                "check": "HOLDING_PERIOD_INTENT",
                "result": "REMIND",
                "detail": f"历史平均持仓仅 {avg_hold:.0f} 天且短线净亏损。"
                          f"本次买入必须能回答: 持有 6 个月以上的理由是什么？"
                          f"若答不出，属于择时交易，与长期目标冲突。",
            })

        # 检查 5：卖出时防止"拿不住"（早期止盈/恐慌卖出）
        if action in ("SELL", "REDUCE") and symbol in self._holdings:
            last_buys = [t for t in self._txns
                         if t["symbol"] == symbol and t["side"] == "BUY"]
            if last_buys:
                held_days = (as_of - last_buys[-1]["date"]).days
                if held_days <= QUICK_ROUNDTRIP_DAYS:
                    penalty += 0.10
                    reasons.append({
                        "check": "SELL_TIMING",
                        "result": "WARN",
                        "detail": f"距最近一次买入仅 {held_days} 天 — "
                                  f"历史数据显示短时往返交易是亏损来源。",
                    })
                else:
                    reasons.append({
                        "check": "SELL_TIMING",
                        "result": "OK",
                        "detail": f"已持有 {held_days} 天。",
                    })

        adjusted = max(0.0, round(confidence - penalty, 2))
        warn_count = sum(1 for r in reasons if r["result"] == "WARN")
        if penalty >= 0.30 or warn_count >= 3:
            verdict = "BLOCK"
        elif warn_count >= 1:
            verdict = "CAUTION"
        else:
            verdict = "PASS"

        return {
            "symbol": symbol,
            "action": action,
            "verdict": verdict,
            "original_confidence": confidence,
            "adjusted_confidence": adjusted,
            "confidence_penalty": round(penalty, 2),
            "reasons": reasons,
            "as_of": as_of.isoformat(),
        }

    # ── 4. 纪律日报 ──────────────────────────────────

    def generate_report(self, as_of: date | None = None) -> Path:
        """生成 reports/discipline_report.md。"""
        impulse = self.trading_impulse_score(as_of=as_of)
        cooldown = self.cooling_off_check(as_of=as_of)

        # 对当前持仓逐一做纪律状态检查
        holding_checks = []
        for sym, h in self._holdings.items():
            fc = self._failures.get(sym)
            holding_checks.append({
                "symbol": sym,
                "name": h.get("name", ""),
                "weight": float(h.get("weight", 0) or 0),
                "in_failure_db": fc is not None,
                "failure_type": fc.get("failure_type", "") if fc else "",
                "historical_loss": float(fc["loss_amount"]) if fc else 0.0,
            })

        md = []
        md.append("# 投资纪律报告 (Phase S: Discipline Engine)")
        md.append("")
        md.append(f"**生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        md.append(f"**数据截至:** {impulse.get('as_of', 'N/A')}")
        md.append("")
        md.append("## Trading Impulse Score")
        md.append("")
        md.append(f"**{impulse['score']} / 100 — {impulse['level']}**")
        md.append("")
        md.append(f"| 指标 | 数值 |")
        md.append(f"|------|------|")
        md.append(f"| 近 {impulse['window_days']} 天交易 | {impulse['window_trades']} 笔 ({impulse['window_trades_per_week']}/周) |")
        md.append(f"| 生涯基线 | {impulse['baseline_trades_per_week']}/周 |")
        md.append(f"| 频率倍数 | {impulse['freq_ratio']}x |")
        md.append("")
        if impulse["signals"]:
            md.append("### 冲动信号")
            md.append("")
            for s in impulse["signals"]:
                md.append(f"- **{s['signal']}** (+{s['points']}分): {s['detail']}")
        else:
            md.append("无冲动信号。")
        md.append("")
        md.append("## 交易冷静期")
        md.append("")
        md.append(f"状态: **{'⚠️ 冷静期中' if cooldown['cooling_off'] else '✅ 正常'}**")
        md.append("")
        for t in cooldown.get("triggers", []):
            md.append(f"- {t}")
        md.append(f"\n{cooldown.get('advice', '')}")
        md.append("")
        md.append("## 当前持仓纪律检查")
        md.append("")
        md.append("| 标的 | 名称 | 权重 | 失败记忆库 | 历史失败类型 | 历史亏损 |")
        md.append("|------|------|------|-----------|-------------|---------|")
        for hc in sorted(holding_checks, key=lambda x: -x["weight"]):
            md.append(
                f"| {hc['symbol']} | {hc['name']} | {hc['weight']*100:.1f}% "
                f"| {'⚠️ 是' if hc['in_failure_db'] else '否'} "
                f"| {hc['failure_type'] or '-'} "
                f"| {hc['historical_loss']:+,.0f} |" if hc["in_failure_db"] else
                f"| {hc['symbol']} | {hc['name']} | {hc['weight']*100:.1f}% | 否 | - | - |"
            )
        md.append("")
        md.append("## 行为画像提醒（Phase Q 结论）")
        md.append("")
        style = self._behavior.get("investor_style", "unknown")
        md.append(f"- 识别风格: **{style}**（目标: 长期多因子投资 — 存在偏差）")
        for w in self._behavior.get("weaknesses", []):
            md.append(f"- 弱点: {w}")
        for r in self._behavior.get("recommendations", []):
            md.append(f"- 建议: {r}")
        md.append("")
        md.append("## 使用方式")
        md.append("")
        md.append("任何 BUY/SELL 建议执行前必须过守门：")
        md.append("")
        md.append("```python")
        md.append("from portfolio_manager.discipline_engine import DisciplineEngine")
        md.append("engine = DisciplineEngine()")
        md.append('result = engine.pre_trade_check("00700", action="BUY", confidence=0.7)')
        md.append('# verdict: PASS / CAUTION / BLOCK, adjusted_confidence 已按纪律扣分')
        md.append("```")

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / "discipline_report.md"
        out.write_text("\n".join(md), encoding="utf-8")
        return out


# ── CLI ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    engine = DisciplineEngine()

    if len(sys.argv) > 2 and sys.argv[1] == "check":
        # python -m portfolio_manager.discipline_engine check 00700 BUY 0.7
        symbol = sys.argv[2]
        action = sys.argv[3] if len(sys.argv) > 3 else "BUY"
        conf = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
        r = engine.pre_trade_check(symbol, action, conf)
        print(f"=== Pre-trade Discipline Check: {symbol} {action} ===\n")
        print(f"Verdict: {r['verdict']}")
        print(f"Confidence: {r['original_confidence']} → {r['adjusted_confidence']} "
              f"(penalty -{r['confidence_penalty']})")
        print()
        for reason in r["reasons"]:
            mark = {"OK": "✅", "WARN": "⚠️", "REMIND": "💡"}[reason["result"]]
            print(f"  {mark} [{reason['check']}] {reason['detail']}")
        sys.exit(0 if r["verdict"] != "BLOCK" else 2)

    # 默认：完整纪律报告
    print("=" * 60)
    print("  Phase S: Investment Discipline Engine")
    print("=" * 60)
    print()

    impulse = engine.trading_impulse_score()
    print(f"Trading Impulse Score: {impulse['score']}/100 ({impulse['level']})")
    print(f"  近{impulse['window_days']}天: {impulse['window_trades']}笔 "
          f"({impulse['window_trades_per_week']}/周) vs 基线 {impulse['baseline_trades_per_week']}/周")
    for s in impulse["signals"]:
        print(f"  ⚠️  {s['signal']} (+{s['points']}): {s['detail']}")
    print()

    cooldown = engine.cooling_off_check()
    print(f"Cooling-off: {'⚠️ ACTIVE' if cooldown['cooling_off'] else '✅ 正常'}")
    for t in cooldown.get("triggers", []):
        print(f"  - {t}")
    print()

    path = engine.generate_report()
    print(f"报告已保存: {path}")
