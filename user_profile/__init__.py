"""
user_profile — 用户投资画像系统。

根据历史交易自动学习用户投资特点：
- risk_preference（风险偏好）
- investment_horizon（投资期限）
- preferred_market（偏好市场）
- style（投资风格: value/growth/balanced）
- behavior_patterns（行为模式）

保存为 investor_profile.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import Warehouse
from investment_behavior import BehaviorAnalyzer


# 风格分类阈值
VALUE_SYMBOLS = {"601166", "601398", "601288", "601857", "600028", "600036",
                 "601318", "601088", "000651", "000333"}
GROWTH_SYMBOLS = {"300750", "300059", "300124", "688012", "688981",
                  "002415", "002475", "002594", "002230"}


class UserProfile:
    """用户投资画像。"""

    def __init__(self, warehouse: Warehouse,
                 profile_path: str | Path = "models/investor_profile.json"):
        self.warehouse = warehouse
        self.profile_path = Path(profile_path)
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.behavior = BehaviorAnalyzer(warehouse=warehouse)

    def build_profile(self) -> dict[str, Any]:
        """从交易历史和持仓数据构建用户画像。"""
        # 1. 行为分析
        behavior_report = self.behavior.analyze()

        # 2. 风险偏好
        risk_pref = self._infer_risk_preference(behavior_report)

        # 3. 投资期限
        horizon = self._infer_horizon(behavior_report)

        # 4. 风格判断
        style = self._infer_style()

        # 5. 模式总结
        patterns = self._infer_patterns(behavior_report)

        profile = {
            "profile_id": f"investor_{datetime.now().strftime('%Y%m%d')}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_preference": risk_pref,
            "investment_horizon": horizon,
            "style": style,
            "preferred_market": self._infer_market(),
            "behavior_patterns": patterns,
            "behavior_summary": {
                "total_trades": behavior_report.get("summary", {}).get("total_transactions", 0),
                "buy_chase_ratio": behavior_report.get("buy_behavior", {}).get("chase_ratio", 0),
                "early_sell_ratio": behavior_report.get("sell_behavior", {}).get("early_sell_ratio", 0),
                "avg_holding_days": behavior_report.get("sell_behavior", {}).get("avg_holding_days", 0),
                "profit_sell_ratio": behavior_report.get("sell_behavior", {}).get("profit_sell_ratio", 0),
            },
        }

        # 保存
        self.profile_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return profile

    def load_profile(self) -> dict[str, Any] | None:
        if self.profile_path.exists():
            return json.loads(self.profile_path.read_text(encoding="utf-8"))
        return None

    def get_ai_context(self) -> str:
        """生成 AI 提示上下文——告诉 AI 这个用户是什么风格。"""
        profile = self.load_profile() or self.build_profile()
        lines = [
            "## 用户投资画像",
            f"- 风险偏好: {profile.get('risk_preference', '未知')}",
            f"- 投资期限: {profile.get('investment_horizon', '未知')}",
            f"- 投资风格: {profile.get('style', '未知')}",
            f"- 偏好市场: {profile.get('preferred_market', '未知')}",
        ]
        patterns = profile.get("behavior_patterns", {})
        if patterns.get("chase_trend"):
            lines.append("- ⚠ 有追涨倾向")
        if patterns.get("panic_sell"):
            lines.append("- ⚠ 有恐慌卖出倾向")
        if patterns.get("frequent_trade"):
            lines.append("- ⚠ 交易频率偏高")
        if patterns.get("concentrated"):
            lines.append("- ⚠ 持仓集中")
        return "\n".join(lines)

    # ── 推断方法 ──────────────────────────────────────

    def _infer_risk_preference(self, br: dict) -> str:
        b = br.get("buy_behavior", {})
        s = br.get("sell_behavior", {})

        # 持有港股 = 风险承受较高
        markets = b.get("market_preference", {})
        has_hk = any("HK" in m for m in markets)

        chase = b.get("chase_ratio", 0)
        early_sell = s.get("early_sell_ratio", 0)
        hold_days = s.get("avg_holding_days", 0)

        if has_hk and chase < 0.2 and hold_days > 180:
            return "high"
        elif hold_days > 90 or (has_hk and early_sell < 0.3):
            return "medium_high"
        elif 30 <= hold_days <= 90:
            return "medium"
        else:
            return "low"

    def _infer_horizon(self, br: dict) -> str:
        hold_days = br.get("sell_behavior", {}).get("avg_holding_days", 0)
        if hold_days >= 365:
            return "long_term"
        elif hold_days >= 90:
            return "medium_term"
        else:
            return "short_term"

    def _infer_style(self) -> str:
        positions = self.warehouse.query("SELECT * FROM portfolio_computed_position")
        if not positions:
            return "unknown"

        value_count = sum(1 for p in positions if p["symbol"] in VALUE_SYMBOLS)
        growth_count = sum(1 for p in positions if p["symbol"] in GROWTH_SYMBOLS)

        if value_count > growth_count + 1:
            return "value"
        elif growth_count > value_count + 1:
            return "growth"
        else:
            return "balanced"

    def _infer_market(self) -> str:
        markets = set()
        for p in self.warehouse.query(
            "SELECT DISTINCT market FROM portfolio_computed_position"
        ):
            m = p["market"] or ""
            if "HK" in m:
                markets.add("HK")
            elif m == "A":
                markets.add("CN")
            elif m == "US":
                markets.add("US")
        return ", ".join(sorted(markets)) if markets else "CN"

    def _infer_patterns(self, br: dict) -> dict[str, bool]:
        b = br.get("buy_behavior", {})
        s = br.get("sell_behavior", {})

        return {
            "chase_trend": (b.get("chase_ratio", 0) or 0) > 0.3,
            "panic_sell": (s.get("early_sell_ratio", 0) or 0) > 0.5,
            "frequent_trade": br.get("summary", {}).get("total_transactions", 0) > 50,
            "concentrated": self._is_concentrated(),
        }

    def _is_concentrated(self) -> bool:
        positions = self.warehouse.query("SELECT * FROM portfolio_computed_position")
        if not positions:
            return False
        weights = [p["weight"] or 0 for p in positions]
        return max(weights) > 0.4 if weights else False

    def close(self) -> None:
        self.behavior.close()
