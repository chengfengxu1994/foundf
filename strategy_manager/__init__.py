"""
strategy_manager — 策略版本管理与自动优化框架。

设计原则（来自设计文档）：
    - 不要让AI直接修改策略
    - 策略版本管理：strategy_v1 → backtest → strategy_v2 → compare → 保留优秀版本
    - 评价指标：年化收益、最大回撤、夏普比率、胜率、换手率

架构：
    1. StrategyRegistry — 注册/加载/比较策略版本
    2. BacktestRunner — 对某个版本运行回测
    3. StrategyGovernor — 审批门控（禁止AI直接修改）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

@dataclass
class StrategyVersion:
    """一个策略版本的完整描述。"""
    version_id: str            # e.g. "v1", "v2"
    name: str                  # 人类可读名称
    model_type: str            # 'multifactor_v2', 'multifactor_v3'
    config: dict[str, Any]     # 因子权重 + 参数快照
    description: str
    created_at: str
    parent_version: str | None = None  # 基于哪个版本迭代
    approval_status: str = "draft"     # 'draft', 'pending_review', 'approved', 'rejected', 'deprecated'
    backtest_results: dict[str, Any] | None = None
    config_hash: str = ""


@dataclass
class BacktestResult:
    """一次回测的完整结果。"""
    version_id: str
    start_date: str
    end_date: str
    annual_return: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    turnover: float
    total_return: float
    volatility: float
    calmar_ratio: float
    benchmark_return: float | None = None
    excess_return: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


class StrategyRegistry:
    """策略版本注册表。

    存储位置：DuckDB strategy_versions 表 或本地 JSON 文件。
    """

    def __init__(self, storage_path: str | Path = "models/strategy_registry.json"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._versions: dict[str, StrategyVersion] = {}
        self._load()

    # ── 预置策略版本 ──────────────────────────────────

    @staticmethod
    def builtin_v2() -> StrategyVersion:
        """现有 v2 策略（8因子，保留兼容）。"""
        config = {
            "factor_weights": {
                "momentum": 0.24, "trend": 0.16, "defensive": 0.16,
                "consistency": 0.12, "liquidity": 0.07, "reversal": 0.05,
                "attention": 0.08, "cashflow": 0.12,
            },
            "min_history": 130, "top_n": 6, "max_weight": 0.25,
            "target_vol": 0.14, "min_budget": 0.25,
        }
        return StrategyVersion(
            version_id="v2",
            name="多因子策略 v2（原版）",
            model_type="multifactor_v2",
            config=config,
            description="原8因子策略：momentum/trend/defensive/consistency/liquidity/reversal/attention/cashflow",
            created_at="2026-07-01T00:00:00",
            approval_status="approved",
            config_hash=StrategyRegistry._hash_config(config),
        )

    @staticmethod
    def builtin_v3_default() -> StrategyVersion:
        """v3 默认权重（设计文档标准配置）。"""
        config = {
            "factor_weights": {
                "value": 0.25, "quality": 0.25, "growth": 0.20,
                "momentum": 0.15, "risk": 0.15,
            },
            "min_history": 130, "top_n": 6, "max_weight": 0.25,
            "target_vol": 0.14, "min_budget": 0.25, "turnover_blend": 0.25,
        }
        return StrategyVersion(
            version_id="v3_default",
            name="多因子策略 v3（标准权重）",
            model_type="multifactor_v3",
            config=config,
            description="5因子标准：Value 25% + Quality 25% + Growth 20% + Momentum 15% + Risk 15%",
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_version="v2",
            approval_status="draft",
            config_hash=StrategyRegistry._hash_config(config),
        )

    @staticmethod
    def builtin_v3_conservative() -> StrategyVersion:
        """v3 保守配置（提高风险权重，降低动量）。"""
        config = {
            "factor_weights": {
                "value": 0.25, "quality": 0.30, "growth": 0.15,
                "momentum": 0.10, "risk": 0.20,
            },
            "min_history": 130, "top_n": 6, "max_weight": 0.20,
            "target_vol": 0.12, "min_budget": 0.30, "turnover_blend": 0.30,
        }
        return StrategyVersion(
            version_id="v3_conservative",
            name="多因子策略 v3（保守配置）",
            model_type="multifactor_v3",
            config=config,
            description="5因子保守：Quality 30% + Risk 20%，降波动率目标至12%",
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_version="v3_default",
            approval_status="draft",
            config_hash=StrategyRegistry._hash_config(config),
        )

    @staticmethod
    def builtin_v3_aggressive() -> StrategyVersion:
        """v3 进取配置（提高动量成长权重）。"""
        config = {
            "factor_weights": {
                "value": 0.20, "quality": 0.20, "growth": 0.25,
                "momentum": 0.20, "risk": 0.15,
            },
            "min_history": 130, "top_n": 8, "max_weight": 0.30,
            "target_vol": 0.18, "min_budget": 0.20, "turnover_blend": 0.20,
        }
        return StrategyVersion(
            version_id="v3_aggressive",
            name="多因子策略 v3（进取配置）",
            model_type="multifactor_v3",
            config=config,
            description="5因子进取：Growth 25% + Momentum 20%，升波动率目标至18%，选8只",
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_version="v3_default",
            approval_status="draft",
            config_hash=StrategyRegistry._hash_config(config),
        )

    # ── 注册表方法 ────────────────────────────────────

    def register(self, version: StrategyVersion) -> None:
        """注册一个新版本。"""
        if version.version_id in self._versions:
            raise ValueError(f"版本 {version.version_id} 已存在")
        if not version.config_hash:
            version.config_hash = self._hash_config(version.config)
        self._versions[version.version_id] = version
        self._save()

    def get(self, version_id: str) -> StrategyVersion | None:
        return self._versions.get(version_id)

    def list_versions(self) -> list[StrategyVersion]:
        return sorted(self._versions.values(), key=lambda v: v.created_at, reverse=True)

    def compare(self, *version_ids: str) -> list[dict[str, Any]]:
        """比较多个版本的指标。"""
        results = []
        for vid in version_ids:
            v = self._versions.get(vid)
            if v and v.backtest_results:
                results.append({
                    "version_id": vid,
                    "name": v.name,
                    "approval_status": v.approval_status,
                    **v.backtest_results,
                })
        return results

    # 禁止 AI 自动设置的状态（与 StrategyGovernor 口径一致）
    HUMAN_ONLY_STATUSES = {"approved", "rejected"}

    def update_status(self, version_id: str, status: str,
                      human: bool = False) -> None:
        """更新审批状态。

        ``approved``/``rejected`` 只能由人工设置（``human=True``，即
        人手工调用/CLI 显式确认）；AI 调用方（如 Governor.request_review）
        只能设 draft/pending_review/deprecated。此前 HUMAN_ONLY_STATUSES
        定义后从未校验，审批门形同虚设（2026-08-06 review 修复）。
        """
        if status in self.HUMAN_ONLY_STATUSES and not human:
            raise PermissionError(
                f"状态 {status!r} 需人工审批：AI/自动链路不得直接设置；"
                f"人工确认请用 update_status(..., human=True)")
        v = self._versions.get(version_id)
        if v:
            v.approval_status = status
            self._save()

    def propose_new(
        self, name: str, description: str, config: dict[str, Any],
        parent_version: str | None = None,
    ) -> StrategyVersion:
        """提议一个新版本（自动生成 version_id）。"""
        v = StrategyVersion(
            version_id=f"v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            name=name,
            model_type="multifactor_v3",
            config=config,
            description=description,
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_version=parent_version,
            approval_status="draft",
            config_hash=self._hash_config(config),
        )
        return v

    # ── 持久化 ────────────────────────────────────────

    def _save(self) -> None:
        data = []
        for v in self._versions.values():
            data.append({
                "version_id": v.version_id,
                "name": v.name,
                "model_type": v.model_type,
                "config": v.config,
                "description": v.description,
                "created_at": v.created_at,
                "parent_version": v.parent_version,
                "approval_status": v.approval_status,
                "config_hash": v.config_hash,
                "backtest_results": v.backtest_results,
            })
        self.storage_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> None:
        if not self.storage_path.exists():
            # 首次启动：注册预置版本
            for v in [self.builtin_v2(), self.builtin_v3_default(),
                      self.builtin_v3_conservative(), self.builtin_v3_aggressive()]:
                self._versions[v.version_id] = v
            self._save()
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            for item in data:
                v = StrategyVersion(
                    version_id=item["version_id"],
                    name=item["name"],
                    model_type=item["model_type"],
                    config=item["config"],
                    description=item["description"],
                    created_at=item["created_at"],
                    parent_version=item.get("parent_version"),
                    approval_status=item.get("approval_status", "draft"),
                    config_hash=item.get("config_hash", ""),
                    backtest_results=item.get("backtest_results"),
                )
                self._versions[v.version_id] = v
        except (json.JSONDecodeError, KeyError):
            pass

    @staticmethod
    def _hash_config(config: dict[str, Any]) -> str:
        raw = json.dumps(config, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def format_comparison(results: list[dict[str, Any]]) -> str:
        """格式化比较结果。"""
        if not results:
            return "无可用比较数据"
        lines = ["## 策略版本比较", ""]
        headers = ["版本", "状态", "年化收益", "最大回撤", "夏普比率", "胜率", "换手率"]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---" for _ in headers) + "|")
        for r in results:
            lines.append(
                f"| {r['version_id']} | {r.get('approval_status', 'N/A')} "
                f"| {r.get('annual_return', 'N/A')} "
                f"| {r.get('max_drawdown', 'N/A')} "
                f"| {r.get('sharpe_ratio', 'N/A')} "
                f"| {r.get('win_rate', 'N/A')} "
                f"| {r.get('turnover', 'N/A')} |"
            )
        return "\n".join(lines)


class StrategyGovernor:
    """策略审批门控。

    规则：
        - 新版本必须先 run backtest
        - 人工审核后才能 approved
        - AI 不能直接修改策略参数
        - 禁止自动将 'pending_review' 改为 'approved'
    """

    # 禁止 AI 自动设置的状态
    HUMAN_ONLY_STATUSES = {"approved", "rejected"}

    # AI 可以设置的状态
    AI_ALLOWED_STATUSES = {"draft", "pending_review", "deprecated"}

    def __init__(self, registry: StrategyRegistry):
        self.registry = registry

    def request_review(self, version_id: str) -> bool:
        """提交人工审核。"""
        v = self.registry.get(version_id)
        if v and v.backtest_results:
            self.registry.update_status(version_id, "pending_review")
            return True
        return False

    def get_pending_reviews(self) -> list[StrategyVersion]:
        return [v for v in self.registry.list_versions() if v.approval_status == "pending_review"]

    def generate_review_report(self, version_id: str) -> str:
        """生成审批报告（供人类阅读）。"""
        v = self.registry.get(version_id)
        if not v:
            return f"版本 {version_id} 不存在"

        lines = [
            f"## 策略审批请求: {v.name} ({v.version_id})",
            f"",
            f"- 类型: {v.model_type}",
            f"- 描述: {v.description}",
            f"- 基于: {v.parent_version or 'N/A'}",
            f"- 创建: {v.created_at}",
            f"- 当前状态: {v.approval_status}",
            f"",
        ]
        if v.config:
            lines.append("### 配置参数")
            lines.append(f"```json")
            lines.append(json.dumps(v.config, ensure_ascii=False, indent=2))
            lines.append(f"```")
        if v.backtest_results:
            lines.append("### 回测结果")
            for key, value in v.backtest_results.items():
                if key != "details":
                    lines.append(f"- {key}: {value}")
        lines.extend([
            "",
            "---",
            "**请人工审核后，设置状态为 'approved' 或 'rejected'。**",
            "AI 不能自动通过此审批。",
        ])
        return "\n".join(lines)
