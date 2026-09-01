"""
portfolio_optimizer — 组合优化模拟器。

不是直接建议买卖，而是模拟"如果调整仓位，未来风险会怎么变化"。

输出格式：
    - 当前组合：收益预期/最大风险
    - 优化方案 A/B 对比：收益/风险/回撤
"""

from __future__ import annotations

import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundf_db import Warehouse


class PortfolioOptimizer:
    """组合优化模拟器。"""

    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse

    def analyze_current(self) -> dict[str, Any]:
        """分析当前组合的风险收益特征。"""
        positions = self.warehouse.query(
            "SELECT * FROM portfolio_computed_position WHERE weight > 0"
        )
        if not positions:
            return {"status": "no_positions"}

        weights = np.array([p["weight"] or 0 for p in positions])
        symbols = [p["symbol"] for p in positions]

        # 获取各标的收益率序列（最近63天）
        returns_matrix = []
        for sym in symbols:
            bars = self.warehouse.query(
                "SELECT close FROM daily_price WHERE symbol = ? "
                "ORDER BY date DESC LIMIT 63",
                [sym],
            )
            if len(bars) >= 20:
                closes = np.array([b["close"] for b in bars][::-1], dtype=float)
                rets = np.diff(closes) / closes[:-1]
                returns_matrix.append(rets)
            else:
                returns_matrix.append(np.zeros(20))  # 注：零填充低估波动，但标的数少时影响有限

        if not returns_matrix:
            return {"status": "insufficient_data"}

        # 对齐长度
        min_len = min(len(r) for r in returns_matrix)
        aligned = np.array([r[-min_len:] for r in returns_matrix])

        # 组合波动率
        cov = np.cov(aligned)
        cov = np.atleast_2d(np.cov(aligned))
        portfolio_vol = float(np.sqrt(weights @ cov @ weights) * np.sqrt(252))
        portfolio_return = float(np.mean(aligned.T @ weights) * 252)

        # 最大回撤估计
        daily_rets = aligned.T @ weights
        nav = np.cumprod(1 + daily_rets)
        peak = np.maximum.accumulate(nav)
        dd = (nav / peak - 1).min()

        # 集中度
        hhi = float(np.sum(weights ** 2))  # Herfindahl 指数

        return {
            "symbols": symbols,
            "weights": {s: round(w, 4) for s, w in zip(symbols, weights)},
            "expected_annual_return": round(portfolio_return, 4),
            "expected_volatility": round(portfolio_vol, 4),
            "max_drawdown_est": round(float(dd), 4),
            "sharpe_est": round(portfolio_return / portfolio_vol, 4) if portfolio_vol > 0 else 0,
            "concentration_hhi": round(hhi, 4),
            "concentration_level": "high" if hhi > 0.3 else ("medium" if hhi > 0.15 else "low"),
        }

    def simulate_adjustment(
        self, adjustments: dict[str, float],
    ) -> dict[str, Any]:
        """模拟一次调仓。

        adjustments: {"symbol": new_weight, ...}  全量目标权重
        """
        current = self.analyze_current()
        if current.get("status") in ("no_positions", "insufficient_data"):
            return {"status": "cannot_simulate"}

        old_weights = np.array([current["weights"].get(s, 0) for s in current["symbols"]])
        new_w = np.array([adjustments.get(s, 0) for s in current["symbols"]])
        new_w = new_w / new_w.sum() if new_w.sum() > 0 else old_weights

        # 模拟新组合
        # (复用上面的协方差矩阵)
        bars_data = {}
        for sym in current["symbols"]:
            bars = self.warehouse.query(
                "SELECT close FROM daily_price WHERE symbol = ? "
                "ORDER BY date DESC LIMIT 63",
                [sym],
            )
            if len(bars) >= 20:
                closes = np.array([b["close"] for b in bars][::-1], dtype=float)
                bars_data[sym] = np.diff(closes) / closes[:-1]
            else:
                bars_data[sym] = np.zeros(20)

        min_len = min(len(r) for r in bars_data.values())
        aligned = np.array([r[-min_len:] for r in bars_data.values()])
        cov = np.cov(aligned)

        old_vol = float(np.sqrt(old_weights @ cov @ old_weights) * np.sqrt(252))
        new_vol = float(np.sqrt(new_w @ cov @ new_w) * np.sqrt(252))
        vol_change = (new_vol - old_vol) / old_vol if old_vol > 0 else 0

        old_ret = float(np.mean(aligned.T @ old_weights) * 252)
        new_ret = float(np.mean(aligned.T @ new_w) * 252)
        ret_change = new_ret - old_ret if old_ret != 0 else 0

        return {
            "current": {
                "volatility": round(old_vol, 4),
                "annual_return": round(old_ret, 4),
            },
            "simulated": {
                "volatility": round(new_vol, 4),
                "annual_return": round(new_ret, 4),
            },
            "change": {
                "volatility": f"{vol_change:+.1%}",
                "annual_return": f"{ret_change:+.2%}",
            },
            "turnover": round(float(np.sum(np.abs(new_w - old_weights)) / 2), 4),
        }

    def suggest_diversification(self) -> list[dict[str, Any]]:
        """生成分散化建议方案。"""
        current = self.analyze_current()
        if current.get("status") in ("no_positions", "insufficient_data"):
            return []

        suggestions = []
        hhi = current.get("concentration_hhi", 0)
        if hhi > 0.3:
            w = current["weights"]
            top_sym = max(w, key=w.get)
            suggestions.append({
                "name": "降低集中度",
                "action": f"减持 {top_sym}",
                "expected_effect": "降低组合波动风险",
                "priority": "high",
            })

        if current.get("sharpe_est", 1) < 0.5:
            suggestions.append({
                "name": "提升风险调整收益",
                "action": "增加低相关性资产",
                "expected_effect": "改善夏普比率",
                "priority": "medium",
            })

        return suggestions
