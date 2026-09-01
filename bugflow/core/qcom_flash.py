# -*- coding: utf-8 -*-
"""高通 xPCAT 整刷模块 — 通过 xPCAT.exe 命令行实现 EDL 整刷。

xPCAT 是高通官方新工具（替代 QFIL），支持新平台芯片。
本模块封装 xPCAT.exe 的 -PLUGIN SD (Software Download) 命令行接口。

xPCAT.exe 路径自动发现，优先级:
  1. 环境变量 XPCAT_PATH（用户手动指定）
  2. Windows 注册表 HKLM\\SOFTWARE\\WOW6432Node\\Qualcomm\\QPST（QPST 安装路径）
  3. PATH 环境变量（where xPCAT）
  4. 常见安装路径扫描

典型流程:
  1. qcom_devices()  → 列出设备、获取 ADB SN
  2. qcom_flash(build_path, memory_type="UFS", flavor="asic")  → EDL 整刷
"""
import os
import subprocess
import logging
import shutil
import threading
import time
try:
    import winreg
except ImportError:
    winreg = None

from bugflow.core import flash_tasks

logger = logging.getLogger(__name__)

# QUTS 服务相关常量
_QUTS_SERVICE = "QUTS"

# xPCAT 输出中含这些关键词的行视为进度/状态行，流式收集时重点保留
_PROGRESS_KEYWORDS = (
    "progress", "%", "success", "fail", "error", "complete",
    "download", "sector", "partition", "sahara", "firehose",
    "reset", "reboot", "elapsed", "total", "status", "response",
)

# 常见安装路径（按优先级排序）
_CANDIDATE_PATHS = [
    r"C:\Program Files (x86)\Qualcomm\PCAT\bin\xPCAT.exe",
    r"C:\Program Files (x86)\Qualcomm\PCAT\xPCATApp\xPCAT.exe",
    r"D:\Program Files (x86)\Qualcomm\PCAT\bin\xPCAT.exe",
    r"D:\Program Files (x86)\Qualcomm\PCAT\xPCATApp\xPCAT.exe",
]

# 注册表路径（QPST 安装信息，PCAT 通常和 QPST 一起装或同目录结构）
# winreg 仅 Windows 可用，非 Windows 平台为空列表
_REG_PATHS = []
if winreg:
    _REG_PATHS = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Qualcomm\QPST\2.0\Server"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Qualcomm\QPST\2.0\Config"),
    ]

_xpcat_cache: str | None = None


