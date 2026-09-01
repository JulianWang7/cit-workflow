# -*- coding: utf-8 -*-
"""Fastboot 设备操作层 — subprocess fastboot 封装。

与 core/adb.py 同构。ADB 断连或设备需要刷分区时使用。

能力：
  - fastboot_devices()           列出 fastboot 模式设备
  - fastboot_flash(partition, image)  刷分区
  - fastboot_reboot(target)      重启到 bootloader/system/recovery
  - fastboot_getvar(var)         查询设备变量
  - fastboot_oem(cmd)            OEM 命令
  - fastboot_erase(partition)    擦除分区

设计要点：
  - fastboot_run(args, timeout) 跟 adb_run 同构
  - fastboot.exe 路径自动发现：ANDROID_HOME/platform-tools > PATH > 常见路径
  - 串口/ADB 都断了时，fastboot 是唯一刷机通道
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("bugflow.core.fastboot")


# ── 异常 ──────────────────────────────────────────────────────
class FastbootError(Exception):
    """Fastboot 操作失败（超时/未找到/非零退出）。"""


# ── fastboot 路径发现 ──────────────────────────────────────────
_fastboot_cache: str | None = None


def _find_fastboot() -> str:
    """动态发现 fastboot 可执行文件路径。结果缓存。

    优先级：
      1. ANDROID_HOME 环境变量下的 platform-tools/fastboot(.exe)
      2. PATH 中的 fastboot（where/which）
      3. 常见安装路径扫描
    """
    global _fastboot_cache
    if _fastboot_cache and os.path.isfile(_fastboot_cache):
        return _fastboot_cache

    exe = "fastboot.exe" if os.name == "nt" else "fastboot"

    # 1. ANDROID_HOME
    android_home = os.environ.get("ANDROID_HOME", "").strip()
    if android_home:
        candidate = os.path.join(android_home, "platform-tools", exe)
        if os.path.isfile(candidate):
            _fastboot_cache = candidate
            logger.info("fastboot found via ANDROID_HOME: %s", candidate)
            return candidate

    # 2. PATH
    found = shutil.which("fastboot")
    if found:
        _fastboot_cache = found
        logger.info("fastboot found in PATH: %s", found)
        return found

    # 3. 常见安装路径（Windows）
    if os.name == "nt":
        _candidates = [
            r"C:\Users\{}\AppData\Local\Android\Sdk\platform-tools\fastboot.exe".format(
                os.environ.get("USERNAME", "")
            ),
            r"C:\Android\Sdk\platform-tools\fastboot.exe",
            r"D:\Android\Sdk\platform-tools\fastboot.exe",
            r"C:\platform-tools\fastboot.exe",
            r"D:\platform-tools\fastboot.exe",
        ]
        for c in _candidates:
            if os.path.isfile(c):
                _fastboot_cache = c
                logger.info("fastboot found at: %s", c)
                return c

    raise FastbootError(
        "fastboot 未找到 — 请确保 Android SDK platform-tools 已安装。\n"
        "方法 1: 下载 platform-tools 并将其加入 PATH。\n"
        "方法 2: 设置 ANDROID_HOME 环境变量指向 SDK 根目录。\n"
        "下载地址: https://developer.android.com/tools/releases/platform-tools"
    )


# ── 底层执行 ───────────────────────────────────────────────────

def fastboot_run(
    args: list[str],
    serial: str = "",
    timeout: int = 120,
) -> tuple[str, str, int]:
    """跑一条 fastboot 命令，返回 (stdout, stderr, exit_code)。

    非零退出抛 FastbootError（带可读建议）。
    """
    exe = _find_fastboot()
    cmd = [exe]
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
            if "no devices" in low or "waiting for device" in low:
                err_msg += "\n建议: 设备未进入 fastboot 模式。adb reboot bootloader 进入后重试。"
            elif "unlocked" in low:
                err_msg += "\n建议: 设备未解锁 bootloader。fastboot flashing unlock 解锁后再刷。"
            raise FastbootError(f"Fastboot 命令失败: {err_msg}")
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        raise FastbootError(
            f"Fastboot 命令超时 ({timeout}s)。建议: 检查设备是否响应或增大 timeout。"
        ) from None
    except FileNotFoundError:
        raise FastbootError(
            "fastboot 未找到 — 请确保 platform-tools 在 PATH 中。"
        ) from None


# ── 公开接口 ───────────────────────────────────────────────────

def fastboot_devices() -> str:
    """列出所有处于 fastboot 模式的设备。

    返回格式化文本。无设备时给出提示。
    """
    try:
        out, _, _ = fastboot_run(["devices"], timeout=10)
        lines = [l for l in out.strip().splitlines() if l.strip()]
        if not lines:
            return (
                "未检测到 fastboot 设备。\n"
                "提示: adb reboot bootloader 可让设备进入 fastboot 模式。"
            )
        result = f"Fastboot 设备 ({len(lines)}):\n"
        for i, line in enumerate(lines):
            result += f"\n  [{i}] {line.strip()}"
        return result
    except FastbootError:
        raise


def fastboot_flash(
    partition: str,
    image: str,
    serial: str = "",
    reboot: bool = False,
    timeout: int = 300,
) -> str:
    """刷分区镜像。

    partition: 分区名（如 boot, vendor, system, recovery, vbmeta）。
    image: 镜像文件本地路径（如 C:\\builds\\vendor.img）。
    serial: 设备序列号，空则自动选择（仅单设备有效）。
    reboot: 刷完是否重启，默认 False。True 则执行 fastboot reboot。
    timeout: 超时秒，大分区镜像可加大，默认 300。

    返回成功消息。
    """
    if not partition:
        raise FastbootError("partition 不能为空")
    if not image or not os.path.isfile(image):
        raise FastbootError(f"镜像文件不存在: {image}")

    out, _, _ = fastboot_run(
        ["flash", partition, image], serial=serial, timeout=timeout,
    )
    result = f"✅ 分区 {partition} 已刷写: {image}"
    if out.strip():
        result += f"\n{out.strip()}"

    if reboot:
        try:
            fastboot_run(["reboot"], serial=serial, timeout=15)
            result += "\n设备已重启。"
        except FastbootError as e:
            result += f"\n⚠️ 重启失败: {e}"

    logger.info("flash %s ← %s (reboot=%s)", partition, image, reboot)
    return result


def fastboot_reboot(
    target: str = "",
    serial: str = "",
    timeout: int = 30,
) -> str:
    """重启设备到指定模式。

    target: 重启目标，空或 'system' 正常重启；'bootloader' 进 fastboot；
            'recovery' 进恢复模式；'fastboot' 进 fastbootd（用户空间 fastboot）。
    serial: 设备序列号，空则自动选择。
    timeout: 超时秒，默认 30。
    """
    args = ["reboot"]
    if target and target != "system":
        args.append(target)

    out, _, _ = fastboot_run(args, serial=serial, timeout=timeout)
    target_desc = target or "system"
    return f"✅ 设备重启到 {target_desc} 模式"


def fastboot_getvar(
    var: str,
    serial: str = "",
    timeout: int = 10,
) -> str:
    """查询 fastboot 变量。

    var: 变量名（如 unlocked, product-name, version-bootloader, slot-suffix, partition-type:vendor）。
    serial: 设备序列号，空则自动选择。

    返回变量值。查询全部变量用 var='all'。
    """
    if not var:
        raise FastbootError("var 不能为空")

    args = ["getvar", var]
    # getvar 的输出在 stderr 里（fastboot 设计如此）
    try:
        exe = _find_fastboot()
        cmd = [exe]
        if serial:
            cmd += ["-s", serial]
        cmd += args
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )
        # fastboot getvar 正常输出在 stderr
        out = result.stderr.strip() or result.stdout.strip()
        if not out:
            return f"(空) 变量 {var} 无输出"
        return out
    except subprocess.TimeoutExpired:
        raise FastbootError(f"getvar 超时 ({timeout}s)") from None


def fastboot_oem(
    oem_cmd: str,
    serial: str = "",
    timeout: int = 30,
) -> str:
    """执行 OEM 命令。

    oem_cmd: OEM 命令内容（如 device-info, unlock, lock, reboot-recovery）。
    serial: 设备序列号，空则自动选择。
    timeout: 超时秒，默认 30。

    返回命令输出。
    """
    if not oem_cmd:
        raise FastbootError("oem_cmd 不能为空")

    out, _, _ = fastboot_run(
        ["oem", oem_cmd], serial=serial, timeout=timeout,
    )
    return out.strip() or f"(无输出) oem {oem_cmd}"


def fastboot_erase(
    partition: str,
    serial: str = "",
    timeout: int = 60,
) -> str:
    """擦除分区。谨慎使用。

    partition: 分区名（如 userdata, cache）。
    serial: 设备序列号，空则自动选择。
    timeout: 超时秒，默认 60。
    """
    if not partition:
        raise FastbootError("partition 不能为空")

    out, _, _ = fastboot_run(
        ["erase", partition], serial=serial, timeout=timeout,
    )
    return f"✅ 分区 {partition} 已擦除"
