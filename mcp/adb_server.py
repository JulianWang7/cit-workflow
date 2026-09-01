"""adb-mcp — ADB 设备操作 MCP server（薄入口）。

ZCode 插件自动启动此 server（plugin.json mcpServers.adb-mcp）。
stdio JSON-RPC → bugflow.core.adb 转发，所有能力逻辑在 pip 包的 core 层。

工具列表（对应 fixer 工作流步骤）：
  device_state   — 列设备/占用/归还（orchestrator + reproduce 前检查）
  reproduce      — 跑复现序列抓 logcat（reproduce 步骤）
  verify         — 推补丁+重启+重跑验证（verify 步骤，内部含刷机）
  flash          — 基线刷机（verify 内部调，也可单独调）
  adb_shell      — 通用 adb shell（只读探查）
  clear_logcat_buffer — 清 logcat 缓冲（复现前调）
  screenshot     — 截图（UI 复现辅助）
  ui_*           — UI 自动化（dump/tap/swipe/scroll/type/key/start/wait）
  adb_suspend    — 触发设备挂起（电源/唤醒 Bug 测试）
  fastboot_*     — fastboot 模式刷分区/重启/查变量/OEM/擦除
  qcom_*         — 高通 EDL 整刷/单分区刷写（xPCAT）
  spd_*          — 展锐 .pac 整刷（CmdDloader）
  flash_status   — 轮询后台刷机任务状态（spd/qcom background 模式）
  flash_stop     — 终止后台刷机任务
  flash_list     — 列出所有刷机后台任务
  firmware_fetch — 从 85 共享浏览/拉取固件包，解压到本地供整刷
  serial_*       — 串口通信（ADB 断连时的生命线）

环境变量（orchestrator 注入，ZCode 会话继承 → MCP server 读取）：
  BUGFIX_DEVICE_SERIAL — ADB 设备 serial（不传则自动选唯一设备）
"""

from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_here)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp SDK 未安装。请 pip install 'bugflow[mcp]'", file=sys.stderr)
    sys.exit(1)

from bugflow.core.config import get_bugfix_env
from bugflow.core.adb import (
    AdbError,
    adb_devices,
    adb_run,
    adb_shell as _adb_shell,
    adb_logcat,
    clear_logcat,
    resolve_device,
    wait_for_device,
    wait_for_boot_completed as _wait_for_boot_completed,
    is_device_online as _is_device_online,
    wait_for_device_reconnect as _wait_for_reconnect,
    adb_root,
    adb_remount,
    adb_push,
    push_patch_and_restart,
    get_main_activity as _get_main_activity,
    check_device_clock as _check_device_clock,
    ui_snapshot as _ui_snapshot,
    ui_tap as _ui_tap,
    ui_tap_text as _ui_tap_text,
    ui_swipe as _ui_swipe,
    ui_scroll as _ui_scroll,
    ui_type as _ui_type,
    ui_key as _ui_key,
    ui_start_app as _ui_start_app,
    ui_wait_text as _ui_wait_text,
    trigger_suspend as _trigger_suspend,
    reproduce as _reproduce,
    verify as _verify,
    flash as _flash,
    screenshot as _screenshot,
)
from bugflow.core.fastboot import (
    FastbootError,
    fastboot_devices as _fb_devices,
    fastboot_flash as _fb_flash,
    fastboot_reboot as _fb_reboot,
    fastboot_getvar as _fb_getvar,
    fastboot_oem as _fb_oem,
    fastboot_erase as _fb_erase,
)
from bugflow.core.qcom_flash import (
    list_devices as _qcom_devices,
    flash_build as _qcom_flash,
    flash_single_image as _qcom_flash_single,
    reboot_edl as _qcom_reboot_edl,
)
from bugflow.core.spd_flash import (
    list_devices as _spd_devices,
    flash_pac as _spd_flash,
)
from bugflow.core import flash_tasks as _flash_tasks
from bugflow.core import firmware as _fw
from bugflow.core.serial import (
    SerialError,
    list_ports as _serial_list_ports,
    serial_open as _serial_open,
    serial_send as _serial_send,
    serial_read as _serial_read,
    serial_close as _serial_close,
)