def _find_xpcat() -> str | None:
    """动态发现 xPCAT.exe 路径。结果缓存，只查一次。"""
    global _xpcat_cache
    if _xpcat_cache and os.path.isfile(_xpcat_cache):
        return _xpcat_cache

    # 1. 环境变量
    env_path = os.environ.get("XPCAT_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        _xpcat_cache = env_path
        logger.info("xPCAT found via env XPCAT_PATH: %s", env_path)
        return env_path

    # 2. 注册表（从 QPST 安装路径推导 PCAT 路径）
    for hive, reg_key in _REG_PATHS:
        try:
            with winreg.OpenKey(hive, reg_key) as key:
                server_path, _ = winreg.QueryValueEx(key, None)  # 默认值
                # QPST Server 路径如 ...\QPST\bin\QPSTServer.exe
                # PCAT 通常在 ...\Qualcomm\PCAT\bin\xPCAT.exe
                qpst_bin = os.path.dirname(server_path)
                qualcomm_dir = os.path.dirname(os.path.dirname(qpst_bin))  # ...\Qualcomm
                pcat_bin = os.path.join(qualcomm_dir, "PCAT", "bin", "xPCAT.exe")
                pcat_app = os.path.join(qualcomm_dir, "PCAT", "xPCATApp", "xPCAT.exe")
                for candidate in (pcat_bin, pcat_app):
                    if os.path.isfile(candidate):
                        _xpcat_cache = candidate
                        logger.info("xPCAT found via registry: %s", candidate)
                        return candidate
        except (FileNotFoundError, OSError):
            continue

    # 3. PATH 环境变量（where/which）
    found = shutil.which("xPCAT") or shutil.which("xPCAT.exe")
    if found and os.path.isfile(found):
        _xpcat_cache = found
        logger.info("xPCAT found via PATH: %s", found)
        return found

    # 4. 常见路径扫描
    for candidate in _CANDIDATE_PATHS:
        if os.path.isfile(candidate):
            _xpcat_cache = candidate
            logger.info("xPCAT found via common path: %s", candidate)
            return candidate

    return None


def _xpcat_path() -> str:
    """返回 xPCAT.exe 路径，找不到时返回空字符串。"""
    found = _find_xpcat()
    if not found:
        return ""
    return found


def _ensure_quts_running() -> tuple[bool, str]:
    """确保 QUTS (Qualcomm Unified Tools Service) 已启动。

    xPCAT 依赖 QUTS 服务与 9008 设备通信，未启动时会报
    "QUTS connection refused"。

    Returns:
        (是否就绪, 状态描述)
    """
    if os.name != "nt":
        return True, "非 Windows 平台，跳过 QUTS 检查"

    # 查询服务状态
    try:
        result = subprocess.run(
            ["sc", "query", _QUTS_SERVICE],
            capture_output=True, text=True, timeout=10, errors="replace",
        )
        output = result.stdout + result.stderr
        if "RUNNING" in output:
            return True, "QUTS 服务已运行"
        if _QUTS_SERVICE not in output and "指定的服务未安装" in output:
            return False, "QUTS 服务未安装，请安装 QPST Suite（含 PCAT）"
    except Exception as e:
        logger.warning("查询 QUTS 服务异常: %s", e)

    # 尝试直接启动（当前进程有权限时）
    try:
        result = subprocess.run(
            ["net", "start", _QUTS_SERVICE],
            capture_output=True, text=True, timeout=15, errors="replace",
        )
        if result.returncode == 0:
            logger.info("QUTS 服务已启动")
            return True, "QUTS 服务已启动"
        # error 5 = 拒绝访问，需要提权
        if "拒绝访问" in result.stderr or result.returncode == 2:
            # 通过 PowerShell 提权启动（会弹 UAC 框，用户需点"是"）
            logger.info("QUTS 需要管理员权限，尝试 PowerShell 提权启动...")
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Start-Process -FilePath 'net.exe' "
                 f"-ArgumentList 'start','{_QUTS_SERVICE}' -Verb RunAs -Wait"],
                capture_output=True, text=True, timeout=30, errors="replace",
            )
            # 验证是否已启动
            time.sleep(1)
            result = subprocess.run(
                ["sc", "query", _QUTS_SERVICE],
                capture_output=True, text=True, timeout=10, errors="replace",
            )
            if "RUNNING" in result.stdout:
                logger.info("QUTS 服务通过提权已启动")
                return True, "QUTS 服务已启动（通过提权）"
            return False, "QUTS 服务启动失败，请手动以管理员身份运行 'net start QUTS'"
    except Exception as e:
        logger.error("启动 QUTS 服务异常: %s", e)
        return False, f"QUTS 服务启动异常: {e}"

    return False, "QUTS 服务未运行"


def _run_xpcat(args: list[str], timeout: int = 600) -> tuple[str, str, int]:
    """执行 xPCAT.exe，流式收集输出，返回 (stdout, stderr, returncode)。

    相比阻塞式 subprocess.run，此函数用 Popen + readline 逐行读取，
    在日志中实时输出进度行，避免长时间刷机无反馈。
    """
    exe = _xpcat_path()
    if not exe:
        return (
            "",
            "xPCAT.exe 未找到。请通过以下方式之一解决:\n"
            "  1. 设置环境变量 XPCAT_PATH 指向 xPCAT.exe 完整路径\n"
            "  2. 确保 xPCAT 已安装（通常随 QPST 一起安装）\n"
            "  3. 将 xPCAT 的 bin 目录加入 PATH",
            127,
        )

    # 前置检查：确保 QUTS 服务已启动
    quts_ok, quts_msg = _ensure_quts_running()
    if not quts_ok:
        return "", f"❌ {quts_msg}", 1
    logger.info("QUTS: %s", quts_msg)

    cmd = [exe] + args
    logger.info("xPCAT cmd: %s", " ".join(cmd))

    stdout_lines: list[str] = []
    proc = None

    try:
        # stderr 合并到 stdout — 避免 pipe-buffer 死锁（stderr 写满会阻塞 stdout 读取）
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )

        # 用独立线程逐行读取 stdout（实时记录进度行），
        # 主线程做超时检测 — 避免 readline 阻塞导致超时检查永不触发
        def _reader():
            for line in proc.stdout:
                line_stripped = line.rstrip("\n\r")
                if not line_stripped:
                    continue
                stdout_lines.append(line_stripped)
                ll = line_stripped.lower()
                if any(kw in ll for kw in _PROGRESS_KEYWORDS):
                    logger.info("xPCAT: %s", line_stripped)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # 主线程等待进程结束（带超时）
        proc.wait(timeout=timeout)
        reader_thread.join(timeout=5)
        ec = proc.returncode
        return "\n".join(stdout_lines), "", ec

    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            proc.wait()
        if reader_thread.is_alive():
            reader_thread.join(timeout=2)
        stdout_lines.append(f"xPCAT 超时 ({timeout}s)")
        return "\n".join(stdout_lines), "", 124

    except Exception as e:
        # 异常时必须 kill 进程，避免孤儿 xPCAT/QUTS 占住 COM 口
        if proc is not None:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
        return "\n".join(stdout_lines), f"xPCAT 执行异常: {e}", 1


