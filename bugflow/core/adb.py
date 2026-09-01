"""ADB 设备操作层 — subprocess adb 封装。

抄自 YWAgent tools/adb_tools.py + build_tools.py 的 root/remount/push 序列。
自包含，不依赖 YWAgent。

能力：
  - adb_run / adb_shell / adb_logcat / adb_devices
  - resolve_device（多设备自动选择/报错）
  - wait_for_device（重启后等待上线）
  - root + remount + push（verify 步骤刷补丁用）
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger("bugflow.core.adb")


# ── 异常 ──────────────────────────────────────────────────────
class AdbError(Exception):
    """ADB 操作失败（超时/未找到/非零退出）。"""


# ── 底层执行 ───────────────────────────────────────────────────
def adb_run(args: list[str], serial: str = "", timeout: int = 30) -> tuple[str, str, int]:
    """跑一条 adb 命令，返回 (stdout, stderr, exit_code)。

    非零退出抛 AdbError（带可读建议）。FileNotFoundError→adb 不在 PATH。
    """
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )
        if result.returncode != 0:
            err_msg = result.stderr.strip() or result.stdout.strip()
            low = err_msg.lower()
            if "device" in low and "not found" in low:
                err_msg += "\n建议: adb devices 检查连接，或 adb reconnect。"
            elif "unauthorized" in low:
                err_msg += "\n建议: 在设备上同意 USB 调试授权弹窗。"
            raise AdbError(f"ADB 命令失败: {err_msg}")
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        raise AdbError(f"ADB 命令超时 ({timeout}s)。建议: 重试或检查设备是否响应。") from None
    except FileNotFoundError:
        raise AdbError(
            "adb 未找到 — 请确保 Android SDK platform-tools 在 PATH 中。"
            "检查 ANDROID_HOME 环境变量。"
        ) from None


def adb_shell(command: str, serial: str = "", timeout: int = 15, root: bool = False) -> tuple[str, str, int]:
    """adb shell <command>，返回 (out, err, ec)。非零不抛（调用方看 ec）。

    root=True 时前置 su 0 执行（需 userdebug 版本），避免手动拼 su 引号嵌套。
    """
    to = max(5, min(int(timeout or 15), 120))
    if root:
        # su 0 sh -c '...' 需要把 command 包在单引号里，内部单引号转义
        escaped_cmd = command.replace("'", "'\\''")
        shell_cmd = f"su 0 sh -c '{escaped_cmd}'"
    else:
        shell_cmd = command
    try:
        result = subprocess.run(
            ["adb"] + (["-s", serial] if serial else []) + ["shell", shell_cmd],
            capture_output=True, text=True, timeout=to, errors="replace",
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"adb shell 超时 ({to}s)", 124
    except FileNotFoundError:
        return "", "adb 未找到 — 检查 platform-tools 在 PATH", 127


# ── 设备解析 ───────────────────────────────────────────────────
def list_device_serials() -> list[str]:
    """返回所有 status=device 的 serial（不含 offline/unauthorized）。"""
    try:
        out, _, _ = adb_run(["devices"])
    except AdbError:
        return []
    serials = []
    for line in out.split("\n")[1:]:  # 跳过 "List of devices attached"
        if "\tdevice" in line:
            serials.append(line.split("\t")[0])
    return serials


def adb_devices() -> str:
    """人类可读设备列表。"""
    try:
        out, _, _ = adb_run(["devices"])
    except AdbError as e:
        return f"❌ {e}"
    lines = [l for l in out.split("\n")[1:] if l.strip()]
    if not lines:
        return "📱 没有连接的设备。请 USB 连接并开启 USB 调试。"
    out_lines = [f"📱 已连接 {len(lines)} 台:"]
    for i, d in enumerate(lines, 1):
        parts = d.split("\t")
        serial = parts[0]
        status = parts[1] if len(parts) > 1 else "unknown"
        icon = {"device": "✅", "unauthorized": "🔒", "offline": "⛔"}.get(status, "❓")
        out_lines.append(f"  [{i}] {serial}  {icon} {status}")
    return "\n".join(out_lines)


def resolve_device(serial: str = "") -> str:
    """解析目标 serial。

    - 指定 → 原样返回
    - 未指定 + 1 台 → 自动选
    - 未指定 + 0 台 → AdbError
    - 未指定 + 2+ 台 → AdbError 列出可选
    """
    if serial:
        return serial
    devices = list_device_serials()
    if len(devices) == 1:
        return devices[0]
    if len(devices) == 0:
        raise AdbError("没有连接的设备。请 USB 连接并开启 USB 调试。")
    raise AdbError(
        f"检测到 {len(devices)} 台设备，必须指定 serial:\n"
        + "\n".join(f"  • {s}" for s in devices)
    )


def wait_for_device(serial: str, timeout: int = 120) -> tuple[bool, float]:
    """等设备重启后重新上线（adb wait-for-device 主动等，非盲 sleep）。

    返回 (是否成功, 实际耗时秒)。
    """
    start = time.time()
    try:
        adb_run(["wait-for-device"], serial=serial, timeout=timeout)
        elapsed = time.time() - start
        logger.info("设备 %s 在 %.1fs 后重新连接", serial, elapsed)
        return True, elapsed
    except AdbError as e:
        elapsed = time.time() - start
        logger.warning("等待设备 %s 超时 (%.1fs): %s", serial, elapsed, e)
        return False, elapsed


def is_device_online(serial: str = "", timeout: int = 5) -> bool:
    """快速检测 ADB 是否在线（不阻塞），用于命令序列中检测断连。"""
    try:
        adb_run(["get-state"], serial=serial, timeout=timeout)
        return True
    except AdbError:
        return False


def wait_for_device_reconnect(serial: str = "", timeout: int = 60) -> tuple[bool, str]:
    """ADB 断连后等待恢复，返回 (是否恢复, 提示信息)。

    设备可能因 reboot/crash/suspend 断连。
    尝试 wait-for-device，超时则返回串口恢复提示。
    """
    online, elapsed = wait_for_device(serial, timeout=timeout)
    if online:
        return True, f"设备在 {elapsed:.1f}s 后重新连接"
    return False, (
        "设备未恢复。可能需要: "
        "serial_open(port) → serial_send('reboot') → wait-for-device"
    )


# ── Logcat ────────────────────────────────────────────────────
def adb_logcat(
    filter: str = "",
    lines: int = 200,
    serial: str = "",
    timeout: int = 20,
) -> str:
    """抓 logcat -d -t <lines>，Python 端按 | 分隔关键字过滤。

    不用 adb logcat -e（正则兼容性差），拉原始日志后 Python 过滤。
    """
    serial = resolve_device(serial)
    out, _, _ = adb_run(["logcat", "-d", "-t", str(lines)], serial=serial, timeout=timeout)
    result = out.strip()
    if not result:
        return "(无日志输出)"

    if filter:
        keywords = [kw.strip() for kw in filter.split("|") if kw.strip()]
        if keywords:
            filtered = [
                line for line in result.split("\n")
                if any(kw.lower() in line.lower() for kw in keywords)
            ]
            if not filtered:
                return f"(无匹配日志, filter={filter})"
            result = "\n".join(filtered)
    return result


def clear_logcat(serial: str = "") -> None:
    """清 logcat 缓冲（复现前调，确保抓到的是新日志）。"""
    serial = resolve_device(serial)
    try:
        adb_run(["logcat", "-c"], serial=serial, timeout=10)
    except AdbError:
        pass  # best-effort


# ── UI 自动化（uiautomator dump + input 命令）──────────────────
import re as _re
import xml.etree.ElementTree as _ET
import tempfile as _tempfile
import time as _time

_UI_DUMP_PATH = "/sdcard/bugfix_ui.xml"

# 常用设置页 intent 映射
_UI_INTENT_MAP = {
    "wifi": "android.settings.WIFI_SETTINGS",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS",
    "battery": "android.settings.BATTERY_SAVER_SETTINGS",
    "apps": "android.settings.APPLICATION_SETTINGS",
    "developer": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
    "home": "android.settings.HOME_SETTINGS",
    "language": "android.settings.LOCALE_SETTINGS",
    "about": "android.settings.DEVICE_INFO_SETTINGS",
    "date": "android.settings.DATE_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS",
    "security": "android.settings.SECURITY_SETTINGS",
    "input_method": "android.settings.INPUT_METHOD_SETTINGS",
}

# 系统按键映射
_UI_KEY_MAP = {
    "back": "4", "home": "3", "power": "26", "enter": "66",
    "menu": "82", "recent": "187", "vol_up": "24", "vol_down": "25",
    "delete": "67",
}


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """解析 bounds="[x1,y1][x2,y2]" → (x1, y1, x2, y2)。"""
    m = _re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def ui_snapshot(serial: str = "", filter: str = "") -> str:
    """uiautomator dump → pull → 解析 XML 控件树。

    返回可交互控件的文本、类型、坐标中心点。filter 筛选含此文本的控件。
    可滚动容器标记 📜 并显示完整边界 [x1,y1-x2,y2]，供 ui_scroll 使用。
    dump 失败时自动重试（页面过渡/动画偶发失败）。
    """
    serial = resolve_device(serial)
    local_dump = os.path.join(_tempfile.gettempdir(), "bugfix_ui.xml")
    last_err = ""
    for attempt in range(3):
        try:
            adb_run(["shell", "uiautomator", "dump", _UI_DUMP_PATH], serial=serial, timeout=15)
            adb_run(["pull", _UI_DUMP_PATH, local_dump], serial=serial, timeout=10)
            break
        except AdbError as e:
            last_err = str(e)
            if attempt < 2:
                _time.sleep(1)
            else:
                return f"❌ uiautomator dump 失败（重试 3 次）: {last_err}"

    try:
        with open(local_dump, "r", encoding="utf-8", errors="replace") as f:
            xml_str = f.read()
        if xml_str.startswith("\ufeff"):
            xml_str = xml_str[1:]
        root = _ET.fromstring(xml_str)
    except Exception as e:
        return f"❌ 解析 UI XML 失败: {e}"

    filter_lower = filter.lower() if filter else ""
    lines = []
    scrollable_count = 0
    for node in root.iter("node"):
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        cls = (node.attrib.get("class") or "").split(".")[-1]
        bounds = node.attrib.get("bounds", "")
        clickable = node.attrib.get("clickable") == "true"
        scrollable = node.attrib.get("scrollable") == "true"
        checked = node.attrib.get("checked") == "true"
        enabled = node.attrib.get("enabled") != "false"

        label = text or desc
        if not label and not clickable and not scrollable:
            continue
        if filter_lower and filter_lower not in label.lower():
            continue

        parsed = _parse_bounds(bounds)
        cx, cy = "", ""
        if parsed:
            cx = str((parsed[0] + parsed[2]) // 2)
            cy = str((parsed[1] + parsed[3]) // 2)

        flags = []
        if clickable:
            flags.append("可点击")
        if scrollable:
            flags.append("📜可滚动")
            scrollable_count += 1
        if checked:
            flags.append("✓")
        if not enabled:
            flags.append("禁用")

        flag_str = f" [{', '.join(flags)}]" if flags else ""
        # 可滚动容器显示完整边界，方便 ui_scroll 定位
        if scrollable and parsed:
            coord_str = f" ({cx},{cy}) [{parsed[0]},{parsed[1]}-{parsed[2]},{parsed[3]}]"
        else:
            coord_str = f" ({cx},{cy})" if cx else ""
        lines.append(f"  {label or cls}{flag_str}{coord_str}")

    if not lines:
        return "界面为空或无可交互控件。"
    header = f"界面控件 ({len(lines)} 个)"
    if scrollable_count:
        header += f"，含 {scrollable_count} 个可滚动容器"
    truncated = len(lines) > 80
    if truncated:
        header += f"（共 {len(lines)} 个，显示前 80 个）"
    return header + ":\n" + "\n".join(lines[:80])


def ui_tap(x: int, y: int, serial: str = "") -> str:
    """点击屏幕坐标。配合 ui_snapshot 从快照获取坐标后点击。"""
    serial = resolve_device(serial)
    try:
        adb_shell(f"input tap {x} {y}", serial=serial, timeout=5)
        return f"✅ 点击 ({x},{y})"
    except AdbError as e:
        return f"❌ 点击失败: {e}"


def _ui_dump_and_parse(serial: str, tag: str = "ui") -> _ET.Element | None:
    """uiautomator dump + pull + parse，返回 XML root 或 None。

    dump 失败时自动重试 2 次（间隔 1s），处理页面过渡/动画导致的偶发失败。
    """
    local_dump = os.path.join(_tempfile.gettempdir(), f"bugfix_{tag}.xml")
    for attempt in range(3):
        try:
            adb_run(["shell", "uiautomator", "dump", _UI_DUMP_PATH], serial=serial, timeout=15)
            adb_run(["pull", _UI_DUMP_PATH, local_dump], serial=serial, timeout=10)
            break
        except AdbError:
            if attempt < 2:
                _time.sleep(1)
            else:
                return None
    try:
        with open(local_dump, "r", encoding="utf-8", errors="replace") as f:
            xml_str = f.read()
        if xml_str.startswith("\ufeff"):
            xml_str = xml_str[1:]
        return _ET.fromstring(xml_str)
    except Exception:
        return None


def ui_tap_text(text: str, exact: bool = False, index: int = 0, serial: str = "") -> str:
    """在 UI 中查找含指定文本的控件并点击其文字坐标中心。

    比 ui_tap(x,y) 精确——自动定位文字标签所在坐标，避免点到列表容器空白处。
    exact=True 时要求完全匹配，False 时包含匹配。
    index: 多个匹配时点第几个（0=第一个，1=第二个...），默认 0。
    """
    serial = resolve_device(serial)
    root = _ui_dump_and_parse(serial, tag="taptext")
    if root is None:
        return "❌ uiautomator dump 失败（重试 3 次）"

    text_lower = text.lower()
    candidates = []
    for node in root.iter("node"):
        node_text = (node.attrib.get("text") or "").strip()
        node_desc = (node.attrib.get("content-desc") or "").strip()
        label = node_text or node_desc
        if not label:
            continue
        match = (label.lower() == text_lower) if exact else (text_lower in label.lower())
        if not match:
            continue
        parsed = _parse_bounds(node.attrib.get("bounds", ""))
        if parsed:
            cx = (parsed[0] + parsed[2]) // 2
            cy = (parsed[1] + parsed[3]) // 2
            candidates.append((label, cx, cy))

    if not candidates:
        return f"❌ 未找到含 '{text}' 的控件"

    if index >= len(candidates):
        return f"❌ 只有 {len(candidates)} 个匹配，index={index} 超出范围（0~{len(candidates)-1}）"

    label, cx, cy = candidates[index]
    try:
        adb_shell(f"input tap {cx} {cy}", serial=serial, timeout=5)
        if len(candidates) > 1:
            # 多匹配时列出所有候选，方便用 index 选择正确的
            listing = "\n".join(
                f"  [{i}] '{lbl}' ({x},{y})"
                for i, (lbl, x, y) in enumerate(candidates)
            )
            return (
                f"✅ 点击 '{label}' ({cx},{cy})\n"
                f"共 {len(candidates)} 个匹配，点了 index={index}:\n{listing}\n"
                f"如需点击其他，用 index=0~{len(candidates)-1}"
            )
        return f"✅ 点击 '{label}' ({cx},{cy})"
    except AdbError as e:
        return f"❌ 点击失败: {e}"


def ui_swipe(
    x1: int, y1: int, x2: int, y2: int,
    duration: int = 300, serial: str = "",
) -> str:
    """从 (x1,y1) 滑动到 (x2,y2)。duration 毫秒，默认 300。"""
    serial = resolve_device(serial)
    try:
        adb_shell(
            f"input swipe {x1} {y1} {x2} {y2} {duration}",
            serial=serial, timeout=5,
        )
        return f"✅ 滑动 ({x1},{y1}) → ({x2},{y2})"
    except AdbError as e:
        return f"❌ 滑动失败: {e}"


def ui_scroll(
    direction: str = "down", filter: str = "",
    return_snapshot: bool = False, serial: str = "",
) -> str:
    """在可滚动容器内滚动。自动查找 scrollable 控件并在其边界内滑动。

    direction: 'down'(向下翻页/看下方内容) 或 'up'(向上翻页)。
    filter: 可选，筛选包含此文本的滚动容器（多个可滚动区域时定位特定对话框）。
    return_snapshot: True 时滚动后自动 dump 一次，返回新出现的控件（省一轮交互，+约 2s）。
    比手动 ui_swipe 更精准——只在容器边界内滑动，不会拖动整个页面。

    容器选择策略（策略 B — z-order 最高优先）：
    1. 过滤掉 Spinner/NumberPicker（下拉选择器不是滚动列表）
    2. 有 filter 时按文本筛选
    3. 取 XML 中最后出现的容器（后出现=绘制在最前=对话框优先于页面）
    Android UI 中对话框总是在页面之后绘制，XML 顺序反映绘制顺序。
    滑动用容器 25%~75% 高度区间，避开边缘和手势导航区。
    """
    serial = resolve_device(serial)
    root = _ui_dump_and_parse(serial, tag="scroll")
    if root is None:
        return "❌ uiautomator dump 失败"

    filter_lower = filter.lower() if filter else ""
    # 显式跳过的容器类型（下拉选择器，非滚动列表）
    _SKIP_CLASSES = {"Spinner", "NumberPicker"}

    scrollables = []
    for node in root.iter("node"):
        if node.attrib.get("scrollable") != "true":
            continue
        parsed = _parse_bounds(node.attrib.get("bounds", ""))
        if not parsed:
            continue
        x1, y1, x2, y2 = parsed
        cls = (node.attrib.get("class") or "").split(".")[-1]

        # 问题 3 修复：显式跳过 Spinner/NumberPicker
        if cls in _SKIP_CLASSES:
            continue

        # 问题 2 修复：abs() 防止反转 bounds 导致负面积
        area = abs(x2 - x1) * abs(y2 - y1)

        if filter_lower:
            has_text = False
            for child in node.iter("node"):
                child_text = (child.attrib.get("text") or "").strip().lower()
                child_desc = (child.attrib.get("content-desc") or "").strip().lower()
                if filter_lower in child_text or filter_lower in child_desc:
                    has_text = True
                    break
            if not has_text:
                continue

        # 按 XML 出现顺序 append，后出现的在列表末尾（z-order 最高）
        scrollables.append((area, x1, y1, x2, y2, cls))

    if not scrollables:
        hint = f"（含 '{filter}'）" if filter else ""
        return f"❌ 未找到可滚动容器{hint}"

    # 问题 1 修复：取最后一个（z-order 最高=最前面的对话框），不按面积
    chosen = scrollables[-1]

    if len(scrollables) > 1:
        container_list = ", ".join(
            f"{s[5]}[{s[1]},{s[2]}-{s[3]},{s[4]}]" for s in scrollables
        )
        chosen_note = (
            f"（共 {len(scrollables)} 个可滚动容器: {container_list}，"
            f"选最前层的 {chosen[5]}）"
        )
    else:
        chosen_note = ""

    _, x1, y1, x2, y2, cls = chosen
    cx = (x1 + x2) // 2
    height = y2 - y1

    # 用容器 25%~75% 高度滑动，避开边缘和手势导航区
    top_q = y1 + int(height * 0.25)
    bot_q = y1 + int(height * 0.75)

    if direction.lower() == "down":
        sy, ey = bot_q, top_q  # 从 75% 滑到 25%（手指上滑，内容下移）
    else:
        sy, ey = top_q, bot_q  # 从 25% 滑到 75%（手指下滑，内容上移）

    try:
        adb_shell(f"input swipe {cx} {sy} {cx} {ey} 400", serial=serial, timeout=5)
        result = (
            f"✅ 滚动{direction} {cls}[{x1},{y1}-{x2},{y2}] "
            f"({cx},{sy})→({cx},{ey}){chosen_note}"
        )
        if return_snapshot:
            _time.sleep(0.5)
            snapshot = ui_snapshot(serial=serial)
            result += f"\n\n--- 滚动后界面 ---\n{snapshot}"
        return result
    except AdbError as e:
        return f"❌ 滚动失败: {e}"


def ui_type(text: str, clear_first: bool = False, serial: str = "") -> str:
    """输入文本（需先点击输入框获取焦点）。中文用 broadcast 回退。

    clear_first=True 时先清空已有内容（MOVE_END + 50×DEL），再输入新文本。
    用于输入框已有内容需要覆盖的场景，如密码重输。
    """
    serial = resolve_device(serial)

    if clear_first:
        try:
            # KEYCODE_MOVE_END=123 移到末尾, KEYCODE_DEL=67 退格
            # input keyevent 支持一次传多个 keycode
            dels = " ".join(["67"] * 50)
            adb_shell(f"input keyevent 123 {dels}", serial=serial, timeout=10)
        except AdbError:
            pass  # best-effort clear

    try:
        escaped = text.replace(" ", "%s").replace("'", "\\'")
        adb_shell(f"input text {escaped}", serial=serial, timeout=5)
        note = "（已清空旧内容）" if clear_first else ""
        return f"✅ 已输入{note}: {text}"
    except AdbError:
        try:
            adb_shell(
                f"am broadcast -a ADB_INPUT_TEXT --es msg {text}",
                serial=serial, timeout=5,
            )
            note = "（已清空旧内容）" if clear_first else ""
            return (
                f"✅ 已输入（中文广播）{note}: {text}\n"
                f"⚠️ 走了 broadcast 回退路径，需设备安装 ADBKeyBoard IME 才生效"
            )
        except AdbError as e:
            return f"❌ 输入失败: {e}"


def ui_key(key: str, serial: str = "") -> str:
    """按下系统按键。支持: back|home|power|enter|menu|recent|vol_up|vol_down|delete。"""
    code = _UI_KEY_MAP.get(key.lower(), "")
    if not code:
        return f"❌ 未知按键: {key}。支持: {', '.join(_UI_KEY_MAP.keys())}"
    serial = resolve_device(serial)
    try:
        adb_shell(f"input keyevent {code}", serial=serial, timeout=5)
        return f"✅ 按下 {key}"
    except AdbError as e:
        return f"❌ 按键失败: {e}"


def ui_start_app(target: str, serial: str = "") -> str:
    """启动应用或打开设置页。target: wifi|bluetooth|display|... 或完整包名。

    优先用 am start -a <intent>，回退用 monkey -p <package>。
    """
    serial = resolve_device(serial)
    intent = _UI_INTENT_MAP.get(target.lower().strip(), target)

    try:
        if "/" in intent:
            adb_shell(
                f"am start -a android.intent.action.MAIN -n {intent}",
                serial=serial, timeout=10,
            )
        else:
            adb_shell(f"am start -a {intent}", serial=serial, timeout=10)
        return f"✅ 已启动: {target}"
    except AdbError:
        try:
            adb_shell(
                f"monkey -p {intent} -c android.intent.category.LAUNCHER 1",
                serial=serial, timeout=10,
            )
            return f"✅ 已启动（monkey）: {target}"
        except AdbError as e:
            return f"❌ 启动失败: {e}"


def ui_wait_text(text: str, timeout: int = 10, interval: float = 1.0, serial: str = "") -> str:
    """轮询 UI 直到出现指定文本或超时。避免盲 sleep，改成条件等待。

    text: 要等待出现的控件文字（包含匹配，不区分大小写）。
    timeout: 最长等待秒数，默认 10。interval: 轮询间隔秒，默认 1.0。
    返回: 找到时返回控件信息，超时返回未找到提示。
    """
    serial = resolve_device(serial)
    text_lower = text.lower()
    deadline = _time.monotonic() + timeout

    while _time.monotonic() < deadline:
        root = _ui_dump_and_parse(serial, tag="wait")
        if root is not None:
            for node in root.iter("node"):
                node_text = (node.attrib.get("text") or "").strip()
                node_desc = (node.attrib.get("content-desc") or "").strip()
                label = node_text or node_desc
                if label and text_lower in label.lower():
                    parsed = _parse_bounds(node.attrib.get("bounds", ""))
                    coord = ""
                    if parsed:
                        coord = f" ({(parsed[0]+parsed[2])//2},{(parsed[1]+parsed[3])//2})"
                    return f"✅ 找到 '{label}'{coord}（等待了 {timeout - int(deadline - _time.monotonic())}s）"
        remaining = deadline - _time.monotonic()
        if remaining > 0:
            _time.sleep(min(interval, remaining))

    return f"❌ 等待 '{text}' 超时（{timeout}s）"


# ── root / remount / push（verify 步骤刷补丁用）──────────────
def adb_root(serial: str = "", retries: int = 3) -> None:
    """adb root（需 userdebug 版本）。失败重试（root 偶发需重启 adbd）。"""
    serial = resolve_device(serial)
    last_err = None
    for _ in range(retries):
        try:
            adb_run(["root"], serial=serial, timeout=15)
            return
        except AdbError as e:
            last_err = e
            time.sleep(2)
    raise AdbError(f"adb root 失败（{retries} 次）: {last_err}")


def adb_remount(serial: str = "", retries: int = 20) -> bool:
    """adb remount。部分设备需先 reboot 再 remount，最多重试 retries 次。

    返回 True 成功；失败抛 AdbError。
    """
    serial = resolve_device(serial)
    for i in range(retries):
        try:
            adb_run(["remount"], serial=serial, timeout=15)
            return True
        except AdbError:
            if i == retries - 1:
                break
            # remount 失败常因需 reboot；重启后等待再试
            try:
                adb_run(["reboot"], serial=serial, timeout=10)
            except AdbError:
                pass
            wait_for_device(serial, timeout=120)
            try:
                adb_root(serial=serial)
            except AdbError:
                pass
            time.sleep(3)
    raise AdbError(f"adb remount 失败（{retries} 次重试含 reboot）")


def adb_push(local_path: str, remote_path: str, serial: str = "", timeout: int = 120) -> str:
    """adb push <local> <remote>，返回 adb 输出。"""
    serial = resolve_device(serial)
    out, _, _ = adb_run(["push", local_path, remote_path], serial=serial, timeout=timeout)
    return out


def detect_erofs(serial: str = "", mount_point: str = "/vendor") -> bool:
    """检测指定挂载点是否为 erofs 只读文件系统。

    erofs 分区无法 adb remount 写入，需用 bind mount 降级。
    """
    serial = resolve_device(serial)
    try:
        out, _, ec = adb_shell(f"mount | grep ' {mount_point} '", serial=serial, timeout=10, root=True)
        return "erofs" in out.lower()
    except Exception:
        return False


def wait_for_boot_completed(serial: str = "", timeout: int = 90) -> bool:
    """轮询 sys.boot_completed=1，确保 PackageManager/system_server 就绪。

    wait_for_device 只等 ADB 可见（daemon 应答），不等系统启动完成。
    推送 system priv-app APK 后需等 boot_completed 才能跑 dexopt/am start。
    """
    serial = resolve_device(serial)
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, _, _ = adb_shell("getprop sys.boot_completed", serial=serial, timeout=5)
        if out.strip() == "1":
            return True
        time.sleep(2)
    return False


def _force_dexopt_for_apks(pushed_paths: list[str], serial: str = "") -> dict:
    """对推送的 .apk 文件强制 dexopt（speed 模式）。

    系统 priv-app APK 替换后旧 odex/vdex 缓存仍在，新代码不生效。
    通过 pm list packages -f 反查包名，再 cmd package compile -m speed -f 强制重编译。

    Returns:
        {pkg: 'ok'/'skip:no_package'/'fail:reason', ...}
        无 APK 时返回空 dict。
    """
    serial = resolve_device(serial)
    apk_paths = [p for p in pushed_paths if p.endswith(".apk")]
    if not apk_paths:
        return {}

    if not wait_for_boot_completed(serial=serial, timeout=90):
        return {"_error": "boot_completed 超时，跳过 dexopt"}

    # 一次性拉取所有包的 apk 路径映射
    out, _, _ = adb_shell("pm list packages -f", serial=serial, timeout=15)
    path_to_pkg: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        apk_part, pkg = line.rsplit("=", 1)
        pkg = pkg.strip()
        # apk_part 格式: package:/system/app/Foo/Foo.apk
        if apk_part.startswith("package:"):
            apk_part = apk_part[len("package:"):]
        path_to_pkg[apk_part] = pkg

    results: dict[str, str] = {}
    for apk_path in apk_paths:
        pkg = path_to_pkg.get(apk_path)
        if not pkg:
            results[apk_path] = "skip:no_package"
            continue
        _, err, ec = adb_shell(
            f"cmd package compile -m speed -f {pkg}",
            serial=serial, timeout=60, root=True,
        )
        results[pkg] = "ok" if ec == 0 else f"fail:{err.strip()[:80]}"
    return results


def push_patch_and_restart(
    patches: list[tuple[str, str]],
    serial: str = "",
    reboot: bool = True,
) -> dict:
    """推一组补丁文件到设备并重启（verify 步骤核心）。

    patches: [(local_path, remote_path), ...]
    流程: root → remount（必要时 reboot）→ push 每个 → 可选 reboot → wait-for-device
    返回 {pushed: [...], rebooted: bool, online: bool, elapsed_s: float}
    """
    serial = resolve_device(serial)
    adb_root(serial=serial)
    remount_rebooted = False
    try:
        adb_remount(serial=serial, retries=3)
    except AdbError:
        # remount 重试内部可能已 reboot；这里降级再试一次
        adb_remount(serial=serial, retries=20)
        remount_rebooted = True

    pushed = []
    for local, remote in patches:
        adb_push(local, remote, serial=serial)
        pushed.append(remote)

    did_reboot = remount_rebooted
    if reboot and not remount_rebooted:
        try:
            adb_run(["reboot"], serial=serial, timeout=10)
            did_reboot = True
        except AdbError:
            pass

    online, elapsed = (True, 0.0)
    if did_reboot:
        online, elapsed = wait_for_device(serial, timeout=180)
        if online:
            try:
                adb_root(serial=serial)
            except AdbError:
                pass

    # 推送 APK 后强制 dexopt — 系统 priv-app APK 替换后旧 odex 缓存不自动更新
    dexopt_result = {}
    if did_reboot and online:
        dexopt_result = _force_dexopt_for_apks(pushed, serial=serial)

    return {
        "pushed": pushed,
        "rebooted": did_reboot,
        "online": online,
        "elapsed_s": elapsed,
        "dexopt": dexopt_result,
    }


def get_main_activity(package: str, serial: str = "") -> str:
    """查询包的启动 Activity（android.intent.action.MAIN + LAUNCHER）。

    推送 APK 后 Activity 名可能与猜测不同（如 .ui.TelecomActivity vs .ui.activity.TelecomActivity），
    用此函数从 dumpsys package 解析正确名字，避免 am start 报 "Activity class does not exist"。

    Returns:
        "pkg/.MainActivity" 或 "pkg/com.foo.MainActivity" 格式；未找到返回 ❌ 错误。
    """
    import re as _re

    serial = resolve_device(serial)
    out, _, _ = adb_shell(
        f"dumpsys package {package}", serial=serial, timeout=10,
    )
    if not out.strip():
        return f"❌ 未找到包 {package}（可能未安装）"

    # dumpsys package 输出中 Activity 注册段格式：
    #   Activity Resolver Table:
    #     ...
    #     <activity_name>:
    #       Action: "android.intent.action.MAIN"
    #       Category: "android.intent.category.LAUNCHER"
    # 更可靠的方式：解析 "android.intent.action.MAIN" 附近的 activity 名
    lines = out.splitlines()
    main_action_idx = None
    for i, line in enumerate(lines):
        if "android.intent.action.MAIN" in line:
            main_action_idx = i
            break

    if main_action_idx is not None:
        # 往前找最近的 activity 名行（格式: "  xxx/yyy.ActivityName:"）
        for j in range(main_action_idx, max(main_action_idx - 20, -1), -1):
            line = lines[j].strip()
            # activity 行通常以包名/开头，以冒号结尾
            m = _re.match(r"^([\w.]+/[\w.$]+):", line)
            if m:
                return m.group(1)
            # 也可能是 "pkg/.ActivityName:" 格式
            m = _re.match(r"^([\w.]+/\.[\w.$]+):", line)
            if m:
                return m.group(1)

    # 备选：用 cmd package resolve-activity 直接查
    out2, err2, ec2 = adb_shell(
        f"cmd package resolve-activity --brief {package}",
        serial=serial, timeout=10,
    )
    if ec2 == 0:
        for line in out2.splitlines():
            line = line.strip()
            if "/" in line and not line.startswith("priority"):
                return line

    return f"❌ 未找到 {package} 的启动 Activity（无 MAIN+LAUNCHER intent filter）"


def check_device_clock(serial: str = "", threshold: int = 60) -> str:
    """检测设备时钟与本机偏差。

    设备时间偏差大时 logcat 时间戳混乱，影响证据判断。
    verify 开始时调用此函数，偏差超过阈值返回 ⚠️ 警告。

    Args:
        threshold: 允许的偏差秒数，默认 60s

    Returns:
        "" — 时钟正常
        "⚠️ ..." — 偏差超阈值
    """
    serial = resolve_device(serial)
    out, _, _ = adb_shell("date +%s", serial=serial, timeout=5)
    try:
        device_epoch = int(out.strip())
    except ValueError:
        return ""  # 无法解析，不报警
    delta = abs(device_epoch - int(time.time()))
    if delta > threshold:
        return f"⚠️ 设备时钟偏差 {delta}s（>{threshold}s），logcat 时间戳可能不准"
    return ""


# ── 电源/挂起（suspend 唤醒 Bug 测试）──────────────────────────
def trigger_suspend(serial: str = "", duration: int = 10) -> dict:
    """触发车载深度休眠并检测 ADB 连接状态。

    cmd car_service suspend --real 触发后 USB dwc3 控制器可能挂死，
    ADB 断连且无法自动恢复（需长按电源键或串口 reboot）。

    Args:
        serial: 设备序列号
        duration: 休眠持续秒数（传给 suspend 命令），默认 10

    Returns:
        {triggered: bool, adb_alive: bool, hint: str}
    """
    serial = resolve_device(serial)
    try:
        adb_shell(
            f"cmd car_service suspend --real {duration}",
            serial=serial, timeout=duration + 15, root=True,
        )
        triggered = True
    except AdbError as e:
        # 命令本身失败可能是 ADB 已断连
        triggered = False

    # 检测 ADB 是否还活着
    adb_alive = True
    try:
        adb_run(["get-state"], serial=serial, timeout=5)
    except AdbError:
        adb_alive = False

    hint = ""
    if not adb_alive:
        hint = (
            "⚠️ ADB 已断连（深度休眠导致 USB 控制器挂死）。"
            "恢复方法: serial(action='send', command='reboot') 通过串口重启，"
            "或长按电源键唤醒设备。"
        )

    return {"triggered": triggered, "adb_alive": adb_alive, "hint": hint}


# ═══════════════════════════════════════════════════════════════
# MCP 工具业务逻辑层（从 adb_server.py 下沉）
# 接收 serial=""，内部 catch 异常返回 str，不 raise
# ═══════════════════════════════════════════════════════════════

def _run_cmds_with_disconnect_recovery(
    cmds: list[str], serial: str, timeout: int = 30,
) -> list[str]:
    """逐条跑 adb shell 命令，检测断连并等待恢复。

    reproduce/verify/_run_assertions 共用的命令执行+断连检测块。
    返回格式化输出行列表。
    """
    cmd_outputs = []
    for cmd in cmds:
        out, err, ec = adb_shell(cmd, serial=serial, timeout=timeout)
        cmd_outputs.append(f"$ adb shell {cmd}\n[exit={ec}] {out.strip() or err.strip()}")

        combined = (err + out).lower()
        if ec != 0 and ("not found" in combined or "device offline" in combined or "device '" in combined):
            cmd_outputs.append("  ⚠️ ADB 断连，等待设备恢复...")
            online, hint = wait_for_device_reconnect(serial, timeout=60)
            cmd_outputs.append(f"  {'✅' if online else '❌'} {hint}")
            if not online:
                break
        elif ec != 0 and "error" in combined:
            cmd_outputs.append("  ⚠️ 命令失败，可能影响复现")
    return cmd_outputs


def reproduce(
    reproduce_commands: list[str],
    logcat_filter: str = "",
    logcat_lines: int = 500,
    clear_log: bool = True,
    serial: str = "",
) -> str:
    """跑复现命令序列并抓 logcat，返回日志证据。"""
    try:
        serial = resolve_device(serial)

        if clear_log:
            clear_logcat(serial=serial)

        cmd_outputs = _run_cmds_with_disconnect_recovery(reproduce_commands, serial)

        logcat_result = adb_logcat(
            filter=logcat_filter, lines=logcat_lines, serial=serial, timeout=30,
        )

        result = "=== 复现命令输出 ===\n" + "\n\n".join(cmd_outputs)
        result += "\n\n=== Logcat ===\n" + logcat_result
        return result
    except Exception as e:
        return f"❌ 复现失败: {e}"


def _run_assertions(assertions: list[dict], serial: str, logcat_lines: int) -> str:
    """断言模式：对每个 assertion 独立 clear_logcat → 跑 commands → 抓 logcat → 判定。

    每个断言: {name, commands, logcat_filter, expect("present"/"absent")}
    """
    lines_out = []
    all_pass = True

    for i, assertion in enumerate(assertions):
        name = assertion.get("name", f"断言{i+1}")
        cmds = assertion.get("commands", [])
        a_filter = assertion.get("logcat_filter", "")
        expect = assertion.get("expect", "absent")

        clear_logcat(serial=serial)

        cmd_outs = _run_cmds_with_disconnect_recovery(cmds, serial)

        lc = adb_logcat(filter=a_filter, lines=logcat_lines, serial=serial, timeout=30)

        keyword_found = False
        if a_filter:
            keywords = [kw.strip().lower() for kw in a_filter.split("|") if kw.strip()]
            keyword_found = any(
                kw in line.lower()
                for line in lc.split("\n")
                for kw in keywords
            )

        if expect == "present":
            passed = keyword_found
        else:
            passed = not keyword_found

        if not passed:
            all_pass = False

        status = "✅ PASS" if passed else "❌ FAIL"
        lines_out.append(f"--- 断言: {name} [{status}] ---")
        lines_out.append("期望: " + (f"关键词 '{a_filter}' 应出现" if expect == "present"
                                     else f"关键词 '{a_filter}' 不应出现"))
        lines_out.append("\n".join(cmd_outs))
        if a_filter:
            lines_out.append(f"Logcat:\n{lc}")
        lines_out.append("")

    summary = f"\n=== 断言总结 ===\n{'✅ 全部通过' if all_pass else '❌ 有断言未通过'}"
    return "\n".join(lines_out) + summary


def verify(
    patches: list[tuple[str, str]],
    verify_commands: list[str] | None = None,
    logcat_filter: str = "",
    logcat_lines: int = 500,
    assertions: list[dict] | None = None,
    serial: str = "",
) -> str:
    """推补丁到设备 + 重启 + 验证。

    patches: [(local_path, remote_path), ...]
    """
    try:
        serial = resolve_device(serial)

        clock_warn = check_device_clock(serial=serial)

        push_result = push_patch_and_restart(patches, serial=serial, reboot=True)

        if not push_result["online"]:
            return f"❌ 设备重启后未上线，无法验证\n推送: {push_result}"

        result = f"=== 推送+重启 ===\n{push_result}\n"
        if clock_warn:
            result += f"\n⚠️ {clock_warn}\n"

        if assertions:
            return result + _run_assertions(assertions, serial, logcat_lines)

        verify_commands = verify_commands or []
        clear_logcat(serial=serial)
        cmd_outputs = _run_cmds_with_disconnect_recovery(verify_commands, serial)

        logcat_result = adb_logcat(
            filter=logcat_filter, lines=logcat_lines, serial=serial, timeout=30,
        )

        symptom_gone = True
        if logcat_filter:
            keywords = [kw.strip().lower() for kw in logcat_filter.split("|") if kw.strip()]
            symptom_gone = not any(
                kw in line.lower()
                for line in logcat_result.split("\n")
                for kw in keywords
            )

        result += f"\n=== 验证命令 ===\n" + "\n\n".join(cmd_outputs)
        result += f"\n\n=== Logcat ===\n{logcat_result}"
        result += f"\n\n=== 判定 ===\nsymptom_gone={symptom_gone}"
        return result
    except Exception as e:
        return f"❌ 验证失败: {e}"


def flash(patches: list[tuple[str, str]], reboot: bool = True, serial: str = "") -> str:
    """推补丁文件到设备并重启。"""
    try:
        serial = resolve_device(serial)
        result = push_patch_and_restart(patches, serial=serial, reboot=reboot)
        status = "✅" if result["online"] else "❌"
        return (f"{status} 刷机完成: pushed={result['pushed']}, "
                f"rebooted={result['rebooted']}, online={result['online']}, "
                f"elapsed={result['elapsed_s']:.1f}s")
    except Exception as e:
        return f"❌ 刷机失败: {e}"


def screenshot(local_path: str = "", serial: str = "") -> str:
    """设备截图。local_path 不传则存临时目录。返回本地文件路径。"""
    try:
        serial = resolve_device(serial)
        import tempfile
        if not local_path:
            local_path = os.path.join(
                tempfile.gettempdir(), f"bugfix_screenshot_{os.getpid()}.png")
        remote = "/sdcard/bugfix_screenshot.png"
        adb_shell(f"screencap -p {remote}", serial=serial, timeout=10)
        adb_run(["pull", remote, local_path], serial=serial, timeout=15)
        adb_shell(f"rm {remote}", serial=serial, timeout=5)
        if os.path.isfile(local_path):
            return f"截图已保存: {local_path}"
        return "截图失败"
    except Exception as e:
        return f"❌ 截图失败: {e}"
