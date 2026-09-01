#!/usr/bin/env python3
"""phone_ctl 传输适配层: deploy/phone 全部 adb 传输统一经此模块。

设计约束:
- phone_ctl 包位置: 必须由 env PHONE_CTL_HOME 指定，或已安装在 Python 环境中；
  import 失败 fail-closed 抛 ImportError(不静默回退裸 adb——传输层不明确宁可报错)。
- 设备钉定: 仅从 FOUNDF_ADB_SERIAL 读取；未配置时使用 phone_ctl 的单设备语义。
- uiautomator 重试(抗系统 SIGKILL exit 137 / 空文件 / 截断 XML)在本层 ui_xml:
  最多 5 次、间隔 3s, 与迁移前 capture_ths_sim.dump_ui 行为一致。
- CLI 供 deploy/phone/phone_*.sh 调用; 交互会话可改用 phone MCP
  (见 .kimi-code/mcp.json), 自动化脚本一律走本库、不经 MCP。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import stat
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

_CTL_HOME = os.getenv("PHONE_CTL_HOME", "")
if _CTL_HOME and _CTL_HOME not in sys.path:
    sys.path.insert(0, _CTL_HOME)
try:
    from phone_ctl.adb import Phone, ADBError
except ImportError as exc:
    raise ImportError(
        "无法导入 phone_ctl(adb 传输层)。请确认 phone_ctl 仓库存在, "
        "或用环境变量 PHONE_CTL_HOME 指向其所在目录 "
        f"(当前 PHONE_CTL_HOME: {_CTL_HOME or '未设置'})。"
    ) from exc

REMOTE_XML = "/sdcard/.foundf_ui.xml"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATTERN_FILE = REPO_ROOT / ".secrets" / "phone_unlock_pattern"
DEVICE_LOCK_FILE = REPO_ROOT / "data" / "runtime" / "phone-ui.lock"
UNLOCK_FAILURE_FILE = REPO_ROOT / "data" / "runtime" / "phone-unlock-failed.json"
GESTURE_HELPER_REMOTE = "/data/local/tmp/foundf-pattern-gesture.jar"

_phone: Phone | None = None

# 手机产物含账户页面与执行记录；新建文件默认仅所有者可写、同组可读。
# 不修改任何既有文件权限。
os.umask(0o027)


class ScreenState(str, Enum):
    SCREEN_OFF = "SCREEN_OFF"
    LOCKED = "LOCKED"
    READY = "READY"


class PhoneNotReadyError(RuntimeError):
    """设备无法安全进入可操作状态；消息不得包含解锁凭据。"""


def screen_state() -> ScreenState:
    """以 Android 系统真值区分息屏、锁屏与可操作状态。"""

    power = shell("dumpsys power", timeout=15)
    awake = bool(re.search(r"mWakefulness=Awake|Display Power: state=ON", power))
    if not awake:
        return ScreenState.SCREEN_OFF
    window = shell("dumpsys window", timeout=15)
    locked = any(re.search(pattern, window, re.IGNORECASE) for pattern in (
        r"mDreamingLockscreen=true",
        r"mShowingLockscreen=true",
        r"isStatusBarKeyguard=true",
        r"mInputRestricted=true",
        r"showing=true.*secure=true",
    ))
    return ScreenState.LOCKED if locked else ScreenState.READY


def _unlock_pattern() -> str:
    """从环境或权限隔离文件读取图案；任何错误都不回显内容。"""

    value = os.getenv("FOUNDF_PHONE_UNLOCK_PATTERN")
    if value is None:
        try:
            mode = stat.S_IMODE(DEFAULT_PATTERN_FILE.stat().st_mode)
            if mode not in (0o400, 0o600):
                raise PhoneNotReadyError("设备解锁凭据文件权限不安全")
            value = DEFAULT_PATTERN_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PhoneNotReadyError("设备已锁定且未配置解锁凭据") from exc
    if not re.fullmatch(r"[1-9]{4,9}", value or "") or len(set(value)) != len(value):
        raise PhoneNotReadyError("设备解锁凭据格式无效")
    return value


def _pattern_points(
    pattern: str,
    bounds: tuple[int, int, int, int],
) -> list[tuple[int, int]]:
    """按 SystemUI 实际 lockPatternView 边界映射九宫格中心。"""

    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise PhoneNotReadyError("锁屏图案控件边界无效")
    xs = tuple(round(left + width * fraction / 6) for fraction in (1, 3, 5))
    ys = tuple(round(top + height * fraction / 6) for fraction in (1, 3, 5))
    return [(xs[(int(n) - 1) % 3], ys[(int(n) - 1) // 3]) for n in pattern]


def _pattern_control_bounds() -> tuple[int, int, int, int] | None:
    """返回 SystemUI 图案控件真边界；缺失或畸形时拒绝猜坐标。"""

    try:
        root = ET.fromstring(ui_xml(retries=2))
    except (RuntimeError, ET.ParseError):
        return None
    for node in root.iter("node"):
        resource_id = (node.get("resource-id") or "").lower()
        if "lockpatternview" in resource_id or "lock_pattern_view" in resource_id:
            match = re.fullmatch(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                node.get("bounds") or "",
            )
            if match:
                return tuple(map(int, match.groups()))
    return None


def _unlock() -> None:
    pattern = _unlock_pattern()
    bounds = _pattern_control_bounds()
    if bounds is None:
        width, height = screen_size()
        if width <= 0 or height <= 0:
            raise PhoneNotReadyError("无法取得有效屏幕尺寸")
        # HyperOS 时钟锁屏需先上滑进入认证页。必须绕过本模块 swipe 包装，
        # 否则其 READY 前置检查会递归回到解锁流程。
        get_phone().swipe(
            round(width * 0.5), round(height * 0.82),
            round(width * 0.5), round(height * 0.25), 350,
        )
        time.sleep(1.0)
        bounds = _pattern_control_bounds()
        if bounds is None:
            raise PhoneNotReadyError("锁屏认证页未显示图案控件")
    points = _pattern_points(pattern, bounds)
    # Android ``input motionevent`` 每次调用会重置 downTime，无法形成真正的
    # 连续折线。使用部署在 /data/local/tmp 的 UiAutomator helper 一次注入整条
    # polyline；参数只有计算后的控件坐标，不含原始凭据。
    encoded = ";".join(f"{x},{y}" for x, y in points)
    command = (
        f"uiautomator runtest {GESTURE_HELPER_REMOTE} "
        "-c foundf.FoundFPatternGesture -e points " + shlex.quote(encoded)
    )
    try:
        shell(f"test -r {GESTURE_HELPER_REMOTE}", timeout=10)
        shell(command, timeout=30)
    except Exception:
        raise PhoneNotReadyError("连续解锁手势 helper 不可用") from None
    time.sleep(1.5)


def _record_unlock_failure() -> None:
    UNLOCK_FAILURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = UNLOCK_FAILURE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"status": "LOCKED_AFTER_ONE_ATTEMPT"}) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, UNLOCK_FAILURE_FILE)


def _clear_unlock_failure() -> None:
    try:
        UNLOCK_FAILURE_FILE.unlink()
    except FileNotFoundError:
        pass


def _screen_state_with_retry(attempts: int = 6, interval: float = 5.0) -> ScreenState:
    """adb server 重启后设备重新枚举需要数秒（2026-08-31 10:12 调仓事故：
    daemon 恰好死亡重启，首次 shell 报 no devices 直接崩掉 capture）。
    撞 ADBError 时短窗重试，最终失败升格为 PhoneNotReadyError fail-closed。"""

    last_error: ADBError | None = None
    for attempt in range(attempts):
        try:
            return screen_state()
        except ADBError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(interval)
    raise PhoneNotReadyError(f"adb 传输不可用: {last_error}")


def ensure_ready() -> ScreenState:
    """必要时唤醒并按授权图案解锁，随后用系统真值复核。"""

    state = _screen_state_with_retry()
    if state is ScreenState.SCREEN_OFF:
        # phone_ctl 只暴露跨设备稳定的 POWER，不接受 Android keyevent
        # 别名 WAKEUP；当前真值已确认息屏，POWER 在此只承担唤醒语义。
        get_phone().press_key("POWER")
        time.sleep(1.0)
        state = _screen_state_with_retry()
    if state is ScreenState.LOCKED:
        if UNLOCK_FAILURE_FILE.exists():
            raise PhoneNotReadyError("自动解锁已熔断，需人工解锁一次后复位")
        _unlock()
        state = _screen_state_with_retry()
        if state is ScreenState.LOCKED:
            _record_unlock_failure()
    if state is not ScreenState.READY:
        raise PhoneNotReadyError(f"设备未就绪: {state.value}")
    _clear_unlock_failure()
    return state


@contextmanager
def device_lock(timeout: float = 0.0):
    """跨进程串行化手机 UI；默认已有操作者时立即 fail-closed。"""

    DEVICE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DEVICE_LOCK_FILE.open("a+") as handle:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise PhoneNotReadyError("手机 UI 正被另一任务操作") from exc
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        try:
            ensure_ready()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def get_phone() -> Phone:
    """进程内单例；序列号未配置或置空时使用 phone_ctl 单设备语义。"""

    global _phone
    if _phone is None:
        serial = os.getenv("FOUNDF_ADB_SERIAL")
        _phone = Phone(serial=serial or None)
    return _phone


def shell(cmd: str, timeout: int = 60) -> str:
    return get_phone().shell(cmd, timeout=timeout)


def screenshot(local_png: Path) -> None:
    Path(local_png).write_bytes(get_phone().screenshot_png())


def ui_xml(retries: int = 5) -> str:
    """抓取当前界面 UI 树 XML; uiautomator 偶发被 SIGKILL(exit 137)、
    产出空文件或截断 XML, 重试若干次(间隔 3s)。"""

    last: Exception | None = None
    for _ in range(retries):
        try:
            phone = get_phone()
            phone.shell(f"uiautomator dump {REMOTE_XML}", timeout=30)
            xml = phone.shell(f"cat {REMOTE_XML}", timeout=15)
            if not xml.strip():
                raise RuntimeError("uiautomator 产出空文件")
            ET.fromstring(xml)  # 截断 XML 视为失败, 走重试
            return xml
        except Exception as exc:  # ADBError / ET.ParseError / OSError 等
            last = exc
        time.sleep(3)
    raise RuntimeError(f"uiautomator dump 连续失败: {last}")


def tap(x: int, y: int) -> None:
    ensure_ready()
    get_phone().tap(int(x), int(y))


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    ensure_ready()
    get_phone().swipe(int(x1), int(y1), int(x2), int(y2), int(duration_ms))


def press_key(key: str | int) -> None:
    get_phone().press_key(key)


def input_text(text: str) -> None:
    ensure_ready()
    get_phone().input_text(text)


def launch_app(package: str) -> None:
    ensure_ready()
    get_phone().launch_app(package)


def current_app() -> str:
    return get_phone().current_app()


def screen_size() -> tuple[int, int]:
    return get_phone().screen_size()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "shot":
        out = Path(args[0])
        screenshot(out)
        print(out)
    elif cmd == "ui":
        out = Path(args[0])
        out.write_text(ui_xml(), encoding="utf-8")
        print(out)
    elif cmd == "tap":
        tap(int(args[0]), int(args[1]))
    elif cmd == "text":
        input_text(args[0])
    elif cmd == "key":
        press_key(args[0])
    elif cmd == "launch":
        launch_app(args[0])
    elif cmd == "foreground":
        print(current_app())
    else:
        print(f"未知子命令: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
