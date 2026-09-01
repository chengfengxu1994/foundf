"""
fee_model.py — 市场费用模型。

用于：
    1. 检查费用是否异常
    2. 回测中模拟未来交易成本
    3. 对缺少费用字段的数据进行估计

注意：历史回溯以券商流水实际费用为最终事实。
费率和最低收费全部放入配置文件，不硬编码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketFeeConfig:
    """市场费用配置。"""
    market: str
    commission_rate: float = 0.0003        # 佣金费率
    min_commission: float = 5.0            # 最低佣金（A股）
    stamp_duty_rate_sell: float = 0.001     # 卖出印花税率
    stamp_duty_rate_buy: float = 0.0        # 买入印花税率
    transfer_fee_rate: float = 0.00002      # 过户费率
    exchange_fee_rate: float = 0.0000687    # 交易经手费


# 各市场默认配置
MARKET_CONFIGS: dict[str, MarketFeeConfig] = {
    "A": MarketFeeConfig(
        market="A",
        commission_rate=0.00025,
        min_commission=5.0,
        stamp_duty_rate_sell=0.001,
        transfer_fee_rate=0.00001,  # 万0.1
    ),
    "ETF_CN": MarketFeeConfig(
        market="ETF_CN",
        commission_rate=0.0003,
        min_commission=5.0,  # 部分券商ETF最低5元
        stamp_duty_rate_sell=0.0,  # ETF免印花税
        transfer_fee_rate=0.0,  # ETF免过户费
    ),
    "HK_CONNECT": MarketFeeConfig(
        market="HK_CONNECT",
        commission_rate=0.0015,  # 港股通佣金较高
        min_commission=0.0,
        stamp_duty_rate_sell=0.001,  # 港股印花税
        transfer_fee_rate=0.000027,  # 香港结算费
    ),
    "US": MarketFeeConfig(
        market="US",
        commission_rate=0.0,  # 0佣金券商
        min_commission=0.0,
        stamp_duty_rate_sell=0.0,
        transfer_fee_rate=0.00008,  # SEC fee
    ),
}


class FeeModel:
    """费用模型。

    使用方式:
        model = FeeModel()
        fees = model.estimate("HK_CONNECT", 12000, 3.998, "BUY")
        # 返回: {commission, stamp_duty, transfer_fee, total}
    """

    def __init__(self, configs: dict[str, MarketFeeConfig] | None = None):
        self.configs = configs or MARKET_CONFIGS

    def estimate(self, market: str, quantity: float, price: float,
                 side: str) -> dict[str, float]:
        """估算交易费用。"""
        config = self.configs.get(market, self.configs["A"])
        trade_value = quantity * price

        commission = max(trade_value * config.commission_rate, config.min_commission)
        if market == "HK_CONNECT":
            commission = round(commission, 2)

        if side == "SELL":
            stamp_duty = trade_value * config.stamp_duty_rate_sell
        else:
            stamp_duty = trade_value * config.stamp_duty_rate_buy

        transfer_fee = trade_value * config.transfer_fee_rate

        return {
            "commission": round(commission, 2),
            "stamp_duty": round(stamp_duty, 2),
            "transfer_fee": round(transfer_fee, 4),
            "exchange_fee": round(trade_value * config.exchange_fee_rate, 4),
            "total": round(commission + stamp_duty + transfer_fee, 2),
        }

    def validate(self, market: str, quantity: float, price: float,
                 side: str, actual_fees: dict[str, float]) -> list[str]:
        """检查实际费用是否异常。"""
        estimated = self.estimate(market, quantity, price, side)
        warnings = []
        for key in ["commission", "stamp_duty"]:
            est = estimated.get(key, 0)
            actual = actual_fees.get(key, 0)
            if est > 0 and actual > 0:
                ratio = actual / est
                if ratio > 3:
                    warnings.append(f"{key}: 实际 {actual} 是估算 {est} 的 {ratio:.1f} 倍")
                elif ratio < 0.3:
                    warnings.append(f"{key}: 实际 {actual} 远低于估算 {est}")
        return warnings
