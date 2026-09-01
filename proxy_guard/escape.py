"""逃生会话：疑似封禁时经 mihomo 切换出口节点重试。

用法（接入点）：

    cfg = load_config()
    if cfg.enabled and is_ban_like(exc):
        with EscapeSession(cfg) as escape:
            for _ in escape.attempts():
                try:
                    ...  # 带 escape.http_proxies 或 socks_egress 重试
                    escape.mark_success()
                    break
                except Exception as retry_exc:
                    escape.mark_failure(retry_exc)

进入会话会临时把 mihomo 切到 global 模式并在配置的 Selector 组
（默认 GLOBAL，rule 模式下闲置）内轮换节点；退出时无论成败都恢复
原模式与原节点。轮换池耗尽抛 ProxyExhaustedError（fail-closed）。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import GuardConfig
from .mihomo import MihomoController, MihomoError


class ProxyExhaustedError(RuntimeError):
    """逃生池耗尽：所有候选节点都不可用或仍在冷却。"""


class _State:
    """data/proxy_guard/state.json 的读写（冷却名单 + 事件日志）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"cooldowns": {}, "events": []}
        if path.is_file():
            try:
                with path.open(encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self.data["cooldowns"] = dict(loaded.get("cooldowns") or {})
                    self.data["events"] = list(loaded.get("events") or [])
            except (json.JSONDecodeError, OSError):
                pass  # 状态损坏不阻塞逃生，按空状态继续

    def in_cooldown(self, node: str, now: datetime) -> bool:
        until = self.data["cooldowns"].get(node)
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > now
        except ValueError:
            return False

    def set_cooldown(self, node: str, until: datetime) -> None:
        self.data["cooldowns"][node] = until.isoformat()

    def log_event(self, event: dict[str, Any]) -> None:
        self.data["events"].append(event)
        self.data["events"] = self.data["events"][-200:]  # 只留最近 200 条

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
        tmp.replace(self.path)


class EscapeSession(AbstractContextManager["EscapeSession"]):
    """一次逃生：切 global + 在 Selector 组内轮换健康节点。"""

    def __init__(self, config: GuardConfig,
                 controller: MihomoController | None = None,
                 reason: str = "") -> None:
        if not config.enabled:
            raise RuntimeError("proxy_guard 未启用（config 缺失或 enabled=false）")
        self.config = config
        self.controller = controller or MihomoController(
            config.controller_url, config.request_timeout_s
        )
        self.reason = reason
        self._state = _State(config.state_file)
        self._orig_mode: str | None = None
        self._orig_node: str | None = None
        self._pool: list[str] = []
        self._tried: list[str] = []
        self.current_node: str | None = None
        self.success = False
        self._prev_handlers: dict[int, Any] = {}

    # ── 上下文管理 ────────────────────────────────────

    def __enter__(self) -> "EscapeSession":
        self._orig_mode = self.controller.get_mode()
        self._orig_node = self.controller.current_node(self.config.selector_group)
        self._pool = self.controller.list_nodes(
            self.config.selector_group, self.config.node_exclude_patterns
        )
        if self._orig_mode != "global":
            self.controller.set_mode("global")
        self._install_signal_guard()
        return self

    def _install_signal_guard(self) -> None:
        """SIGTERM/SIGINT 时先恢复 mihomo 再走默认语义。

        逃生窗口若被 `timeout` 等终止（默认 SIGTERM），没有守卫会把
        mihomo 丢在 global 模式。SIGKILL 无法拦截，仍有
        `python3 -m proxy_guard restore` 兜底。仅主线程可装信号处理器。
        """

        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum: int, frame: Any) -> None:
            self._restore()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._prev_handlers[sig] = signal.signal(sig, _handler)

    def _remove_signal_guard(self) -> None:
        for sig, prev in self._prev_handlers.items():
            signal.signal(sig, prev)
        self._prev_handlers.clear()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._remove_signal_guard()
        self._restore()
        self._state.log_event({
            "at": datetime.now(timezone.utc).isoformat(),
            "reason": self.reason,
            "tried": self._tried,
            "success": self.success,
            "error": str(exc) if exc else None,
        })
        self._state.save()
        return False  # 不吞异常

    def _restore(self) -> None:
        try:
            if self._orig_node and self._orig_node != self.controller.current_node(
                self.config.selector_group
            ):
                self.controller.select_node(self.config.selector_group,
                                            self._orig_node)
        except MihomoError as exc:
            print(f"proxy_guard: 恢复节点失败: {exc}", file=sys.stderr)
        try:
            if self._orig_mode and self._orig_mode != "global":
                self.controller.set_mode(self._orig_mode)
        except MihomoError as exc:
            print(f"proxy_guard: 恢复模式失败: {exc}", file=sys.stderr)

    # ── 节点轮换 ──────────────────────────────────────

    def next_egress(self) -> str:
        """切到下一个健康节点，返回节点名；池耗尽抛 ProxyExhaustedError。"""

        now = datetime.now(timezone.utc)
        cooldown_until = now + timedelta(minutes=self.config.cooldown_minutes)
        budget = self.config.max_nodes_per_escape
        for node in self._pool:
            if node in self._tried or len(self._tried) >= budget:
                continue
            if self._state.in_cooldown(node, now):
                continue
            self._tried.append(node)
            if self.controller.probe_delay(
                node, self.config.test_url, self.config.probe_timeout_ms
            ) is None:
                self._state.set_cooldown(node, cooldown_until)
                continue
            self.controller.select_node(self.config.selector_group, node)
            self.current_node = node
            return node
        raise ProxyExhaustedError(
            f"逃生池耗尽（组 {self.config.selector_group}，"
            f"已试 {self._tried}，池 {len(self._pool)} 个节点）"
        )

    def attempts(self) -> Iterator[int]:
        """最多 max_nodes_per_escape 次轮换的迭代器（每次需自行重试业务调用）。"""

        for i in range(self.config.max_nodes_per_escape):
            yield i

    def mark_success(self) -> None:
        self.success = True

    def mark_failure(self, exc: BaseException) -> None:
        if self.current_node:
            until = datetime.now(timezone.utc) + timedelta(
                minutes=self.config.cooldown_minutes
            )
            self._state.set_cooldown(self.current_node, until)

    # ── 出口形式 ──────────────────────────────────────

    @property
    def proxy_url(self) -> str:
        return self.config.proxy_url

    @property
    def urllib_proxies(self) -> dict[str, str]:
        return self.config.urllib_proxies

    @property
    def socks_addr(self) -> tuple[str, int]:
        return self.config.socks_addr
