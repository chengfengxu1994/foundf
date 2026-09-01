"""
source_quality_engine — 垃圾信息源自动降权机制。

每个来源追踪：
    - 准确率: 历史预测/信号准确比例
    - 提前量: 事件发生前多久发出信号（天）
    - 误报率: 发出信号但未发生或方向错误
    - 影响收益: 按该信号调整持仓后的收益影响

自动流程：
    分数持续下降 → 降低权重 → 最终剔除
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SourceScore:
    """数据源质量评分。"""
    source_id: str
    source_name: str
    tier: int                # 1=官方, 2=专业, 3=社区, 4=自媒体
    accuracy: float          # 0-1
    lead_time_days: float    # 平均提前天数
    false_positive_rate: float  # 0-1
    profit_impact: float     # -1 to 1
    total_score: float       # 0-100 综合
    trend: str               # 'rising', 'stable', 'declining'
    status: str              # 'active', 'reduced', 'deprecated', 'removed'


class SourceQualityEngine:
    """信息源质量引擎。

    内置默认评分:
        东方财富: 90分
        中国人民银行: 95分
        港交所披露易: 88分
        美联储: 92分
        RSS聚合源: 70分
        某财经公众号: 20分 (示例)
    """

    DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
        "pbc_news": {"name": "中国人民银行", "tier": 1, "score": 95},
        "csrc_policy": {"name": "中国证监会", "tier": 1, "score": 93},
        "fed_press": {"name": "Federal Reserve", "tier": 1, "score": 92},
        "sfc_press": {"name": "香港证监会", "tier": 1, "score": 90},
        "hkma_press": {"name": "香港金管局", "tier": 1, "score": 88},
        "stats_gov": {"name": "国家统计局", "tier": 1, "score": 90},
        "eastmoney": {"name": "东方财富", "tier": 2, "score": 85},
        "hkexnews": {"name": "港交所披露易", "tier": 2, "score": 88},
        "sec_edgar": {"name": "SEC EDGAR", "tier": 1, "score": 95},
        "sina_finance": {"name": "新浪财经", "tier": 3, "score": 60},
        "tencent_finance": {"name": "腾讯财经", "tier": 3, "score": 65},
        "blog_amateur": {"name": "财经自媒体", "tier": 4, "score": 25},
    }

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else None
        self._scores: dict[str, SourceScore] = {}
        self._init_scores()

    def _init_scores(self) -> None:
        """初始化评分。"""
        for sid, info in self.DEFAULT_SOURCES.items():
            score = info["score"]
            if score >= 85:
                accuracy, fpr, lead = 0.85, 0.05, 2.0
            elif score >= 70:
                accuracy, fpr, lead = 0.70, 0.15, 1.0
            elif score >= 50:
                accuracy, fpr, lead = 0.55, 0.30, 0.5
            else:
                accuracy, fpr, lead = 0.30, 0.60, 0.1
            self._scores[sid] = SourceScore(
                source_id=sid,
                source_name=info["name"],
                tier=info["tier"],
                accuracy=accuracy,
                lead_time_days=lead,
                false_positive_rate=fpr,
                profit_impact=0.0,
                total_score=score,
                trend="stable",
                status="active",
            )

    def get_score(self, source_id: str) -> SourceScore | None:
        return self._scores.get(source_id)

    def get_event_weight(self, source_id: str) -> float:
        """返回该源的事件权重（0=剔除, 1=正常）。"""
        s = self._scores.get(source_id)
        if s is None:
            return 0.5
        if s.status == "removed":
            return 0.0
        if s.status == "deprecated":
            return 0.2
        if s.status == "reduced":
            return 0.5
        return 1.0

    def record_outcome(
        self, source_id: str, was_accurate: bool, lead_days: float, profit_effect: float,
    ) -> None:
        """记录一次信号结果，更新评分。"""
        s = self._scores.get(source_id)
        if s is None:
            return

        # 更新准确率（指数衰减）
        s.accuracy = s.accuracy * 0.9 + (0.1 if was_accurate else 0)
        s.lead_time_days = s.lead_time_days * 0.9 + lead_days * 0.1
        s.false_positive_rate = s.false_positive_rate * 0.9 + (0.1 if not was_accurate else 0)
        s.profit_impact = s.profit_impact * 0.95 + profit_effect * 0.05

        # 重新计算总分
        score = 100.0
        score *= s.accuracy
        score -= s.false_positive_rate * 30
        score += min(s.lead_time_days * 3, 15)
        score += max(min(s.profit_impact * 20, 10), -20)

        # 基础分调整
        tier_bonus = {1: 15, 2: 5, 3: -5, 4: -15}.get(s.tier, 0)
        score += tier_bonus
        s.total_score = max(0, min(100, round(score, 1)))

        # 判断趋势
        if s.trend == "stable" and s.total_score < 40:
            s.trend = "declining"
        elif s.trend == "declining" and s.total_score > 60:
            s.trend = "rising"

        # 自动降级/剔除
        if s.total_score < 15:
            s.status = "removed"
        elif s.total_score < 30:
            s.status = "deprecated"
        elif s.total_score < 50:
            s.status = "reduced"
        else:
            s.status = "active"

    def list_scores(self) -> list[dict[str, Any]]:
        """列出所有源的评分（按总分降序）。"""
        result = []
        for sid, s in sorted(self._scores.items(), key=lambda x: -x[1].total_score):
            result.append({
                "id": sid,
                "name": s.source_name,
                "tier": s.tier,
                "score": s.total_score,
                "accuracy": round(s.accuracy, 3),
                "lead_days": round(s.lead_time_days, 1),
                "fpr": round(s.false_positive_rate, 3),
                "profit_impact": round(s.profit_impact, 3),
                "trend": s.trend,
                "status": s.status,
                "weight": self.get_event_weight(sid),
            })
        return result

    def save_config(self, path: str | Path) -> None:
        """保存当前评分到 YAML 配置。"""
        data = {"source_scores": self.list_scores()}
        Path(path).write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )

    def load_config(self, path: str | Path) -> None:
        """从 YAML 配置加载评分。"""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for item in raw.get("source_scores", []):
            sid = item.get("id")
            if sid and sid in self._scores:
                s = self._scores[sid]
                s.total_score = item.get("score", s.total_score)
                s.accuracy = item.get("accuracy", s.accuracy)
                s.false_positive_rate = item.get("fpr", s.false_positive_rate)
                s.lead_time_days = item.get("lead_days", s.lead_time_days)
                s.profit_impact = item.get("profit_impact", s.profit_impact)
                s.trend = item.get("trend", s.trend)
                s.status = item.get("status", s.status)