def list_devices() -> str:
    """列出当前连接的高通设备（ADB/EDL/9008）。

    返回设备列表文本，含设备 ID、描述、协议。
    """
    out, err, ec = _run_xpcat(["-DEVICES"], timeout=30)
    if ec != 0 and not out.strip():
        return f"❌ 列设备失败: {err or f'exit={ec}'}"
    # xPCAT -DEVICES 输出已是可读表格
    lines = out.strip().splitlines()
    # 过滤空行和分隔线，保留有用信息
    useful = [l for l in lines if l.strip() and not l.strip().startswith("---")]
    if not useful:
        return "未检测到高通设备。请确认设备已连接（ADB 或 EDL 9008 模式）。"
    return f"高通设备列表:\n" + "\n".join(useful)


def force_edl(device_serial: str) -> str:
    """强制设备进入 EDL 模式。

    device_serial: ADB 序列号（可通过 list_devices 获取）。
    """
    if not device_serial:
        return "❌ device_serial 不能为空"
    out, err, ec = _run_xpcat(["-MODE", "EDL", "-DEVICE", device_serial], timeout=30)
    if ec == 0:
        return f"✅ 设备 {device_serial} 已进入 EDL 模式"
    return f"❌ 进 EDL 失败 (exit={ec}): {err or out}"


def reboot_edl(device_serial: str = "") -> str:
    """通过 adb reboot edl 进入 9008 EDL 模式。

    比 force_edl() 更轻量：不需要 QUTS 服务，不依赖 xPCAT，
    直接走 ADB 通道。设备需先在 ADB 模式（adb shell 可用）。

    Args:
        device_serial: ADB 设备序列号。空则不指定 -s（单设备场景）。

    Returns:
        结果描述。exit=255 视为正常（设备重启 ADB 连接断开）。
    """
    if device_serial:
        cmd = ["adb", "-s", device_serial, "shell", "reboot", "edl"]
    else:
        cmd = ["adb", "shell", "reboot", "edl"]

    logger.info("reboot_edl: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15, errors="replace",
        )
        # exit=255 正常：设备开始重启，ADB 连接断开
        if result.returncode in (0, 255):
            return f"✅ 已发送 reboot edl 指令，设备正在进入 9008 模式"
        return f"❌ reboot edl 失败 (exit={result.returncode}): {result.stderr or result.stdout}"
    except subprocess.TimeoutExpired:
        return "❌ adb reboot edl 超时（设备可能已在重启中）"
    except FileNotFoundError:
        return "❌ adb 未找到，请确认 adb 在 PATH 中"


