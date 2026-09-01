# -*- coding: utf-8 -*-
"""展锐 ResearchDownload 整刷模块 — 通过 CmdDloader.exe 命令行实现。

CmdDloader.exe 是展锐 ResearchDownload 工具的命令行版本，
支持 .pac 固件包整刷，适合自动化场景。

CmdDloader.exe 路径自动发现，优先级:
  1. 环境变量 CMDDLOADER_PATH（用户手动指定）
  2. 常见路径扫描（D:\\soft\\Download_*\\Download_*\\Bin\\CmdDloader.exe，glob 模糊匹配）
  3. PATH 环境变量（where CmdDloader）

典型流程:
  1. spd_devices()  → 列出展锐 download 模式设备（SPRD COM 口）
  2. spd_flash(pac_path, port=5)  → .pac 整刷

注意:
  展锐设备进 download 模式靠硬件按键组合（通常关机→按住音量下+电源插 USB），
  无可靠的 adb 命令可强制进入，故本模块不提供 force_download_mode。
  设备进入 download 模式后，Windows 会枚举一个描述含 "SPRD" 的 COM 口，
  本模块的 list_devices() 正向匹配该标识（与 serial.py 排除 SPRD 的逻辑相反，
  因为用途不同: serial.py 找的是外接调试转接芯片，本模块找的是设备自身的下载口）。
"""
import os
import re
import glob
import shutil
import subprocess
import threading
import logging

from bugflow.core import flash_tasks

try:
    from serial.tools import list_ports as _list_ports
except ImportError:
    _list_ports = None

logger = logging.getLogger(__name__)

# 常见安装路径前缀（glob 模糊匹配，兼容版本升级换目录）
_CANDIDATE_GLOBS = [
    r"D:\soft\Download_*\Download_*\Bin\CmdDloader.exe",
    r"D:\soft\*\Download_*\Bin\CmdDloader.exe",
    r"C:\Program Files*\*\Bin\CmdDloader.exe",
    r"D:\Program Files*\*\Bin\CmdDloader.exe",
    r"C:\Spreadtrum\*\Bin\CmdDloader.exe",
    r"D:\Spreadtrum\*\Bin\CmdDloader.exe",
    os.path.expanduser(r"~\Downloads\*\Bin\CmdDloader.exe"),
]

# 展锐 download 模式设备的 COM 口标识关键词
# 设备进入 download 模式后，Windows 枚举的 COM 口描述含 "U2S"（如 "SPRD U2S Debugger"）
_SPDR_DOWNLOAD_HINTS = [
    "u2s",           # SPRD U2S Debugger / Verbose（download 模式专用口）
    "download",
]

# 展锐设备正常运行时的 modem 控制口标识（不能用于刷机，但能说明设备在线）
# 如 "SPRD AT"、"SPRD LTE AT"、"SPRD WCN AT"
_SPDR_AT_HINTS = [
    "sprd",
    "spreadtrum",
    "unisoc",
]

_cmddloader_cache: str | None = None
_cmddloader_lock = threading.Lock()


