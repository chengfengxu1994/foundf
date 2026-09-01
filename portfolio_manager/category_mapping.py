"""
Phase H2.2 category mapping.
Defines the canonical mapping from raw Chinese categories to normalized event types.
"""
from typing import Final

# ── Category mapping v3 ──────────────────────────────────────
# Each entry: raw -> {event_type, event_family, cash_direction, position_direction,
#                     security_type_scope, confidence, evidence, needs_review}

CATEGORY_MAP_V3: Final[dict] = {
    "证券买入": {
        "event_type": "BUY",
        "event_family": "EQUITY_TRADE",
        "cash_direction": "OUTFLOW",
        "position_direction": "INCREASE",
        "security_type_scope": "A_STOCK",
        "confidence": 1.0,
        "evidence": "A-share stock purchase: cash decreases, position increases",
        "needs_review": False,
    },
    "证券卖出": {
        "event_type": "SELL",
        "event_family": "EQUITY_TRADE",
        "cash_direction": "INFLOW",
        "position_direction": "DECREASE",
        "security_type_scope": "A_STOCK",
        "confidence": 1.0,
        "evidence": "A-share stock sale: cash increases, position decreases",
        "needs_review": False,
    },
    "港股通买入": {
        "event_type": "BUY",
        "event_family": "EQUITY_TRADE",
        "cash_direction": "OUTFLOW",
        "position_direction": "INCREASE",
        "security_type_scope": "HONGKONG_STOCK",
        "confidence": 1.0,
        "evidence": "HK Connect stock purchase: cash decreases, position increases",
        "needs_review": False,
    },
    "港股通卖出": {
        "event_type": "SELL",
        "event_family": "EQUITY_TRADE",
        "cash_direction": "INFLOW",
        "position_direction": "DECREASE",
        "security_type_scope": "HONGKONG_STOCK",
        "confidence": 1.0,
        "evidence": "HK Connect stock sale: cash increases, position decreases",
        "needs_review": False,
    },
    "银行转证券": {
        "event_type": "CASH_TRANSFER_IN",
        "event_family": "CASH_MOVEMENT",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "NONE",
        "confidence": 1.0,
        "evidence": "Bank to securities transfer: cash increases, no position change",
        "needs_review": False,
    },
    "证券转银行": {
        "event_type": "CASH_TRANSFER_OUT",
        "event_family": "CASH_MOVEMENT",
        "cash_direction": "OUTFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "NONE",
        "confidence": 1.0,
        "evidence": "Securities to bank transfer: cash decreases, no position change",
        "needs_review": False,
    },
    "OTC资金划入": {
        "event_type": "CASH_TRANSFER_IN",
        "event_family": "CASH_MOVEMENT",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "NONE",
        "confidence": 1.0,
        "evidence": "OTC fund transfer in: cash increases, no position change",
        "needs_review": False,
    },
    "OTC资金划出": {
        "event_type": "CASH_TRANSFER_OUT",
        "event_family": "CASH_MOVEMENT",
        "cash_direction": "OUTFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "NONE",
        "confidence": 1.0,
        "evidence": "OTC fund transfer out: cash decreases, no position change",
        "needs_review": False,
    },
    "利息归本": {
        "event_type": "INTEREST",
        "event_family": "PASSIVE_INCOME",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "NONE",
        "confidence": 0.9,
        "evidence": "Interest credited to principal: cash increases, no position change",
        "needs_review": False,
    },
    "融券回购": {
        "event_type": "UNKNOWN",
        "event_family": "UNKNOWN",
        "cash_direction": "UNKNOWN",
        "position_direction": "UNKNOWN",
        "security_type_scope": "UNKNOWN",
        "confidence": 0.0,
        "evidence": "Insufficient evidence: need to check security code/name, cash delta, position delta",
        "needs_review": True,
    },
    "融券回购": {
        "event_type": "UNKNOWN",
        "event_family": "UNKNOWN",
        "cash_direction": "UNKNOWN",
        "position_direction": "UNKNOWN",
        "security_type_scope": "UNKNOWN",
        "confidence": 0.0,
        "evidence": "Insufficient evidence: need to check security code/name, cash delta, position delta",
        "needs_review": True,
    },
    "融券购回": {
        "event_type": "UNKNOWN",
        "event_family": "UNKNOWN",
        "cash_direction": "UNKNOWN",
        "position_direction": "UNKNOWN",
        "security_type_scope": "UNKNOWN",
        "confidence": 0.0,
        "evidence": "Insufficient evidence: need to check security code/name, cash delta, position delta",
        "needs_review": True,
    },
    # Real categories discovered in pdfplumber extraction
    "港股通组合费": {
        "event_type": "FEE",
        "event_family": "FEE_AND_CHARGE",
        "cash_direction": "OUTFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "HONGKONG_STOCK",
        "confidence": 1.0,
        "evidence": "HK Connect portfolio fee: recurring fee, no position change",
        "needs_review": False,
    },
    "港股通红利发放": {
        "event_type": "DIVIDEND",
        "event_family": "PASSIVE_INCOME",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "HONGKONG_STOCK",
        "confidence": 1.0,
        "evidence": "HK stock dividend disbursement: cash increases no position change",
        "needs_review": False,
    },
    "红利入账": {
        "event_type": "DIVIDEND",
        "event_family": "PASSIVE_INCOME",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "A_STOCK",
        "confidence": 1.0,
        "evidence": "A-share dividend credited: cash increases no position change",
        "needs_review": False,
    },
    "新股申购": {
        "event_type": "IPO_SUBSCRIPTION",
        "event_family": "CAPITAL_EVENT",
        "cash_direction": "OUTFLOW",
        "position_direction": "TEMPORARY_FREEZE",
        "security_type_scope": "A_STOCK",
        "confidence": 1.0,
        "evidence": "IPO subscription: cash frozen temporarily, pending allotment result",
        "needs_review": False,
    },
    "股息红利差异扣税": {
        "event_type": "TAX_ADJUSTMENT",
        "event_family": "TAX",
        "cash_direction": "OUTFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "A_STOCK",
        "confidence": 1.0,
        "evidence": "Dividend tax difference withholding: cash decrease, no position change",
        "needs_review": False,
    },
    "基金投顾资金转入": {
        "event_type": "CASH_TRANSFER_IN",
        "event_family": "CASH_MOVEMENT",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "FUND",
        "confidence": 0.9,
        "evidence": "Fund advisory account transfer in: cash increases, no position change",
        "needs_review": False,
    },
    "基金投顾资金转出": {
        "event_type": "CASH_TRANSFER_OUT",
        "event_family": "CASH_MOVEMENT",
        "cash_direction": "OUTFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "FUND",
        "confidence": 0.9,
        "evidence": "Fund advisory account transfer out: cash decreases, no position change",
        "needs_review": False,
    },
    "申购还款": {
        "event_type": "IPO_REFUND",
        "event_family": "CAPITAL_EVENT",
        "cash_direction": "INFLOW",
        "position_direction": "NO_CHANGE",
        "security_type_scope": "A_STOCK",
        "confidence": 0.9,
        "evidence": "IPO subscription refund: unallotted funds returned, no position change",
        "needs_review": False,
    },
    "配售缴款": {
        "event_type": "PLACEMENT_PAYMENT",
        "event_family": "CAPITAL_EVENT",
        "cash_direction": "OUTFLOW",
        "position_direction": "INCREASE",
        "security_type_scope": "A_STOCK",
        "confidence": 0.9,
        "evidence": "Rights/placement payment: cash decrease, position increase upon allotment",
        "needs_review": False,
    },
}

