"""proxy_guard 配置加载。

`config/proxy_guard.json` 缺失或 `enabled=false` 时所有接入点均为 no-op。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path("config/proxy_guard.json")
STATE_DIR = Path("data/proxy_guard")
STATE_FILE = STATE_DIR / "state.json"

DEFAULT_EXCLUDE_PATTERNS = ["剩余流量", "套餐到期", "距离下次重置"]


@dataclass(frozen=True)
class GuardConfig:
    """mihomo 控制器与逃生策略配置。"""

    enabled: bool = False
    controller_url: str = "http://127.0.0.1:9090"
    mixed_host: str = "127.0.0.1"
    mixed_port: int = 7890
    selector_group: str = "GLOBAL"
    test_url: str = "http://www.gstatic.com/generate_204"
    probe_timeout_ms: int = 3000
    node_exclude_patterns: tuple[str, ...] = tuple(DEFAULT_EXCLUDE_PATTERNS)
    cooldown_minutes: int = 60
    max_nodes_per_escape: int = 5
    request_timeout_s: float = 5.0
    state_file: Path = field(default=STATE_FILE)

    @property
    def proxy_url(self) -> str:
        """httpx `proxy=` 参数用的单 URL。"""
        return f"http://{self.mixed_host}:{self.mixed_port}"

    @property
    def urllib_proxies(self) -> dict[str, str]:
        """urllib ProxyHandler / requests 通用的 proxies 字典。"""
        return {"http": self.proxy_url, "https": self.proxy_url}

    @property
    def socks_addr(self) -> tuple[str, int]:
        return (self.mixed_host, self.mixed_port)


def load_config(path: Path | str = CONFIG_PATH) -> GuardConfig:
    """读取配置；文件缺失或 enabled=false 返回禁用的默认实例。"""

    path = Path(path)
    if not path.is_file():
        return GuardConfig()
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"proxy_guard 配置必须是 JSON 对象: {path}")
    patterns = raw.get("node_exclude_patterns", DEFAULT_EXCLUDE_PATTERNS)
    return GuardConfig(
        enabled=bool(raw.get("enabled", False)),
        controller_url=str(raw.get("controller_url", "http://127.0.0.1:9090")).rstrip("/"),
        mixed_host=str(raw.get("mixed_host", "127.0.0.1")),
        mixed_port=int(raw.get("mixed_port", 7890)),
        selector_group=str(raw.get("selector_group", "GLOBAL")),
        test_url=str(raw.get("test_url", "http://www.gstatic.com/generate_204")),
        probe_timeout_ms=int(raw.get("probe_timeout_ms", 3000)),
        node_exclude_patterns=tuple(patterns),
        cooldown_minutes=int(raw.get("cooldown_minutes", 60)),
        max_nodes_per_escape=int(raw.get("max_nodes_per_escape", 5)),
        request_timeout_s=float(raw.get("request_timeout_s", 5.0)),
    )