mcp = FastMCP("adb-mcp")


# ── 辅助 ──────────────────────────────────────────────────────
def _serial() -> str:
    """从 env 读 device serial，空则 resolve_device 自动选。"""
    return get_bugfix_env()["device_serial"]


def _resolved_serial() -> str:
    """确保拿到有效 serial（env 有就用，没有自动选）。"""
    return resolve_device(_serial())


# ── 工具：device_state ────────────────────────────────────────
@mcp.tool()
def device_state() -> str:
    """列出所有连接的 Android 设备及状态。用于确认设备在线、多设备时选 serial。"""
    return adb_devices()


# ── 工具：reproduce ───────────────────────────────────────────
@mcp.tool()
def reproduce(
    reproduce_commands: list[str],
    logcat_filter: str = "",
    logcat_lines: int = 500,
    clear_log: bool = True,
) -> str:
    """跑复现命令序列并抓 logcat，返回日志证据。

    流程: clear_logcat（可选）→ 依次跑 reproduce_commands → 抓 logcat -d -t <lines>。
    reproduce_commands: adb shell 命令列表（如 ['am start -n com.foo/.MainActivity', 'input tap 100 200']）。
    logcat_filter: 关键字过滤（| 分隔多个），如 'AndroidRuntime|FATAL|denied'。
    """
    try:
        serial = _resolved_serial()
        return _reproduce(
            reproduce_commands, logcat_filter=logcat_filter,
            logcat_lines=logcat_lines, clear_log=clear_log, serial=serial,
        )
    except Exception as e:
        return f"❌ {e}"


# ── 工具：verify ──────────────────────────────────────────────
@mcp.tool()
def verify(
    patches: list[dict],
    verify_commands: list[str] = None,
    logcat_filter: str = "",
    logcat_lines: int = 500,
    assertions: list[dict] = None,
) -> str:
    """推补丁到设备 + 重启 + 验证。

    两种验证模式（二选一）：
    1. 经典模式（默认）：verify_commands + logcat_filter → symptom_gone 判定
    2. 断言模式：传 assertions 支持多场景双向验证
       （如"普通号被阻止 + 紧急号可拨出"需同时验证正反两面）

    patches: [{local_path, remote_path}, ...]（如 [{local_path: '/tmp/foo.apk', remote_path: '/system/app/foo/foo.apk'}]）
    verify_commands: 经典模式下重启后跑的 adb shell 命令列表（复现原 bug 的序列）。
    logcat_filter: 经典模式下的验证关键词（如原 bug 的 denial/crash 关键词）。
    assertions: 断言列表，每项格式:
        {
          "name": "普通号被阻止",
          "commands": ["am start -a android.intent.action.CALL -d tel:12345678"],
          "logcat_filter": "blocked|denied",
          "expect": "present"  # present=关键词应出现, absent=关键词不应出现
        }

    返回: 推送结果 + 验证输出 + logcat + 判定。
    """
    try:
        serial = _resolved_serial()
        patch_pairs = [(p["local_path"], p["remote_path"]) for p in patches]
        return _verify(
            patch_pairs, verify_commands=verify_commands,
            logcat_filter=logcat_filter, logcat_lines=logcat_lines,
            assertions=assertions, serial=serial,
        )
    except Exception as e:
        return f"❌ {e}"


# ── 工具：flash ───────────────────────────────────────────────
@mcp.tool()
def flash(patches: list[dict], reboot: bool = True) -> str:
    """推补丁文件到设备并重启。patches: [{local_path, remote_path}, ...]。

    内部流程: root → remount（必要时 reboot）→ push → reboot → wait-for-device。
    verify 工具内部已调此流程，单独调用于纯刷机场景。
    """
    try:
        serial = _resolved_serial()
        patch_pairs = [(p["local_path"], p["remote_path"]) for p in patches]
        return _flash(patch_pairs, reboot=reboot, serial=serial)
    except Exception as e:
        return f"❌ {e}"


