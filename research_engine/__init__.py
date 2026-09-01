"""
research_engine — Phase P: Factor Research Platform.

研究因子长期有效性，目标不是"最高收益组合"，而是找到稳定因子。

对每个因子输出四个维度证据：
    factor_ic.py        — IC / RankIC / ICIR（预测力）
    factor_decay.py     — 多 horizon 衰减形态（与长期目标匹配度）
    factor_return.py    — 分组多空收益（盈利能力）
    factor_stability.py — 逐年稳定性（跨周期可靠性）

判定规则（成熟量化实践，防止"只看历史最高收益"的过拟合）：
    KEEP:          mean RankIC ≥ +0.03 且 ICIR ≥ +0.2 且稳定年份比例 ≥ 55%
                   且通过 Benjamini-Hochberg FDR 校正（α=0.05）
    KEEP_REVERSED: mean RankIC ≤ -0.03 且 ICIR ≤ -0.2 且稳定年份比例 ≥ 55%
                   — 负方向有效，显式标记；生产按正方向用因子，不允许
                   靠 abs() 静默通过，须人工翻转因子定义后重新研究
    CULL:          |mean RankIC| < 0.01 或 |ICIR| < 0.05
    WATCH:         其余 — 保留观察，不进正式组合

多重检验防护（2026-08-13 新增）：同时研究十余个因子，KEEP/KEEP_REVERSED
必须通过 BH 校正后的显著性门槛（IC 序列单样本 t，正态近似双侧 p），
未通过者自动降级 WATCH。

输出: reports/factor_research/factor_report_{date}.md + factor_research.json
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import Warehouse

from .factor_series import FACTOR_DEFS, FactorSeriesBuilder
from .factor_ic import compute_ic_series, summarize_ic
from .factor_decay import analyze_decay
from .factor_return import compute_spread_series, summarize_returns
from .factor_stability import analyze_stability

OUT_DIR = Path("reports/factor_research")
DEFAULT_HORIZON = 21  # 主评估 horizon：21 个交易日（约 1 个月）

# 判定阈值
KEEP_MIN_RANK_IC = 0.03
KEEP_MIN_ICIR = 0.20
KEEP_MIN_STABLE_RATIO = 0.55
CULL_MAX_RANK_IC = 0.01
CULL_MAX_ICIR = 0.05
FDR_ALPHA = 0.05  # Benjamini-Hochberg 多重检验校正显著性水平


class FactorResearch:
    """因子研究平台主入口。

    使用方式:
        research = FactorResearch("data/finance.duckdb")
        result = research.run()
        research.save(result)
    """

    def __init__(self, duckdb_path: str | Path = "data/finance.duckdb"):
        self.warehouse = Warehouse(duckdb_path)
        self.warehouse.init()
        self.builder = FactorSeriesBuilder(self.warehouse)

    @staticmethod
    def _verdict(ic_summary: dict[str, Any],
                 stability: dict[str, Any]) -> tuple[str, str, str]:
        """根据证据给出 KEEP / KEEP_REVERSED / WATCH / CULL 判定。

        方向敏感（2026-08-13 修复）：生产按因子正方向使用，强负 IC 因子
        不允许靠 abs() 静默通过 KEEP；负方向有效必须显式判定为
        KEEP_REVERSED（须人工翻转因子定义后重新研究，方可转 KEEP）。

        Returns: (verdict, reason, direction)，direction 为 "+" 或 "-"。
        """
        mean_ric = float(ic_summary.get("mean_rank_ic") or 0)
        icir = float(ic_summary.get("icir") or 0)
        stable = stability.get("stable_year_ratio", 0)

        if abs(mean_ric) < CULL_MAX_RANK_IC or abs(icir) < CULL_MAX_ICIR:
            return "CULL", (f"RankIC={mean_ric:+.3f} 或 ICIR={icir:+.2f} "
                            f"低于有效性下限，无预测力证据"), "+"
        if (mean_ric >= KEEP_MIN_RANK_IC and icir >= KEEP_MIN_ICIR
                and stable >= KEEP_MIN_STABLE_RATIO):
            return "KEEP", (f"RankIC={mean_ric:+.3f}, ICIR={icir:+.2f}, "
                            f"稳定年份 {stable:.0%}，通过全部有效性门槛"), "+"
        if (mean_ric <= -KEEP_MIN_RANK_IC and icir <= -KEEP_MIN_ICIR
                and stable >= KEEP_MIN_STABLE_RATIO):
            return "KEEP_REVERSED", (
                f"RankIC={mean_ric:+.3f}, ICIR={icir:+.2f}, "
                f"稳定年份 {stable:.0%} — 负方向有效：因子取值越高预测收益"
                f"越低；不允许直接进组合，须人工确认并翻转因子定义后重新研究"
            ), "-"
        return "WATCH", (f"RankIC={mean_ric:+.3f}, ICIR={icir:+.2f}, "
                         f"稳定年份 {stable:.0%} — 部分达标，保留观察"), \
            ("+" if mean_ric >= 0 else "-")

    @staticmethod
    def _ic_t_p(ic_summary: dict[str, Any]) -> tuple[float | None, float | None]:
        """由 IC 汇总统计量计算单样本 t 与双侧 p 值（正态近似）。

        项目无 scipy/statsmodels 依赖：t = mean / (std/√n)，p 用 math.erfc
        的正态尾部近似。样本不足或方差为 0 返回 (None, None)，
        FDR 判定按不显著处理（fail-closed）。
        """
        n = int(ic_summary.get("periods") or 0)
        mean = ic_summary.get("mean_rank_ic")
        std = ic_summary.get("std_rank_ic")
        if n < 3 or mean is None or not std:
            return None, None
        t = float(mean) / (float(std) / math.sqrt(n))
        p = math.erfc(abs(t) / math.sqrt(2))  # 双侧 p = 2·(1-Φ(|t|))
        return round(t, 3), p

    @staticmethod
    def _apply_fdr_control(factors: dict[str, Any],
                           alpha: float = FDR_ALPHA) -> None:
        """Benjamini-Hochberg FDR 控制（原地修改 factors）。

        同时研究十余个因子时，KEEP / KEEP_REVERSED 必须通过 BH 校正后的
        显著性门槛，防止多重检验下的假阳性保留；未通过者降级 WATCH。
        """
        tested: list[tuple[float, str]] = []
        for name, f in factors.items():
            t, p = FactorResearch._ic_t_p(f["ic"])
            f["ic_t_stat"] = t
            f["ic_p_value"] = round(p, 6) if p is not None else None
            f["fdr_significant"] = False
            if p is not None:
                tested.append((p, name))
        if not tested:
            return
        tested.sort()
        m = len(tested)
        cutoff = 0
        for k, (p, _) in enumerate(tested, start=1):
            if p <= (k / m) * alpha:
                cutoff = k
        significant = {name for _, name in tested[:cutoff]}
        for name, f in factors.items():
            if name in significant:
                f["fdr_significant"] = True
            elif f["verdict"] in ("KEEP", "KEEP_REVERSED"):
                f["verdict"] = "WATCH"
                f["verdict_reason"] += (
                    f"；但多重检验校正（BH, α={alpha}）后未达显著"
                    f"（p={f['ic_p_value']}），降级 WATCH")

    def run(self, horizon: int = DEFAULT_HORIZON) -> dict[str, Any]:
        """运行完整因子研究。"""
        data = self.builder.build()
        if not data["dates"]:
            return {"error": "daily_price 无数据，无法研究"}

        n_symbols = len(data["symbols"])
        date_range = (f"{data['dates'][0]} ~ {data['dates'][-1]}"
                      if data["dates"] else "N/A")

        factors: dict[str, Any] = {}
        for name, (_, category, is_proxy, _input) in FACTOR_DEFS.items():
            ic_series = compute_ic_series(data, name, self.builder, horizon)
            ic_summary = summarize_ic(ic_series)
            decay = analyze_decay(data, name, self.builder)
            spread = compute_spread_series(data, name, self.builder, horizon)
            ret_summary = summarize_returns(spread, horizon)
            stability = analyze_stability(ic_series)
            verdict, reason, direction = self._verdict(ic_summary, stability)

            factors[name] = {
                "category": category,
                "is_price_proxy": is_proxy,
                "direction": direction,
                "ic": ic_summary,
                "decay": decay,
                "returns": ret_summary,
                "stability": stability,
                "verdict": verdict,
                "verdict_reason": reason,
            }

        # 多重检验防护：BH 校正后未显著的 KEEP/KEEP_REVERSED 降级 WATCH
        self._apply_fdr_control(factors)

        # 数据充分性声明（诚实报告，防止小样本过拟合）
        caveats = []
        if n_symbols < 30:
            caveats.append(
                f"横截面仅 {n_symbols} 个标的（成熟研究通常 ≥100），"
                f"IC 估计噪声大，结论仅供方向性参考")
        if data["dates"]:
            span_years = (data["dates"][-1] - data["dates"][0]).days / 365.25
            if span_years < 10:
                caveats.append(
                    f"样本期 {span_years:.1f} 年，未满 10 年目标；"
                    f"随数据资产积累需定期重跑")
        proxy_count = sum(1 for f in factors.values() if f["is_price_proxy"])
        if proxy_count:
            caveats.append(
                f"{proxy_count} 个因子为价格代理，保留作对照 — "
                f"Quality/Growth 真基本面版（quality_roe_real/accrual_cfo_np/"
                f"growth_profit_yoy）已随 baostock 财报回填接入，"
                f"按 filed_at 做 PIT 对齐；"
                f"Value 已接 tushare daily_basic 真实估值序列")
        tested_count = sum(1 for f in factors.values()
                           if f.get("ic_p_value") is not None)
        caveats.append(
            f"已对 {tested_count} 个可检验因子做 Benjamini-Hochberg 多重检验"
            f"校正（α={FDR_ALPHA}），未通过校正的 KEEP 自动降级 WATCH")

        # 数据截止日（证据指纹）：evolution 据此判定"独立运行"，
        # 同一批数据重复生成的报告不算新的独立证据
        all_price_dates = [d for dates in data.get("price_dates", {}).values()
                           for d in dates]
        data_as_of = str(max(all_price_dates)) if all_price_dates else None

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_as_of": data_as_of,
            "horizon_days": horizon,
            "universe_size": n_symbols,
            "sample_dates": len(data["dates"]),
            "date_range": date_range,
            "fdr_alpha": FDR_ALPHA,
            "caveats": caveats,
            "factors": factors,
            "summary": {
                "keep": [n for n, f in factors.items() if f["verdict"] == "KEEP"],
                "keep_reversed": [n for n, f in factors.items()
                                  if f["verdict"] == "KEEP_REVERSED"],
                "watch": [n for n, f in factors.items() if f["verdict"] == "WATCH"],
                "cull": [n for n, f in factors.items() if f["verdict"] == "CULL"],
            },
        }

    # ── 报告输出 ─────────────────────────────────────

    def to_markdown(self, result: dict[str, Any]) -> str:
        md = []
        md.append("# 因子研究报告 (Phase P: Factor Research Platform)")
        md.append("")
        md.append(f"**生成时间:** {result['generated_at'][:16]}")
        md.append(f"**样本:** {result['date_range']} | "
                  f"{result['universe_size']} 标的 | "
                  f"{result['sample_dates']} 个采样日 | "
                  f"主 horizon {result['horizon_days']} 个交易日")
        md.append("")
        if result.get("caveats"):
            md.append("## ⚠️ 数据充分性声明")
            md.append("")
            for c in result["caveats"]:
                md.append(f"- {c}")
            md.append("")

        md.append("## 因子判定总览")
        md.append("")
        md.append("| 因子 | 类别 | RankIC | ICIR | 正IC比例 | 稳定年份 | 多空年化 | 最大回撤 | 衰减形态 | p值(BH) | 判定 |")
        md.append("|------|------|--------|------|---------|---------|---------|---------|---------|---------|------|")
        for name, f in result["factors"].items():
            ic = f["ic"]
            ret = f["returns"]
            st = f["stability"]
            proxy_mark = "*" if f["is_price_proxy"] else ""
            verdict_mark = {"KEEP": "✅ KEEP", "KEEP_REVERSED": "🔁 KEEP(反向)",
                            "WATCH": "🟡 WATCH", "CULL": "⛔ CULL"}[f["verdict"]]
            p_val = f.get("ic_p_value")
            p_cell = ("-" if p_val is None
                      else f"{p_val:.4f}{' ✓' if f.get('fdr_significant') else ''}")
            md.append(
                f"| {name}{proxy_mark} | {f['category']} "
                f"| {ic.get('mean_rank_ic', '-')} | {ic.get('icir', '-')} "
                f"| {ic.get('positive_ratio', '-')} "
                f"| {st.get('stable_year_ratio', '-')} "
                f"| {ret.get('ann_return', '-')} | {ret.get('max_drawdown', '-')} "
                f"| {f['decay']['decay_shape']} | {p_cell} | {verdict_mark} |"
            )
        md.append("")
        md.append("\\* = 价格代理因子（待财务数据沉淀后用真基本面因子替换重跑）；"
                  "p值 ✓ = 通过 Benjamini-Hochberg 多重检验校正")
        md.append("")

        s = result["summary"]
        md.append("## 结论")
        md.append("")
        md.append(f"- **KEEP（进入正式组合候选）:** {', '.join(s['keep']) or '无'}")
        md.append(f"- **KEEP_REVERSED（负方向有效，须人工翻转定义后重新研究）:** "
                  f"{', '.join(s.get('keep_reversed', [])) or '无'}")
        md.append(f"- **WATCH（保留观察）:** {', '.join(s['watch']) or '无'}")
        md.append(f"- **CULL（剔除）:** {', '.join(s['cull']) or '无'}")
        md.append("")

        md.append("## 各因子详细证据")
        for name, f in result["factors"].items():
            md.append("")
            md.append(f"### {name} ({f['category']}) — {f['verdict']}")
            md.append("")
            md.append(f"> {f['verdict_reason']}")
            md.append("")
            md.append("**IC 衰减:**")
            md.append("")
            md.append("| Horizon | RankIC | ICIR | 正IC比例 |")
            md.append("|---------|--------|------|---------|")
            for h, hs in f["decay"]["by_horizon"].items():
                md.append(f"| {h} | {hs.get('mean_rank_ic', '-')} "
                          f"| {hs.get('icir', '-')} | {hs.get('positive_ratio', '-')} |")
            md.append("")
            yr = f["stability"].get("yearly_ic", {})
            if yr:
                md.append("**逐年 RankIC:**")
                md.append("")
                md.append("| 年份 | RankIC | 期数 |")
                md.append("|------|--------|------|")
                for y, ys in yr.items():
                    md.append(f"| {y} | {ys['mean_rank_ic']} | {ys['periods']} |")
                md.append("")
            yret = f["returns"].get("yearly_returns", {})
            if yret:
                md.append("**逐年多空收益:** "
                          + ", ".join(f"{y}: {v:+.1%}" for y, v in yret.items()))
                md.append("")
        return "\n".join(md)

    def save(self, result: dict[str, Any]) -> Path:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        md_path = OUT_DIR / f"factor_report_{today}.md"
        md_path.write_text(self.to_markdown(result), encoding="utf-8")
        json_path = OUT_DIR / "factor_research.json"
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        return md_path


# ── CLI ────────────────────────────────────────────

def main() -> None:
    research = FactorResearch()
    result = research.run()
    if "error" in result:
        print(f"Error: {result['error']}")
        raise SystemExit(1)

    print("=" * 70)
    print("  Phase P: Factor Research Platform")
    print("=" * 70)
    print(f"\n样本: {result['date_range']} | {result['universe_size']} 标的 | "
          f"{result['sample_dates']} 采样日\n")

    print(f"{'Factor':16s} {'RankIC':>7s} {'ICIR':>6s} {'Stable':>6s} "
          f"{'AnnRet':>8s} {'MaxDD':>7s} {'Decay':12s} Verdict")
    print("-" * 78)
    for name, f in result["factors"].items():
        ic, ret, st = f["ic"], f["returns"], f["stability"]
        print(f"{name:16s} {ic.get('mean_rank_ic', 0):7.3f} "
              f"{(ic.get('icir') or 0):6.2f} "
              f"{st.get('stable_year_ratio', 0):6.0%} "
              f"{(ret.get('ann_return') or 0):8.1%} "
              f"{(ret.get('max_drawdown') or 0):7.1%} "
              f"{f['decay']['decay_shape']:12s} {f['verdict']}")

    for c in result.get("caveats", []):
        print(f"\n⚠️  {c}")

    s = result["summary"]
    print(f"\nKEEP: {', '.join(s['keep']) or '无'}")
    print(f"WATCH: {', '.join(s['watch']) or '无'}")
    print(f"CULL: {', '.join(s['cull']) or '无'}")

    path = research.save(result)
    print(f"\n报告已保存: {path}")


if __name__ == "__main__":
    main()