def _find_cmddloader() -> str | None:
    """动态发现 CmdDloader.exe 路径。结果缓存，只查一次。"""
    global _cmddloader_cache
    with _cmddloader_lock:
        if _cmddloader_cache and os.path.isfile(_cmddloader_cache):
            return _cmddloader_cache

    # 1. 环境变量
    env_path = os.environ.get("CMDDLOADER_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        _cmddloader_cache = env_path
        logger.info("CmdDloader found via env CMDDLOADER_PATH: %s", env_path)
        return env_path

    # 2. 常见路径 glob 扫描（兼容版本号目录名变化）
    for pattern in _CANDIDATE_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            _cmddloader_cache = matches[0]
            logger.info("CmdDloader found via glob %s: %s", pattern, matches[0])
            return matches[0]

    # 3. PATH 环境变量
    found = shutil.which("CmdDloader") or shutil.which("CmdDloader.exe")
    if found and os.path.isfile(found):
        _cmddloader_cache = found
        logger.info("CmdDloader found via PATH: %s", found)
        return found

    return None


def _cmddloader_path() -> str:
    """返回 CmdDloader.exe 路径，找不到时返回空字符串。"""
    found = _find_cmddloader()
    if not found:
        return ""
    return found


def _run_cmddloader(args: list[str], timeout: int = 600) -> tuple[str, str, int]:
    """执行 CmdDloader.exe，返回 (stdout, stderr, returncode)。"""
    exe = _cmddloader_path()
    if not exe:
        return (
            "",
            "CmdDloader.exe 未找到。请通过以下方式之一解决:\n"
            "  1. 设置环境变量 CMDDLOADER_PATH 指向 CmdDloader.exe 完整路径\n"
            "  2. 确认展锐 ResearchDownload 已安装（通常在 D:\\soft\\Download_*\\...\\Bin\\下）\n"
            "  3. 将 CmdDloader 所在 Bin 目录加入 PATH",
            127,
        )
    cmd = [exe] + args
    logger.info("CmdDloader cmd: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"CmdDloader 超时 ({timeout}s)", 124


def list_devices() -> str:
    """列出当前连接的展锐 download 模式设备。

    展锐设备进入 download 模式后，Windows 会枚举描述含 "U2S" 的 COM 口
    （如 "SPRD U2S Debugger"），该口可被 CmdDloader 用于刷机。

    设备正常运行时暴露的是 "SPRD AT/LTE/WCN" 控制口，不能用于刷机。
    本函数区分两种状态，给出准确提示。

    返回设备列表文本，含 COM 口名、描述、可用的 -port 参数值。
    """
    if _list_ports is None:
        return "❌ pyserial 未安装，无法枚举 COM 口。请 `pip install pyserial`。"

    ports = list(_list_ports.comports())
    if not ports:
        return (
            "未检测到任何 COM 口。\n"
            "请确认展锐设备已进入 download 模式:\n"
            "  关机 → 按住音量下键不放 → 插入 USB → 松开按键\n"
            "  （不同机型组合可能不同，部分需按住音量下+电源）"
        )

    # 第一层：匹配 download 模式口（U2S / Download），可刷机
    download_ports = []
    for p in ports:
        searchable = f"{p.description} {p.hwid}".lower()
        if any(hint in searchable for hint in _SPDR_DOWNLOAD_HINTS):
            download_ports.append(p)

    # 第二层：匹配 SPRD AT 控制口（设备在线但未进 download 模式）
    at_ports = []
    for p in ports:
        searchable = f"{p.description} {p.hwid}".lower()
        if any(hint in searchable for hint in _SPDR_AT_HINTS):
            at_ports.append(p)

    if download_ports:
        lines = [f"展锐 download 模式设备 ({len(download_ports)})，可刷机:"]
        for i, p in enumerate(download_ports):
            m = re.search(r"\d+", p.device)
            port_num = m.group() if m else "?"
            lines.append(
                f"  [{i}] {p.device:<10} - {p.description or '?'}  "
                f"[-port {port_num}]  [{p.hwid or ''}]"
            )
        return "\n".join(lines)

    # 有 SPRD AT 口但没有 download 口 → 设备在线但未进 download 模式
    if at_ports:
        at_list = "\n".join(
            f"  {p.device:<10} - {p.description or '?'}  [{p.hwid or ''}]"
            for p in at_ports
        )
        return (
            f"⚠️ 检测到展锐设备但未进入 download 模式（只有 AT 控制口，不能刷机）。\n"
            f"AT 控制口 ({len(at_ports)}):\n{at_list}\n\n"
            f"请将设备切换到 download 模式:\n"
            f"  关机 → 按住音量下键不放 → 插入 USB → 松开按键\n"
            f"  进入 download 模式后会新增一个 'SPRD U2S' COM 口"
        )

    # 既无 download 口也无 AT 口
    all_list = "\n".join(
        f"  {p.device:<10} - {p.description or '?'}  [{p.hwid or ''}]"
        for p in ports
    )
    return (
        f"未检测到展锐设备。\n"
        f"当前 COM 口 ({len(ports)}):\n{all_list}\n\n"
        f"请确认展锐设备已连接并进入 download 模式:\n"
        f"  关机 → 按住音量下键不放 → 插入 USB → 松开按键"
    )


def flash_pac(
    pac_path: str,
    port: int = 0,
    skip_fileid: str = "",
    reset: bool = True,
    version: int = 0,
    timeout: int = 600,
    background: bool = True,
) -> str:
    """通过 CmdDloader.exe 执行展锐 .pac 整刷。

    pac_path: 展锐固件包路径（.pac 文件）。
    port: 下载端口号（COM 口数字，如 5 表示 COM5）。0=自动查找可用设备（仅单设备时有效）。
    skip_fileid: 跳过的分区 FileID，逗号分隔（如 "BOOT,DTBO"）。
      展锐的"单分区"替代方案: 整刷但跳过指定分区，达到只刷部分分区的效果。
    reset: 刷完是否重启设备，默认 True。
    version: 下载模式: 0=ResearchDownload(默认), 1=FactoryDownload, 2=UpgradeDownload。
    timeout: 超时秒数，整刷很慢，默认 600s（10 分钟）。
    background: 后台模式，默认 True。整刷耗时 3-10 分钟，MCP stdio 有 30s 硬超时，
      阻塞调用会被杀掉导致刷机中断。后台模式用 Popen 起进程立即返回 task_id，
      用 flash_status(task_id) 轮询进度。background=False 仅用于极小 .pac 快刷（<20s）。
    """
    pac_path = pac_path.strip()
    if not pac_path:
        return "❌ pac_path 不能为空"
    if not os.path.isfile(pac_path):
        return f"❌ .pac 文件不存在: {pac_path}"
    if not pac_path.lower().endswith(".pac"):
        logger.warning("pac_path 不以 .pac 结尾，仍尝试: %s", pac_path)

    if port < 0:
        return f"❌ port 不能为负数（0=自动，>0=指定 COM 端口号）"

    if version not in (0, 1, 2):
        return f"❌ version 取值 0/1/2（0=Research, 1=Factory, 2=Upgrade），当前: {version}"

    # 构造刷机命令
    args = [
        "-pac", pac_path,
        "-port", str(port),
        "-version", str(version),
        "-EZMode",       # 只输出状态和结果，便于解析
        "-DDDP",         # 禁用详细过程打印
        "-timeout", str(timeout * 1000),  # CmdDloader timeout 单位是毫秒
    ]

    if reset:
        args.append("-reset")

    if skip_fileid.strip():
        args += ["-skipfileid", skip_fileid.strip()]

    # 后台模式：Popen 起进程，立即返回 task_id
    if background:
        exe = _cmddloader_path()
        if not exe:
            return (
                "❌ CmdDloader.exe 未找到。请通过以下方式之一解决:\n"
                "  1. 设置环境变量 CMDDLOADER_PATH 指向 CmdDloader.exe 完整路径\n"
                "  2. 确认展锐 ResearchDownload 已安装\n"
                "  3. 将 CmdDloader 所在 Bin 目录加入 PATH"
            )
        task = flash_tasks.submit_flash_task(exe, args, platform="spd")
        return (
            f"⏳ 展锐整刷已提交后台执行\n"
            f"  task_id: {task.task_id}\n"
            f"  pid: {task.pid}\n"
            f"  pac: {os.path.basename(pac_path)}\n"
            f"  用 flash_status(task_id=\"{task.task_id}\") 轮询进度\n"
            f"  用 flash_stop(task_id=\"{task.task_id}\") 终止刷机"
        )

    # 阻塞模式（仅极小 .pac 快刷用）
    logger.info("开始展锐整刷(阻塞): %s", " ".join(args))
    out, err, ec = _run_cmddloader(args, timeout=timeout)

    # 解析结果
    out_lower = out.lower()
    if ec == 0 and any(kw in out_lower for kw in ("success", "complete", "done")):
        return f"✅ 展锐整刷完成\n{_output_summary(out)}"
    elif ec == 0:
        return f"✅ 展锐整刷完成 (exit=0)\n{_output_summary(out)}"
    else:
        return f"❌ 展锐整刷失败 (exit={ec})\n{_output_summary(out)}\n{err}"


def _output_summary(output: str) -> str:
    """提取 CmdDloader EZMode 输出的关键信息（状态行、结果行、错误行）。"""
    lines = output.splitlines()
    key_lines = []
    for line in lines:
        l = line.strip()
        if not l:
            continue
        ll = l.lower()
        # 保留含关键词的行
        if any(kw in ll for kw in (
            "download", "success", "fail", "error", "complete",
            "progress", "reset", "reboot", "port", "pac",
            "sector", "partition", "file", "%", "elapsed", "total",
            "ready", "connect", "start", "finish", "done",
        )):
            key_lines.append(l)
    # 最多保留 50 行避免输出过长
    if len(key_lines) > 50:
        return "\n".join(key_lines[:20]) + "\n...\n" + "\n".join(key_lines[-30:])
    return "\n".join(key_lines) if key_lines else output.strip()