# ── 工具：adb_shell ───────────────────────────────────────────
@mcp.tool()
def adb_shell(command: str, timeout: int = 15) -> str:
    """在设备上执行只读 shell 命令（如 ls /system/app, getprop）。

    返回 stdout + stderr + exit_code。非零不抛异常。
    """
    try:
        serial = _resolved_serial()
        out, err, ec = _adb_shell(command, serial=serial, timeout=timeout)
        result = out.strip()
        if err.strip():
            result += f"\n[stderr] {err.strip()}"
        result += f"\n[exit={ec}]"
        return result
    except Exception as e:
        return f"❌ adb shell 失败: {e}"


# ── 工具：get_launch_activity ─────────────────────────────────
@mcp.tool()
def get_launch_activity(package: str) -> str:
    """查询已安装包的启动 Activity 名（MAIN/LAUNCHER）。

    推送 APK 后 Activity 名可能与猜测不同（如 .ui.TelecomActivity vs
    .ui.activity.TelecomActivity），用此工具获取正确名字避免 am start 报错。
    返回格式: "pkg/.MainActivity"。

    Args:
        package: 包名，如 com.android.dialer
    """
    try:
        serial = _resolved_serial()
        return _get_main_activity(package, serial=serial)
    except Exception as e:
        return f"❌ 查询失败: {e}"


# ── 工具：check_device_clock ──────────────────────────────────
@mcp.tool()
def check_device_clock(threshold: int = 60) -> str:
    """检测设备时钟与本机偏差。偏差大时 logcat 时间戳混乱，影响证据判断。

    Args:
        threshold: 允许的偏差秒数，默认 60s
    """
    try:
        serial = _resolved_serial()
        return _check_device_clock(serial=serial, threshold=threshold) or "✅ 设备时钟正常"
    except Exception as e:
        return f"❌ 时钟检测失败: {e}"


# ── 工具：clear_logcat ────────────────────────────────────────
@mcp.tool()
def clear_logcat_buffer() -> str:
    """清 logcat 缓冲。复现前调，确保抓到的是新日志。"""
    try:
        serial = _resolved_serial()
        clear_logcat(serial=serial)
        return "logcat 已清"
    except Exception as e:
        return f"❌ 清 logcat 失败: {e}"


# ── 工具：screenshot ──────────────────────────────────────────
@mcp.tool()
def screenshot(local_path: str = "") -> str:
    """设备截图。local_path 不传则存临时目录。返回本地文件路径。"""
    try:
        serial = _resolved_serial()
        return _screenshot(local_path=local_path, serial=serial)
    except Exception as e:
        return f"❌ {e}"


