#!/usr/bin/env python3
"""同花顺模拟炒股 页面抓取与操作库(host 侧, 经 adb)。

设计约束:
- 只操作同花顺 App 内明确标注「模拟炒股/模拟练习区」的页面;
  一旦发现界面出现真实券商交易登录或缺少模拟标识, 立即中止(fail-closed)。
- 所有抓取产物落到 data/phone_sim_capture/<UTC时间戳>/ 下:
  每页一份 UI XML + 截图 PNG, 并追加汇总 JSONL(captures.jsonl)。
- 坐标基于 1220x2712 物理像素; 优先用 UI 文本定位, 坐标仅作兜底。
- adb 传输层已切换 phone_ctl(2026-08-06): 底层统一委托同目录 phone_client.py,
  设备钉定(FOUNDF_ADB_SERIAL)与 uiautomator 重试语义由 phone_client 保留。
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import phone_client

# 模拟环境标识: 页面必须出现其中之一, 否则拒绝操作(防误入真实交易)
SIM_MARKERS = ("模拟炒股", "模拟练习区", "模拟交易", "(模拟炒股)")

# 营销弹窗(如「8月值得投」): WebView 浮层, 无障碍树不可见、BACK 无效,
# 特征为居中大红色卡片; 截图中心红通道均值检测(暗色主题下分离显著:
# 弹窗 ~238 vs 干净页 ~72)。X 关闭按钮在卡片正下方居中。
POPUP_CLOSE_XY = (610, 2042)
POPUP_RED_THRESHOLD = 150.0


def marketing_popup_present(png: Path) -> bool:
    """截图中心区域红通道均值超阈值 → 判营销弹窗(大红色卡片)在场。"""

    out = subprocess.run(
        ["convert", str(png), "-crop", "500x1100+360+700", "+repage",
         "-channel", "R", "-separate", "-format", "%[fx:mean*255]", "info:"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        return float(out) > POPUP_RED_THRESHOLD
    except ValueError:
        return False  # 检测失败按无弹窗处理(不盲点关闭键)


def dismiss_marketing_popup(workdir: Path) -> bool:
    """检测并关闭营销弹窗; 返回是否成功关闭(原本无弹窗也算 True)。

    冷启动同花顺几乎必弹, 浮层会吞掉一切点按(2026-08-10 实证:
    scrape 20 分钟零进展、BACK 无效), 故 goto_sim_trade 每次落地
    模拟主页后必过此关。检测失败的保守取向是不动作——点按坐标
    在干净页面上可能命中「诊·持仓」等真实入口。
    """

    png = Path(workdir) / "_popup_probe.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    screenshot(png)
    if not marketing_popup_present(png):
        return True
    tap(*POPUP_CLOSE_XY)
    time.sleep(2)
    screenshot(png)
    return not marketing_popup_present(png)


# 屏幕真值探针: 模拟主页标题「模拟练习区」的截图裁剪区(1220x2712 原图)。
# 2026-08-10 实证: 进程被异常 kill 后 uiautomator 无障碍缓存会保留最后
# 一页, dump 显示模拟页而真实屏幕在首页, 按 dump 坐标点按会命中首页元素
# (含真实券商开户入口)。故 dump 判定之外必须以截图 OCR 复核真实屏幕。
SIM_HOME_TITLE_CROP = "400x90+240+300"


def screen_shows_sim_home(png: Path) -> bool:
    """OCR 截图标题区, 校验真实屏幕在模拟主页(含「练习区」字样)。

    检测失败(OCR 不可用/出错)保守返回 False → 调用方冷重启重试,
    最终 fail-closed。psm 6 对该区域白字黑底最稳; chi_sim 会把「模」
    误识为「蛋」, 故只匹配「练习区」。
    """

    crop = png.with_name(png.stem + "_title.png")
    try:
        subprocess.run(
            ["convert", str(png), "-crop", SIM_HOME_TITLE_CROP, "+repage",
             "-resize", "400%", "-colorspace", "Gray", "-level", "20%,80%",
             str(crop)],
            capture_output=True, check=True,
        )
        out = subprocess.run(
            ["tesseract", str(crop), "stdout", "-l", "chi_sim", "--psm", "6"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return False
    return "练习区" in re.sub(r"\s+", "", out)


def sh(args: list[str], timeout: int = 30) -> str:
    """执行 adb shell 命令(传输经 phone_client/phone_ctl); 仅保留 shell
    子命令以兼容下游旧调用, 文件传输请直接用 phone_client。"""

    if not args or args[0] != "shell":
        raise ValueError("sh 仅支持 shell 子命令; 其余传输请用 phone_client")
    return phone_client.shell(
        " ".join(shlex.quote(a) for a in args[1:]), timeout=timeout
    )


def dump_ui(local_xml: Path, retries: int = 5) -> ET.Element:
    """抓取 UI 树; 重试语义在 phone_client.ui_xml(最多 retries 次、间隔 3s,
    抗 uiautomator 被系统 SIGKILL exit 137 / 空文件 / 截断 XML)。"""

    xml = phone_client.ui_xml(retries=retries)
    local_xml.write_text(xml, encoding="utf-8")
    return ET.fromstring(xml)


def screenshot(local_png: Path) -> None:
    phone_client.screenshot(local_png)


def tap(x: int, y: int) -> None:
    phone_client.tap(x, y)


def back() -> None:
    phone_client.press_key(4)


def launch_app(cold: bool = False) -> None:
    if cold:
        phone_client.shell("am force-stop com.hexin.plat.android")
        time.sleep(2)
    phone_client.launch_app("com.hexin.plat.android")
    time.sleep(10 if cold else 6)


def nodes_with_text(root: ET.Element) -> list[tuple[str, str]]:
    out = []
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        if t:
            out.append((t, n.get("bounds") or ""))
    return out


def find_center(
    root: ET.Element, pattern: str, *, min_y: int = 0, last: bool = False
) -> tuple[int, int] | None:
    """按文本定位控件中心; min_y 过滤顶部同名标签, last 取最后一个匹配。"""

    rx = re.compile(f"^{re.escape(pattern)}$")
    matches: list[tuple[int, int]] = []
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        if rx.match(t):
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds") or "")
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                cy = (y1 + y2) // 2
                if cy >= min_y:
                    matches.append(((x1 + x2) // 2, cy))
    if not matches:
        return None
    return matches[-1] if last else matches[0]


def page_texts(root: ET.Element) -> list[str]:
    return [t for t, _ in nodes_with_text(root)]


def is_sim_page(root: ET.Element) -> bool:
    texts = page_texts(root)
    return any(any(m in t for m in SIM_MARKERS) for t in texts)


def tap_text(
    root: ET.Element | None, text: str, *, min_y: int = 0, last: bool = False,
    fallback: tuple[int, int] | None = None,
) -> bool:
    """按文本点按; root 为 None(页面无障碍树抓不下来)时用固定坐标兜底。"""

    c = find_center(root, text, min_y=min_y, last=last) if root is not None else None
    if c is None:
        c = fallback
    if c is None:
        return False
    tap(*c)
    return True


def dismiss_popups(root: ET.Element) -> bool:
    """关掉「系统信息」类错误弹窗(如网络抖动导致的 Begin failed!)。"""

    texts = page_texts(root)
    if "系统信息" in texts and "确定" in texts:
        return tap_text(root, "确定")
    return False


def sim_home_on_screen(tmp: Path) -> bool:
    """营销弹窗关闸 + 截图 OCR 复核真实屏幕在模拟主页。

    dump 判定(is_sim_page)之外的真值防线: uiautomator 缓存树可能与屏幕
    分叉(2026-08-10 实证), 分叉时返回 False 由 goto_sim_trade 冷重启重试。
    """

    if not dismiss_marketing_popup(tmp):
        return False
    png = tmp / "_screen_probe.png"
    screenshot(png)
    return screen_shows_sim_home(png)


def goto_sim_trade(tmp: Path, retries: int = 5) -> ET.Element:
    """进入模拟炒股主页(买入/卖出/撤单/持仓/查询标签页), 返回该页 UI 树。

    同花顺首页(信息流 WebView)与个别查询页的无障碍树会让 uiautomator 递归溢出,
    抓不下来时对「模拟炒股」入口用固定坐标(591,1031)盲点; 进入后的模拟交易
    页面是原生控件, 可正常抓取。每条成功路径都要过 sim_home_on_screen
    截图复核——进程异常终止后 uiautomator 缓存树可能与真实屏幕分叉。
    """

    for attempt in range(retries):
        launch_app(cold=attempt > 0)
        try:
            root = dump_ui(tmp / "probe.xml")
        except RuntimeError:
            # 首页病理树: 盲点「模拟炒股」图标(首页第三枚, 位置稳定)
            tap(591, 1031)
            time.sleep(5)
            try:
                root = dump_ui(tmp / "probe.xml")
            except RuntimeError:
                continue
        if dismiss_popups(root):
            time.sleep(2)
            root = dump_ui(tmp / "probe.xml")
        if is_sim_page(root) and find_center(root, "买入"):
            # 需要账户主页(有「总资产」); 若停在委托表单页, 按返回回到主页
            if "总资产" not in page_texts(root):
                back()
                time.sleep(3)
                try:
                    r2 = dump_ui(tmp / "probe.xml")
                    if "总资产" in page_texts(r2) and find_center(r2, "买入"):
                        if sim_home_on_screen(tmp):
                            return r2
                except RuntimeError:
                    pass
            if sim_home_on_screen(tmp):
                return root
        # 首页找「模拟炒股」入口
        if tap_text(root, "模拟炒股"):
            time.sleep(5)
            root = dump_ui(tmp / "probe.xml")
            if dismiss_popups(root):
                time.sleep(2)
                root = dump_ui(tmp / "probe.xml")
            if is_sim_page(root) and find_center(root, "买入"):
                if sim_home_on_screen(tmp):
                    return root
                # 屏幕复核未过(dump/屏幕分叉): 不落 return, 走下轮冷重启
        # 兜底: 底部交易 tab (固定坐标), 再找模拟入口
        tap(710, 2632)
        time.sleep(4)
        try:
            root = dump_ui(tmp / "probe.xml")
        except RuntimeError:
            continue
        if is_sim_page(root) and find_center(root, "买入"):
            if sim_home_on_screen(tmp):
                return root
        if tap_text(root, "模拟"):
            time.sleep(4)
            root = dump_ui(tmp / "probe.xml")
            if is_sim_page(root) and find_center(root, "买入"):
                if sim_home_on_screen(tmp):
                    return root
                # 屏幕复核未过(dump/屏幕分叉): 不落 return, 走下轮冷重启
        back()
    raise RuntimeError("无法进入模拟炒股主页, 或页面缺少模拟标识, 已中止")


def _capture_unlocked(out_root: Path) -> Path:
    """抓取模拟账户关键页面, 返回本次抓取目录。"""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / stamp
    out.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []

    root = goto_sim_trade(out)

    # 模拟资金账号掩码在不同页面的星号数量可能不一致，
    # 因此只取掩码后的数字部分做本次会话内的一致性校验。
    account_digits = None
    for t in page_texts(root):
        m = re.search(r"\*{2,}(\d{3,})", t)
        if m:
            account_digits = m.group(1)
            break

    def page_is_sim(r: ET.Element) -> bool:
        if is_sim_page(r):
            return True
        return bool(account_digits) and any(
            re.search(rf"\*{{2,}}{account_digits}", t) for t in page_texts(r)
        )

    def snap(name: str, r: ET.Element) -> None:
        xml_path = out / f"{name}.xml"
        # dump_ui 已写入 probe.xml; 重新正式抓一份
        try:
            r2 = dump_ui(xml_path)
        except RuntimeError:
            # 病理无障碍树(WebView 递归溢出): 降级为仅截图留档
            screenshot(out / f"{name}.png")
            summary.append({"page": name, "texts": [], "dump_failed": True})
            return
        if not page_is_sim(r2):
            # 可能是资讯弹窗遮挡: 按返回键关闭后重抓一次再判定
            back()
            time.sleep(2)
            try:
                r2 = dump_ui(xml_path)
            except RuntimeError:
                screenshot(out / f"{name}.png")
                summary.append({"page": name, "texts": [], "dump_failed": True})
                return
        screenshot(out / f"{name}.png")
        texts = page_texts(r2)
        summary.append({"page": name, "texts": texts})
        if not page_is_sim(r2):
            raise RuntimeError(f"页面 {name} 缺少模拟标识, 已中止")

    snap("sim_home", root)

    def try_dump() -> ET.Element | None:
        try:
            return dump_ui(out / "nav.xml")
        except RuntimeError:
            return None

    # 买入页内嵌的 持仓/委托/成交 标签(顶部有同名导航标签, min_y 避开)
    if tap_text(try_dump(), "买入", fallback=(122, 327)):
        time.sleep(3)
        for tab, fb in (("持仓", (152, 1406)), ("委托", (457, 1406)),
                        ("成交", (762, 1406))):
            r = try_dump()
            if tap_text(r, tab, min_y=1000, fallback=fb):
                time.sleep(3)
                snap(f"order_{tab}", r)
                if tab == "持仓":
                    # 持仓超 5 行时同花顺列表虚拟化裁行(2026-08-12 建行
                    # 事故根因): 上滑加载下方行后抓第二屏, 解析端按名称合并
                    phone_client.swipe(600, 2100, 600, 1400, 400)
                    time.sleep(3)
                    snap("order_持仓_b", try_dump() or r)
        back()
        time.sleep(2)

    # 查询页: 当日/历史 委托/成交(标签坐标固定, 页面可能是病理 WebView)
    r = try_dump()
    if tap_text(r, "查询", fallback=(1098, 1087)):
        time.sleep(3)
        for tab, fb in (("当日委托", (152, 321)), ("当日成交", (457, 321)),
                        ("历史委托", (762, 321)), ("历史成交", (1067, 321))):
            r = try_dump()
            if tap_text(r, tab, fallback=fb):
                time.sleep(3)
                snap(f"query_{tab}", r)
        back()
        time.sleep(2)

    (out / "captures.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in summary) + "\n",
        encoding="utf-8",
    )
    print(out)
    return out


def capture(out_root: Path) -> Path:
    """独占手机 UI 完成整次抓取，锁屏会在进入任务时安全处理。"""

    with phone_client.device_lock():
        return _capture_unlocked(out_root)


if __name__ == "__main__":
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/phone_sim_capture")
    capture(out_root)