# Build reverse index
EVENT_TYPE_TO_RAW: Final[dict] = {}
for raw, info in CATEGORY_MAP_V3.items():
    et = info["event_type"]
    EVENT_TYPE_TO_RAW.setdefault(et, []).append(raw)


def map_category(raw_category: str) -> dict:
    """Return mapping info for a raw category, or UNKNOWN if not found."""
    info = CATEGORY_MAP_V3.get(raw_category)
    if info is None:
        return {
            "event_type": "UNKNOWN",
            "event_family": "UNKNOWN",
            "cash_direction": "UNKNOWN",
            "position_direction": "UNKNOWN",
            "security_type_scope": "UNKNOWN",
            "confidence": 0.0,
            "evidence": "Category not in mapping table",
            "needs_review": True,
        }
    return dict(info)


def is_transaction(raw_category: str) -> bool:
    """True if this category represents a trade (not cash movement, not interest)."""
    info = CATEGORY_MAP_V3.get(raw_category)
    if info is None:
        return False
    return info["event_type"] in ("BUY", "SELL")


def is_cash_event(raw_category: str) -> bool:
    """True if this category represents a cash-only event."""
    info = CATEGORY_MAP_V3.get(raw_category)
    if info is None:
        return False
    return info["event_type"] in ("CASH_TRANSFER_IN", "CASH_TRANSFER_OUT", "INTEREST")