# ── 工具：UI 自动化（uiautomator dump + input 命令）────────────
@mcp.tool()
def ui_snapshot(filter: str = "") -> str:
    """获取当前屏幕 UI 元素树（uiautomator dump）。filter 可过滤包名。"""
    try:
        return _ui_snapshot(filter=filter, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_tap(x: int, y: int) -> str:
    """点击屏幕坐标。"""
    try:
        return _ui_tap(x, y, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_tap_text(text: str, exact: bool = False, index: int = 0) -> str:
    """点击屏幕上指定文本。exact=True 精确匹配，index 选第几个。"""
    try:
        return _ui_tap_text(text, exact=exact, index=index, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_swipe(x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> str:
    """从 (x1,y1) 滑动到 (x2,y2)，duration 毫秒。"""
    try:
        return _ui_swipe(x1, y1, x2, y2, duration=duration, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_scroll(direction: str = "down", filter: str = "", return_snapshot: bool = False) -> str:
    """滚动屏幕。direction: up/down/left/right。return_snapshot=True 返回滚动后的 UI 树。"""
    try:
        return _ui_scroll(
            direction=direction, filter=filter,
            return_snapshot=return_snapshot, serial=_serial(),
        )
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_type(text: str, clear_first: bool = False) -> str:
    """输入文本。clear_first=True 先清空输入框。"""
    try:
        return _ui_type(text, clear_first=clear_first, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_key(key: str) -> str:
    """按键。key: BACK/HOME/ENTER/APP_SWITCH/MENU/SEARCH 等。"""
    try:
        return _ui_key(key, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_start_app(target: str) -> str:
    """启动应用。target: 包名/Activity/URI。"""
    try:
        return _ui_start_app(target, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def ui_wait_text(text: str, timeout: int = 10, interval: float = 1.0) -> str:
    """等待屏幕上出现指定文本，超时返回失败。"""
    try:
        return _ui_wait_text(text, timeout=timeout, interval=interval, serial=_serial())
    except Exception as e:
        return f"❌ {e}"


# ── 工具：adb_suspend（电源/唤醒 Bug 测试）─────────────────────
@mcp.tool()
def adb_suspend(duration: int = 10) -> str:
    """触发设备挂起（suspend）用于电源/唤醒 Bug 测试。duration 秒后唤醒。"""
    try:
        result = _trigger_suspend(serial=_serial(), duration=duration)
    except Exception as e:
        return f"❌ {e}"
    status = "✅" if result["adb_alive"] else "⚠️"
    lines = [f"{status} suspend 已触发 (duration={duration}s)"]
    lines.append(f"  triggered={result['triggered']}, adb_alive={result['adb_alive']}")
    if result["hint"]:
        lines.append(result["hint"])
    return "\n".join(lines)


# ── 工具：fastboot（fastboot 模式刷机）─────────────────────────
@mcp.tool()
def fastboot_devices() -> str:
    """列出 fastboot 模式设备。"""
    try:
        return _fb_devices()
    except FastbootError as e:
        return f"❌ {e}"


@mcp.tool()
def fastboot_flash(partition: str, image: str, device_serial: str = "") -> str:
    """刷写分区。partition: 如 boot/vendor/system。image: 本地镜像路径。
    device_serial: 设备序列号，空则用 workspace 中已设的 serial（多设备场景需指定）。
    """
    try:
        return _fb_flash(partition, image, serial=device_serial or _serial())
    except FastbootError as e:
        return f"❌ {e}"


@mcp.tool()
def fastboot_reboot(target: str = "normal", device_serial: str = "") -> str:
    """重启设备。target: normal/bootloader/recovery。
    device_serial: 设备序列号，空则用 workspace 中已设的 serial（多设备场景需指定）。
    """
    # core fastboot_reboot 用 "" / "system" 表示正常重启，其它值直接拼到 reboot 后
    target_arg = "" if target in ("normal", "system", "") else target
    try:
        return _fb_reboot(target=target_arg, serial=device_serial or _serial())
    except FastbootError as e:
        return f"❌ {e}"


@mcp.tool()
def fastboot_getvar(var: str = "all", device_serial: str = "") -> str:
    """查询 fastboot 变量。var=all 查全部。
    device_serial: 设备序列号，空则用 workspace 中已设的 serial（多设备场景需指定）。
    """
    try:
        return _fb_getvar(var=var, serial=device_serial or _serial())
    except FastbootError as e:
        return f"❌ {e}"


@mcp.tool()
def fastboot_oem(command: str, device_serial: str = "") -> str:
    """执行 OEM 命令。
    device_serial: 设备序列号，空则用 workspace 中已设的 serial（多设备场景需指定）。
    """
    try:
        return _fb_oem(oem_cmd=command, serial=device_serial or _serial())
    except FastbootError as e:
        return f"❌ {e}"


@mcp.tool()
def fastboot_erase(partition: str, device_serial: str = "") -> str:
    """擦除分区。
    device_serial: 设备序列号，空则用 workspace 中已设的 serial（多设备场景需指定）。
    """
    try:
        return _fb_erase(partition, serial=device_serial or _serial())
    except FastbootError as e:
        return f"❌ {e}"


# ── 工具：qcom（高通 EDL 整刷，xPCAT）──────────────────────────
@mcp.tool()
def qcom_devices() -> str:
    """列出高通 EDL 模式设备。"""
    try:
        return _qcom_devices()
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def qcom_flash(
    build_path: str,
    memory_type: str = "UFS",
    flavor: str = "",
    device_serial: str = "",
    erase: bool = True,
    reset: bool = True,
    force_edl_mode: bool = False,
    device_programmer: str = "",
    rawprog: str = "",
    patchprog: str = "",
    skip_sahara: bool = False,
    timeout: int = 600,
    background: bool = True,
) -> str:
    """高通整包刷机。build_path: 编译产物目录（Meta build 指向 contents.xml，需 flavor；Flat build 指向目录，不需 flavor）。
    memory_type: UFS/EMMC/NAND/SPINOR。flavor: asic/core_asic（meta build 必填）。force_edl_mode: 先强制进 EDL。
    device_programmer: firehose programmer 文件路径（如 xbl_s_devprg_ns.melf），覆盖默认选择。
    rawprog: rawprogram XML 文件名，多个用分号分隔（如 "rawprogram0_WIPE_PARTITIONS.xml;rawprogram0_split.xml"）。
    patchprog: patch XML 文件名（如 "patch0.xml"）。skip_sahara: 跳过 Sahara 握手。
    background: 后台模式（默认 True），整刷耗时 5-15 分钟，MCP 有 30s 超时必须后台执行。
      返回 task_id 后用 flash_status(task_id=...) 轮询进度，flash_stop(task_id=...) 终止。
    固件包可从 85 共享拉取：firmware_fetch(action="products") 浏览 → fetch 解压到本地。
    """
    try:
        return _qcom_flash(
            build_path,
            memory_type=memory_type,
            flavor=flavor,
            device_serial=device_serial,
            erase=erase,
            reset=reset,
            force_edl_mode=force_edl_mode,
            device_programmer=device_programmer,
            rawprog=rawprog,
            patchprog=patchprog,
            skip_sahara=skip_sahara,
            timeout=timeout,
            background=background,
        )
    except Exception as e:
        return f"❌ 高通整刷失败: {e}"


@mcp.tool()
def qcom_reboot_edl(device_serial: str = "") -> str:
    """通过 adb reboot edl 进入 9008 EDL 模式（不依赖 QUTS/xPCAT）。

    设备需先在 ADB 模式。比 force_edl_mode 更轻量，直接走 ADB 通道。
    device_serial: ADB 设备序列号，空则不指定（单设备场景）。
    """
    try:
        return _qcom_reboot_edl(device_serial)
    except Exception as e:
        return f"❌ reboot edl 失败: {e}"


@mcp.tool()
def qcom_flash_single(
    image: str,
    device_programmer: str,
    memory_type: str = "UFS",
    device_serial: str = "",
    lun: int = 0,
    start_sector: int = 0,
    timeout: int = 300,
    background: bool = True,
) -> str:
    """高通单分区刷写。image: 本地镜像，device_programmer: firehose programmer 文件路径。
    memory_type: UFS/EMMC。lun: LUN 编号。start_sector: 起始扇区。
    background: 后台模式（默认 True），单分区刷写可能 >30s，后台执行避免 MCP 超时。
    """
    try:
        return _qcom_flash_single(
            image,
            device_programmer,
            memory_type=memory_type,
            device_serial=device_serial,
            lun=lun,
            start_sector=start_sector,
            timeout=timeout,
            background=background,
        )
    except Exception as e:
        return f"❌ 高通单分区刷写失败: {e}"


# ── 工具：spd（展锐 download 整刷，CmdDloader）─────────────────
@mcp.tool()
def spd_devices() -> str:
    """列出展锐 download 模式设备（SPRD COM 口）。

    设备需先进入 download 模式（关机→按住音量下+电源插 USB），
    Windows 才会枚举描述含 SPRD 的 COM 口。
    """
    try:
        return _spd_devices()
    except Exception as e:
        return f"❌ {e}"


@mcp.tool()
def spd_flash(
    pac_path: str,
    port: int = 0,
    skip_fileid: str = "",
    reset: bool = True,
    version: int = 0,
    timeout: int = 600,
    background: bool = True,
) -> str:
    """展锐 .pac 整包刷机。pac_path: 展锐固件包（.pac 文件）。
    port: COM 端口号数字（如 5=COM5，0=自动查找）。skip_fileid: 跳过的分区 FileID（逗号分隔，展锐的"部分刷"替代方案）。
    reset: 刷完重启（默认 True）。version: 0=ResearchDownload(默认)/1=Factory/2=Upgrade。timeout: 超时秒，默认 600。
    background: 后台模式（默认 True），整刷耗时 3-10 分钟，MCP 有 30s 超时必须后台执行。
      返回 task_id 后用 flash_status(task_id=...) 轮询进度，flash_stop(task_id=...) 终止。
    固件包可从 85 共享拉取：firmware_fetch(action="products") 浏览 → fetch 解压到本地。
    """
    try:
        return _spd_flash(
            pac_path,
            port=port,
            skip_fileid=skip_fileid,
            reset=reset,
            version=version,
            timeout=timeout,
            background=background,
        )
    except Exception as e:
        return f"❌ 展锐整刷失败: {e}"


# ── 工具：flash 后台任务管理（spd_flash/qcom_flash 后台轮询）─────
@mcp.tool()
def flash_status(task_id: str) -> str:
    """轮询后台刷机任务状态（spd_flash/qcom_flash background=True 返回的 task_id）。

    返回任务状态（running/completed/failed/stopped）+ log 尾部 + 已耗时。
    典型用法：spd_flash(background=True) → 拿到 task_id → 循环 flash_status 直到 completed/failed。
    """
    try:
        r = _flash_tasks.check_flash_task(task_id)
        status = r.get("status", "?")
        ec = r.get("exit_code")
        elapsed = r.get("elapsed", 0)
        log_tail = r.get("log_tail", "")
        platform = r.get("platform", "")

        if status == "running":
            return (
                f"⏳ 刷机任务运行中 [{platform}]\n"
                f"  task_id: {task_id}\n"
                f"  已耗时: {int(elapsed)}s\n"
                f"  --- 日志尾部 ---\n{log_tail}"
            )
        elif status == "completed":
            return (
                f"✅ 刷机任务完成 [{platform}]\n"
                f"  task_id: {task_id}\n"
                f"  耗时: {int(elapsed)}s\n"
                f"  --- 日志尾部 ---\n{log_tail}"
            )
        elif status == "failed":
            return (
                f"❌ 刷机任务失败 [{platform}]\n"
                f"  task_id: {task_id}\n"
                f"  exit_code: {ec}\n"
                f"  耗时: {int(elapsed)}s\n"
                f"  --- 日志尾部 ---\n{log_tail}"
            )
        elif status == "stopped":
            return (
                f"⏹ 刷机任务已终止 [{platform}]\n"
                f"  task_id: {task_id}\n"
                f"  耗时: {int(elapsed)}s"
            )
        else:
            return f"❓ 未知任务: {task_id} (status={status})"
    except Exception as e:
        return f"❌ 查询刷机状态失败: {e}"


@mcp.tool()
def flash_stop(task_id: str) -> str:
    """终止后台刷机任务。task_id 来自 spd_flash/qcom_flash(background=True) 的返回值。"""
    try:
        r = _flash_tasks.stop_flash_task(task_id)
        return f"⏹ 已终止刷机任务 {task_id} (pid={r.get('pid', '?')})"
    except Exception as e:
        return f"❌ 终止刷机任务失败: {e}"


@mcp.tool()
def flash_list() -> str:
    """列出所有刷机后台任务（含历史），用于查看有哪些任务在跑或已完成。"""
    try:
        tasks = _flash_tasks.list_flash_tasks()
        if not tasks:
            return "无刷机任务记录。"
        lines = [f"刷机任务列表 ({len(tasks)}):"]
        for t in tasks:
            status_icon = {"running": "⏳", "completed": "✅",
                           "failed": "❌", "stopped": "⏹"}.get(t["status"], "?")
            lines.append(
                f"  {status_icon} {t['task_id']}  [{t['platform']}]  "
                f"{t['status']}  {t['elapsed']}s  pid={t['pid']}  {t['exe']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 列刷机任务失败: {e}"


# ── 工具：serial（串口通信，ADB 断连时的生命线）────────────────
@mcp.tool()
def serial_ports() -> str:
    """枚举可用串口端口。"""
    try:
        return _serial_list_ports()
    except SerialError as e:
        return f"❌ {e}"


@mcp.tool()
def serial_open(port: str = "", baudrate: int = 0, platform: str = "", timeout: float = 1.0) -> str:
    """打开串口连接。

    port: 串口设备名。空=自动检测（1 个串口直接选，多个按转接芯片过滤）。
    baudrate: 波特率，0=自动（从 platform 查）。手动指定如 115200/921600。
    platform: 平台名（如 SRM965, uis7870_3h10_car_native）。
              baudrate=0 时自动查速率：展锐=921600，高通=115200。
    """
    try:
        return _serial_open(port, baudrate=baudrate, platform=platform, timeout=timeout)
    except SerialError as e:
        return f"❌ {e}"


@mcp.tool()
def serial_send(command: str, wait: float = 2.0) -> str:
    """发送命令到串口，等待 wait 秒读取响应。"""
    try:
        return _serial_send(command, wait=wait)
    except SerialError as e:
        return f"❌ {e}"


@mcp.tool()
def serial_read(duration: int = 10) -> str:
    """持续读取串口 duration 秒。"""
    try:
        return _serial_read(duration=duration)
    except SerialError as e:
        return f"❌ {e}"


@mcp.tool()
def serial_close() -> str:
    """关闭串口连接。"""
    try:
        return _serial_close()
    except SerialError as e:
        return f"❌ {e}"


# ── 工具：firmware_fetch（从 85 共享拉取固件包）─────────────────
@mcp.tool()
def firmware_fetch(
    action: str = "products",
    product: str = "",
    subdir: str = "",
    version: str = "",
    local_dir: str = "",
    keyword: str = "",
    keep: int = 0,
) -> str:
    """从内网 85 共享浏览/拉取固件包，解压到本地供整刷。

    依赖 Windows 已缓存 85 服务器 SMB 凭证。首次使用请在资源管理器
    访问 \\\\192.168.0.85 并勾选"记住凭据"。

    典型流程：products → browse → versions → fetch → qcom_flash/spd_flash

    Args:
        action: products(列产品)/browse(列子目录)/versions(列版本)/fetch(拉取解压)/clean(清缓存)
        product: 产品名（browse/versions/fetch 必填），如 SRM965
        subdir: 子目录名（versions/fetch 必填），从 browse 结果获取
        version: 版本目录名（fetch 必填），从 versions 结果获取
        local_dir: 本地缓存目录（fetch/clean 可选，空则用上次保存的偏好）
        keyword: 产品过滤关键词（products 用），如 SRM
        keep: 清缓存时保留最近 N 个版本（clean 用），0=全部清空
    """
    import logging
    logger = logging.getLogger("bugflow.mcp.adb")

    action_norm = (action or "products").strip().lower()
    try:
        if action_norm == "products":
            return _fw.list_products(keyword=keyword)
        elif action_norm == "browse":
            return _fw.browse_product(product=product)
        elif action_norm == "versions":
            return _fw.list_versions(product=product, subdir=subdir)
        elif action_norm == "fetch":
            return _fw.fetch_firmware(
                product=product, subdir=subdir, version=version, local_dir=local_dir
            )
        elif action_norm == "clean":
            return _fw.clean_firmware_cache(local_dir=local_dir, keep=keep)
        else:
            return (
                f"❌ 未知 action「{action}」。支持: "
                "products/browse/versions/fetch/clean"
            )
    except Exception as e:
        logger.exception("firmware_fetch 异常 (action=%s)", action_norm)
        return f"❌ 固件拉取失败: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
