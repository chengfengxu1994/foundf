"""mihomo 外部控制器（Clash.Meta RESTful API）客户端。

只用 stdlib urllib，并显式装空 ProxyHandler：宿主 shell 常带
http_proxy=127.0.0.1:7890，若默认走代理会把 127.0.0.1:9090 的
控制器请求也送进代理里（build_stock_registry.py 的 502 教训）。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


class MihomoError(RuntimeError):
    """控制器调用失败。"""


class MihomoController:
    """mihomo external-controller 的薄封装。"""

    def __init__(self, base_url: str = "http://127.0.0.1:9090",
                 timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        # 空 ProxyHandler = 强制直连控制器，忽略环境代理变量
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ── 底层请求 ──────────────────────────────────────

    def _request(self, method: str, path: str,
                 body: dict[str, Any] | None = None,
                 query: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                payload = resp.read()
        except Exception as exc:  # URLError / TimeoutError / OSError
            raise MihomoError(f"mihomo 控制器 {method} {path} 失败: {exc}") from exc
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MihomoError(f"mihomo 控制器 {path} 返回非 JSON") from exc

    # ── 模式与节点 ────────────────────────────────────

    def get_mode(self) -> str:
        data = self._request("GET", "/configs")
        return str((data or {}).get("mode", ""))

    def set_mode(self, mode: str) -> None:
        if mode not in ("rule", "global", "direct"):
            raise ValueError(f"未知 mihomo 模式: {mode!r}")
        self._request("PATCH", "/configs", {"mode": mode})

    def get_group(self, group: str) -> dict[str, Any]:
        return self._request("GET", f"/proxies/{urllib.parse.quote(group)}")

    def current_node(self, group: str) -> str:
        return str(self.get_group(group).get("now", ""))

    def select_node(self, group: str, node: str) -> None:
        self._request("PUT", f"/proxies/{urllib.parse.quote(group)}",
                      {"name": node})

    # mihomo 组内固定出现的保留项，不是真实节点
    RESERVED_NAMES = frozenset({"DIRECT", "REJECT", "GLOBAL", "PASS"})

    def list_nodes(self, group: str,
                   exclude_patterns: tuple[str, ...] = ()) -> list[str]:
        """列出 Selector 组成员，剔除保留项与流量/套餐信息类伪节点。"""

        data = self.get_group(group)
        if data.get("type") != "Selector":
            raise MihomoError(
                f"代理组 {group!r} 类型为 {data.get('type')}，仅支持 Selector"
            )
        nodes = []
        for name in data.get("all", []):
            name = str(name)
            if name in self.RESERVED_NAMES:
                continue
            if any(pat in name for pat in exclude_patterns):
                continue
            nodes.append(name)
        return nodes

    def probe_delay(self, node: str, test_url: str,
                    timeout_ms: int) -> int | None:
        """返回延迟毫秒；节点不可用返回 None。"""

        try:
            data = self._request(
                "GET", f"/proxies/{urllib.parse.quote(node)}/delay",
                query={"timeout": timeout_ms, "url": test_url},
            )
        except MihomoError:
            return None
        delay = (data or {}).get("delay")
        return int(delay) if delay else None