def flash_build(
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
    """通过 xPCAT 执行高通整刷（Software Download）。

    build_path: 固件路径。
      - Meta build: 指向 contents.xml 文件（需 flavor）
      - Flat build: 指向 flat build 目录（不需 flavor）
    memory_type: 存储类型 UFS/EMMC/NAND/SPINOR，默认 UFS。
    flavor: 产品 flavor（asic/core_asic），meta build 必填，flat build 不需要。
    device_serial: 设备序列号。空则 xPCAT 自动选择（仅单设备时有效）。
    erase: 是否擦除整个 flash，默认 True。
    reset: 刷完是否重启设备，默认 True。
    force_edl_mode: 是否先强制进 EDL 模式，默认 False（设备已在 EDL 时跳过）。
    device_programmer: firehose programmer 文件路径（如 xbl_s_devprg_ns.melf）。
        指定后覆盖 xPCAT 默认的 programmer 选择，flat build 场景常需指定。
    rawprog: rawprogram XML 文件名，多个用分号分隔（如
        "rawprogram0_WIPE_PARTITIONS.xml;rawprogram0_split.xml"）。
        指定后只刷这些 XML 对应的分区，不指定则 xPCAT 自动选择。
    patchprog: patch XML 文件名（如 "patch0.xml"）。
    skip_sahara: 是否跳过 Sahara 握手，默认 False。设备已加载 programmer 时可跳过。
    timeout: 超时秒数，整刷很慢，默认 600s（10 分钟）。
    background: 后台模式，默认 True。整刷耗时 5-15 分钟，MCP stdio 有 30s 硬超时，
      阻塞调用会被杀掉导致刷机中断。后台模式用 Popen 起进程立即返回 task_id，
      用 flash_status(task_id) 轮询进度。background=False 仅用于极快操作（<20s）。
    """
    build_path = build_path.strip()
    if not build_path:
        return "❌ build_path 不能为空"
    if not os.path.exists(build_path):
        return f"❌ 路径不存在: {build_path}"

    # 判断 meta vs flat build
    is_meta = build_path.lower().endswith("contents.xml")
    if is_meta and not flavor:
        return "❌ Meta build（contents.xml）需要指定 flavor（如 asic / core_asic）"

    mem = memory_type.upper()
    if mem not in ("UFS", "EMMC", "NAND", "SPINOR"):
        return f"❌ 不支持的 memory_type: {mem}（可选 UFS/EMMC/NAND/SPINOR）"

    # 可选：先进 EDL
    if force_edl_mode:
        if not device_serial:
            return "❌ force_edl_mode=True 时必须指定 device_serial"
        edl_result = force_edl(device_serial)
        if edl_result.startswith("❌"):
            return edl_result

    # 构造刷机命令
    args = ["-PLUGIN", "SD", "-BUILD", build_path, "-MEMORYTYPE", mem]

    if device_serial:
        args += ["-DEVICE", device_serial]

    if is_meta and flavor:
        args += ["-FLAVOR", flavor]

    if device_programmer:
        args += ["-DEVICEPROG", device_programmer]

    if rawprog:
        args += ["-RAWPROG", rawprog]

    if patchprog:
        args += ["-PATCHPROG", patchprog]

    if skip_sahara:
        args += ["-SKIPSAHARA", "True"]

    args += ["-ERASE", "True" if erase else "False"]
    args += ["-RESET", "True" if reset else "False"]

    # 后台模式：先同步检查 QUTS + xPCAT 路径，再 Popen 起进程
    if background:
        exe = _xpcat_path()
        if not exe:
            return (
                "❌ xPCAT.exe 未找到。请通过以下方式之一解决:\n"
                "  1. 设置环境变量 XPCAT_PATH 指向 xPCAT.exe 完整路径\n"
                "  2. 确保 xPCAT 已安装（通常随 QPST 一起安装）\n"
                "  3. 将 xPCAT 的 bin 目录加入 PATH"
            )
        quts_ok, quts_msg = _ensure_quts_running()
        if not quts_ok:
            return f"❌ {quts_msg}"
        task = flash_tasks.submit_flash_task(exe, args, platform="qcom")
        return (
            f"⏳ 高通整刷已提交后台执行\n"
            f"  task_id: {task.task_id}\n"
            f"  pid: {task.pid}\n"
            f"  build: {os.path.basename(build_path)}\n"
            f"  用 flash_status(task_id=\"{task.task_id}\") 轮询进度\n"
            f"  用 flash_stop(task_id=\"{task.task_id}\") 终止刷机"
        )

    # 阻塞模式（仅极快操作用）
    logger.info("开始高通整刷(阻塞): %s", " ".join(args))
    out, err, ec = _run_xpcat(args, timeout=timeout)

    # 解析结果
    out_lower = out.lower()
    if ec == 0 and ("executed successfully" in out_lower
                    or ("download" in out_lower and "success" in out_lower)
                    or "complete" in out_lower):
        return f"✅ 整刷完成\n{_extract_summary(out)}"
    elif ec == 0:
        return f"✅ 整刷完成 (exit=0)\n{_extract_summary(out)}"
    else:
        return f"❌ 整刷失败 (exit={ec})\n{_extract_summary(out)}\n{err}"


def _extract_summary(output: str) -> str:
    """提取 xPCAT 输出的关键信息（进度行、错误行、结果行）。

    保留含进度关键词的行；成功时取最后几行（含结果+耗时），
    失败时取最后 20 行（含错误上下文）。
    """
    lines = output.splitlines()
    key_lines = []
    for line in lines:
        l = line.strip()
        if not l:
            continue
        ll = l.lower()
        if any(kw in ll for kw in _PROGRESS_KEYWORDS):
            key_lines.append(l)
    # 最多保留 50 行避免输出过长
    if len(key_lines) > 50:
        return "\n".join(key_lines[:20]) + "\n...\n" + "\n".join(key_lines[-30:])
    return "\n".join(key_lines) if key_lines else output.strip()


# 向后兼容：旧函数名保留为 _extract_summary 的别名
xpcat_output_summary = _extract_summary


def flash_single_image(
    image_path: str,
    device_programmer: str,
    memory_type: str = "UFS",
    device_serial: str = "",
    lun: int = 0,
    start_sector: int = 0,
    timeout: int = 300,
    background: bool = True,
) -> str:
    """刷写单个分区镜像（非整刷）。

    image_path: 镜像文件路径（如 boot.img）。
    device_programmer: firehose programmer 文件路径（prog_firehose_*.elf）。
    memory_type: 存储类型，默认 UFS。
    device_serial: 设备序列号，空则自动选择。
    lun: LUN 编号，默认 0。
    start_sector: 起始扇区，默认 0。
    timeout: 超时秒，默认 300s。
    background: 后台模式，默认 True。单分区刷写可能仍 >30s，后台模式避免 MCP 超时。
    """
    image_path = image_path.strip()
    if not image_path or not os.path.isfile(image_path):
        return f"❌ 镜像文件不存在: {image_path}"
    if not os.path.isfile(device_programmer):
        return f"❌ programmer 文件不存在: {device_programmer}"

    args = [
        "-PLUGIN", "SD",
        "-SENDIMAGE", image_path,
        "-DEVICEPROG", device_programmer,
        "-MEMORYTYPE", memory_type.upper(),
        "-LUN", str(lun),
        "-STARTSECTOR", str(start_sector),
    ]
    if device_serial:
        args += ["-DEVICE", device_serial]

    # 后台模式
    if background:
        exe = _xpcat_path()
        if not exe:
            return (
                "❌ xPCAT.exe 未找到。请通过以下方式之一解决:\n"
                "  1. 设置环境变量 XPCAT_PATH 指向 xPCAT.exe 完整路径\n"
                "  2. 确保 xPCAT 已安装（通常随 QPST 一起安装）\n"
                "  3. 将 xPCAT 的 bin 目录加入 PATH"
            )
        quts_ok, quts_msg = _ensure_quts_running()
        if not quts_ok:
            return f"❌ {quts_msg}"
        task = flash_tasks.submit_flash_task(exe, args, platform="qcom")
        return (
            f"⏳ 高通单分区刷写已提交后台执行\n"
            f"  task_id: {task.task_id}\n"
            f"  pid: {task.pid}\n"
            f"  image: {os.path.basename(image_path)}\n"
            f"  用 flash_status(task_id=\"{task.task_id}\") 轮询进度\n"
            f"  用 flash_stop(task_id=\"{task.task_id}\") 终止刷机"
        )

    # 阻塞模式
    out, err, ec = _run_xpcat(args, timeout=timeout)
    if ec == 0:
        return f"✅ 单分区刷写完成: {os.path.basename(image_path)}\n{xpcat_output_summary(out)}"
    return f"❌ 单分区刷写失败 (exit={ec})\n{xpcat_output_summary(out)}\n{err}"
