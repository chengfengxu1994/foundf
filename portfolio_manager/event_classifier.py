"""
event_classifier.py — 交易事件分类器。

将券商流水中的"买卖类别"字符串映射为规范化事件类型。
精确映射优先，模糊匹配作为低置信度回退。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ── 规范化事件类型（19种） ─────────────────────────

class EventType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CASH_TRANSFER_IN = "CASH_TRANSFER_IN"
    CASH_TRANSFER_OUT = "CASH_TRANSFER_OUT"
    DIVIDEND_CASH = "DIVIDEND_CASH"
    DIVIDEND_STOCK = "DIVIDEND_STOCK"
    INTEREST = "INTEREST"
    SUBSCRIPTION = "SUBSCRIPTION"
    REDEMPTION = "REDEMPTION"
    ETF_CREATION = "ETF_CREATION"
    ETF_REDEMPTION = "ETF_REDEMPTION"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    ALLOTMENT = "ALLOTMENT"
    TAX = "TAX"
    FEE = "FEE"
    REVERSAL = "REVERSAL"
    CORRECTION = "CORRECTION"
    MARGIN_REPO = "MARGIN_REPO"         # 融券回购（资金融入）
    MARGIN_REPAY = "MARGIN_REPAY"       # 融券购回（资金偿还）
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassificationResult:
    event_type: EventType
    confidence: float          # 0.0 ~ 1.0
    reason: str                # 分类理由
    requires_manual: bool = False


# ── 精确映射表 ────────────────────────────────────

EXACT_MAPPING: dict[str, EventType] = {
    "港股通买入": EventType.BUY,
    "港股通卖出": EventType.SELL,
    "证券买入": EventType.BUY,
    "证券卖出": EventType.SELL,
    "OTC资金划入": EventType.CASH_TRANSFER_IN,
    "OTC资金划出": EventType.CASH_TRANSFER_OUT,
    "证券转银行": EventType.CASH_TRANSFER_OUT,
    "银行转证券": EventType.CASH_TRANSFER_IN,
    "利息归本": EventType.INTEREST,
    "融券回购": EventType.MARGIN_REPO,
    "融券购回": EventType.MARGIN_REPAY,
}

# 模糊回退规则（低置信度）
FUZZY_RULES: list[tuple[str, EventType, float]] = [
    ("买入", EventType.BUY, 0.6),
    ("卖出", EventType.SELL, 0.6),
    ("划入", EventType.CASH_TRANSFER_IN, 0.7),
    ("划出", EventType.CASH_TRANSFER_OUT, 0.7),
    ("分红", EventType.DIVIDEND_CASH, 0.8),
    ("股息", EventType.DIVIDEND_CASH, 0.8),
    ("利息", EventType.INTEREST, 0.7),
    ("申购", EventType.SUBSCRIPTION, 0.8),
    ("赎回", EventType.REDEMPTION, 0.8),
    ("冲正", EventType.REVERSAL, 0.7),
    ("更正", EventType.CORRECTION, 0.7),
    ("手续费", EventType.FEE, 0.8),
    ("印花税", EventType.TAX, 0.8),
]


class EventClassifier:
    """交易事件分类器。

    使用方式:
        classifier = EventClassifier()
        result = classifier.classify("港股通买入", quantity=1200)
    """

    def classify(self, raw_category: str, quantity: float = 0,
                 price: float = 0.0, symbol: str = "") -> ClassificationResult:
        """将券商原始类别映射为规范化事件类型。"""
        raw = raw_category.strip()

        # 1. 精确匹配
        if raw in EXACT_MAPPING:
            et = EXACT_MAPPING[raw]
            return ClassificationResult(
                event_type=et,
                confidence=1.0,
                reason=f"精确映射: {raw} → {et.value}",
            )

        # 2. 模糊回退
        for keyword, et, confidence in FUZZY_RULES:
            if keyword in raw:
                return ClassificationResult(
                    event_type=et,
                    confidence=confidence,
                    reason=f"模糊匹配: '{keyword}' in '{raw}' → {et.value}",
                    requires_manual=True,
                )

        # 3. 完全未知
        return ClassificationResult(
            event_type=EventType.UNKNOWN,
            confidence=0.0,
            reason=f"无法识别的类别: {raw}",
            requires_manual=True,
        )

    def generate_dictionary(self, raw_categories: list[str]) -> list[dict]:
        """从原始类别列表生成类别映射字典。"""
        results = []
        for cat in sorted(set(raw_categories)):
            result = self.classify(cat)
            results.append({
                "raw_category": cat,
                "normalized_event_type": result.event_type.value,
                "confidence": result.confidence,
                "reason": result.reason,
                "requires_manual_confirmation": result.requires_manual,
            })
        return results
