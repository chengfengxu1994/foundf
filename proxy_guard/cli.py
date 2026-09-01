"""proxy_guard 命令行：status / probe / escape-test / baostock-test。

test 类子命令是显式人工触发的冒烟测试，即使 config 未启用也强制
以默认参数执行（不影响生产路径的 no-op 语义）。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import replace

from .baostock_socks import socks_egress
from .config import GuardConfig, load_config
from .escape import EscapeSession, ProxyExhaustedError
from .mihomo import MihomoController, MihomoError

USAGE = """用法: python3 -m proxy_guard <command>

  status                  当前 mihomo 模式 / 选择组节点 / 池大小（只读）
  probe [--top N]         对前 N 个节点测延迟（只读，默认 5）
  escape-test             完整逃生冒烟：切 global→换节点→对比出口 IP→恢复
  baostock-test           baostock 直连 vs SOCKS 代理登录对照（远离 17:15 窗口）
  restore [--node NAME]   应急恢复：mode 切回 rule（可选同时指定 GLOBAL 节点）
                          （逃生进程被 SIGKILL 后的手动兜底；SIGTERM/SIGINT 已自动恢复）
"""


def _test_config() -> GuardConfig:
    cfg = load_config()
    if not cfg.enabled:
        cfg = replace(cfg, enabled=True)
    return cfg


IP_ECHO_URLS = ("https://api.ip.sb/ip", "https://myip.ipip.net")


def _fetch_ip(proxies: dict[str, str] | None, timeout: float = 10.0) -> str:
    handler = urllib.request.ProxyHandler(proxies or {})
    opener = urllib.request.build_opener(handler)
    last_exc: Exception | None = None
    for url in IP_ECHO_URLS:
        try:
            with opener.open(url, timeout=timeout) as resp:
                text = resp.read().decode().strip()
            # myip.ipip.net 返回 "当前 IP：x 来自于：..."，取首个 IP 形字段
            for token in text.replace("：", " ").split():
                if "." in token or ":" in token:
                    return token
            return text
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"IP 回显服务全部不可达: {last_exc}")


def _cmd_status(cfg: GuardConfig) -> int:
    ctl = MihomoController(cfg.controller_url, cfg.request_timeout_s)
    print(f"controller : {cfg.controller_url}")
    print(f"mode       : {ctl.get_mode()}")
    for group in ("GLOBAL", "良心云", "自动选择", "故障转移"):
        try:
            data = ctl.get_group(group)
        except MihomoError:
            continue
        print(f"group {group:<8}: type={data.get('type')} now={data.get('now')}")
    nodes = ctl.list_nodes(cfg.selector_group, cfg.node_exclude_patterns)
    print(f"pool {cfg.selector_group}    : {len(nodes)} 个可用节点（已剔除信息伪节点）")
    return 0


def _cmd_probe(cfg: GuardConfig, top: int) -> int:
    ctl = MihomoController(cfg.controller_url, cfg.request_timeout_s)
    nodes = ctl.list_nodes(cfg.selector_group, cfg.node_exclude_patterns)[:top]
    for node in nodes:
        delay = ctl.probe_delay(node, cfg.test_url, cfg.probe_timeout_ms)
        print(f"{'OK' if delay is not None else 'FAIL':>4} "
              f"{delay if delay is not None else '-':>5} ms  {node}")
    return 0


def _cmd_escape_test(cfg: GuardConfig) -> int:
    ctl = MihomoController(cfg.controller_url, cfg.request_timeout_s)
    direct_ip = _fetch_ip(None)
    print(f"直连出口 IP : {direct_ip}")
    print(f"原 mode/node: {ctl.get_mode()} / {ctl.current_node(cfg.selector_group)}")
    changed = False
    with EscapeSession(cfg, reason="cli-escape-test") as escape:
        for _ in escape.attempts():
            try:
                node = escape.next_egress()
            except ProxyExhaustedError as exc:
                print(f"池耗尽: {exc}")
                return 1
            try:
                proxy_ip = _fetch_ip(escape.urllib_proxies)
            except Exception as exc:
                print(f"节点 {node} -> 回显不可达 ({exc})，换下一个")
                escape.mark_failure(exc)
                continue
            print(f"节点 {node} -> 出口 IP {proxy_ip}")
            if proxy_ip != direct_ip:
                changed = True
                escape.mark_success()
                break
            escape.mark_failure(RuntimeError("出口 IP 未变化"))
    print(f"恢复后 mode/node: {ctl.get_mode()} / {ctl.current_node(cfg.selector_group)}")
    print(f"结论: {'出口 IP 已改变，逃生有效' if changed else '出口 IP 未改变'}")
    return 0 if changed else 1


def _baostock_login_query(tag: str) -> tuple[bool, str]:
    import baostock as bs

    login = bs.login()
    if str(login.error_code) != "0":
        return False, f"{tag}: 登录失败 {login.error_msg}"
    try:
        # 冒烟查询用 query_profit_data（财报回填主路径）。
        # 注意：baostock 服务端异常时（如周末维护窗口）客户端 next() 会
        # 100% CPU 空转且不抛错（2026-08-29 实测三个接口均复现，
        # AGENTS.md 有记录）——本测试若挂起即为此情况，非 proxy_guard 故障。
        result = bs.query_profit_data(code="sh.600000", year=2026, quarter=1)
        if str(result.error_code) != "0":
            return False, f"{tag}: 查询失败 {result.error_msg}"
        rows = 0
        while result.next():
            rows += 1
        return True, f"{tag}: 登录成功，基本面查询返回 {rows} 行"
    finally:
        bs.logout()


def _cmd_baostock_test(cfg: GuardConfig) -> int:
    ok, msg = _baostock_login_query("直连")
    print(msg)
    if not ok:
        print("直连已失败，直接进入逃生对照")
    escaped = False
    with EscapeSession(cfg, reason="cli-baostock-test") as escape:
        for _ in escape.attempts():
            try:
                node = escape.next_egress()
            except ProxyExhaustedError as exc:
                print(f"池耗尽: {exc}")
                return 1
            host, port = escape.socks_addr
            try:
                with socks_egress(host, port):
                    ok2, msg2 = _baostock_login_query(f"代理[{node}]")
                print(msg2)
                if ok2:
                    escape.mark_success()
                    escaped = True
                    break
                escape.mark_failure(RuntimeError(msg2))
            except Exception as exc:  # noqa: BLE001 - 冒烟测试需要全捕获继续轮换
                print(f"代理[{node}]: 异常 {exc}")
                escape.mark_failure(exc)
    print(f"结论: {'代理路径登录取数成功' if escaped else '代理路径全部失败'}")
    return 0 if escaped else 1


def _cmd_restore(cfg: GuardConfig, node: str | None) -> int:
    ctl = MihomoController(cfg.controller_url, cfg.request_timeout_s)
    if node:
        ctl.select_node(cfg.selector_group, node)
    ctl.set_mode("rule")
    print(f"已恢复: mode={ctl.get_mode()} "
          f"{cfg.selector_group}.now={ctl.current_node(cfg.selector_group)}")
    return 0


def run(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        sys.exit(0 if argv else 2)

    command, rest = argv[0], argv[1:]
    test_commands = {"escape-test", "baostock-test"}
    cfg = _test_config() if command in test_commands else load_config()
    if command not in test_commands and not cfg.enabled \
            and command not in ("probe", "status", "restore"):
        sys.stderr.write("proxy_guard 未启用：复制 config/proxy_guard.example.json "
                         "为 config/proxy_guard.json 并置 enabled=true\n")
        sys.exit(2)

    handlers = {
        "status": lambda: _cmd_status(cfg),
        "probe": lambda: _cmd_probe(cfg, _parse_top(rest)),
        "escape-test": lambda: _cmd_escape_test(cfg),
        "baostock-test": lambda: _cmd_baostock_test(cfg),
        "restore": lambda: _cmd_restore(cfg, _parse_node(rest)),
    }
    handler = handlers.get(command)
    if handler is None:
        sys.stderr.write(f"未知命令: {command}\n{USAGE}")
        sys.exit(2)
    try:
        sys.exit(handler())
    except MihomoError as exc:
        sys.stderr.write(f"控制器不可达: {exc}\n")
        sys.exit(1)


def _parse_top(rest: list[str]) -> int:
    if "--top" in rest:
        idx = rest.index("--top")
        try:
            return int(rest[idx + 1])
        except (IndexError, ValueError):
            pass
    return 5


def _parse_node(rest: list[str]) -> str | None:
    if "--node" in rest:
        idx = rest.index("--node")
        if idx + 1 < len(rest):
            return rest[idx + 1]
    return None
