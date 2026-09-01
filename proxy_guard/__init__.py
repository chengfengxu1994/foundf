"""proxy_guard — mihomo 节点轮换防封禁子模块（逃生式，默认关闭）。

定位：baostock / tushare / eastmoney 等数据源被 IP 拉黑或限频时，
通过宿主机 mihomo（Clash.Meta）的外部控制器切换出口节点重试，
给采集链路多一次逃生机会。默认直连，行为零变化；仅在
`config/proxy_guard.json` 存在且 `enabled=true` 时启用。

核心机制（为什么用 GLOBAL 组）：
    mihomo rule 模式下经 mixed 端口的 CN 域名流量按规则走 DIRECT，
    切换普通 Selector 组不会改变 CN 数据源出口。GLOBAL 组仅在
    global 模式下参与路由，rule 模式下是闲置的——逃生时临时切
    `mode=global` 并在 GLOBAL 组内轮换节点，即可控制出口 IP，
    退出时恢复原 mode/节点，对其他流量影响窗口最小。

边界：
    - 不开 TUN、不改 /etc/mihomo 配置（需 root 且影响全机流量）
    - 容器 collector 不接入（mihomo 只绑 127.0.0.1；config 缺席即 no-op）
    - baostock 单会话约束不变：逃生重试仍是串行 login/logout

存储: data/proxy_guard/state.json （失败节点冷却 + 逃生事件日志）
配置: config/proxy_guard.json （样例见 config/proxy_guard.example.json）
CLI:  python3 -m proxy_guard status | probe [--top N] | escape-test | baostock-test
"""

from __future__ import annotations

import sys

from .config import GuardConfig, load_config
from .detector import is_ban_like
from .escape import EscapeSession, ProxyExhaustedError

__all__ = [
    "EscapeSession",
    "GuardConfig",
    "ProxyExhaustedError",
    "is_ban_like",
    "load_config",
    "main",
]


def main(argv: list[str] | None = None) -> None:
    from .cli import run

    run(sys.argv[1:] if argv is None else argv)
