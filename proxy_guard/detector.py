"""疑似封禁/限流检测。

把各数据源的异常归为两类：
- ban-like（值得换出口 IP 重试）：连接重置、限频提示、TLS/连接层错误
- 非 ban-like（换 IP 也没用）：参数错误、数据缺失、鉴权错误等
"""

from __future__ import annotations

# 异常消息中的限频/封禁关键字（tushare / 通用网关）
_BAN_MSG_KEYWORDS = (
    "最多访问",      # tushare 限频 "每分钟最多访问..."
    "限频",
    "访问频率",
    "too many requests",
    "rate limit",
    "429",
    "403",
    "forbidden",
    "blocked",
    "blacklist",
    "拉黑",
)

# 连接层异常类型名（不 import httpx，按类名匹配避免硬依赖）
_BAN_EXC_TYPES = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "TimeoutError",
)


def is_ban_like(exc: BaseException) -> bool:
    """判断异常是否疑似 IP 封禁/限流（值得换出口重试）。"""

    msg = str(exc).lower()
    if any(kw in msg for kw in _BAN_MSG_KEYWORDS):
        return True
    # 沿异常链检查连接层错误
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _BAN_EXC_TYPES:
            return True
        current = current.__cause__ or current.__context__
    return False
