"""baostock SOCKS5 出口注入。

baostock 走私有 TCP 协议，HTTP 代理无效；这里用 PySocks 在 login
之前把进程内 socket.socket 替换为 socks 代理版本，登出后恢复。
仅在逃生路径使用，不改变默认直连行为。

注意：这是进程级 monkey-patch，作用域为整个解释器，务必只在
短时 backfill/采集脚本中使用，且与 EscapeSession 同生命周期。
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def socks_egress(host: str, port: int) -> Iterator[None]:
    """在 with 块内把所有新建 TCP 连接路由到 SOCKS5 代理。"""

    try:
        import socks
    except ImportError as exc:
        raise RuntimeError(
            "PySocks 未安装；请在 .venv 中安装 requirements.txt 的 PySocks"
        ) from exc

    original_socket = socket.socket
    socks.set_default_proxy(socks.SOCKS5, host, port)
    socket.socket = socks.socksocket  # type: ignore[assignment,misc]
    try:
        yield
    finally:
        socket.socket = original_socket  # type: ignore[assignment,misc]
        socks.set_default_proxy()
