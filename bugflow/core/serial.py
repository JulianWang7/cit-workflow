# -*- coding: utf-8 -*-
"""串口通信层 — pyserial 封装。

ADB 断连时（深度休眠/USB 挂死）的生命线：串口是 UART 硬件直连，
不经过 USB 控制器，深度休眠/USB 挂死时仍然存活。

能力：
  - list_ports()        枚举本地 COM 口
  - serial_open()       打开串口（持久连接）
  - serial_send()       发命令 + 读回显
  - serial_read()       纯读 N 秒（抓内核日志）
  - serial_close()      关闭串口

设计要点：
  - 串口连接持久化（模块级 _serial_conn），open 后可反复 send/read
  - send 内部按超时读回显，避免死等
  - read 用循环读取 + 超时控制，能抓长时间日志
  - pyserial 未装时给出明确提示
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("bugflow.core.serial")

try:
    import serial as _pyserial
    import serial.tools.list_ports as _list_ports
except ImportError:
    _pyserial = None
    _list_ports = None


# ── 异常 ──────────────────────────────────────────────────────
class SerialError(Exception):
    """串口操作失败（未安装/未打开/超时/IO 错误）。"""


# ── 平台 → 串口速率预设表 ──────────────────────────────────────
# 各平台调试串口速率固定，AI 只需传 platform 名即可自动选速率。
# 扩展方式：直接在此表添加条目，或通过 servers.yaml 的 platform 字段覆盖。
BAUDRATE_MAP: dict[str, int] = {
    # Qualcomm 平台 — 调试串口 115200
    "SRM965": 115200,
    "SRM965_A16": 115200,
    "SRM963": 115200,
    "SRM965_LXD": 115200,
    # Unisoc 展锐平台 — 调试串口 921600
    "uis7870_3h10_car": 921600,
    "uis7870_3h10_car_native": 921600,
}


def get_baudrate_for_platform(platform: str) -> int:
    """查平台对应的串口速率。未知平台返回 115200 默认值。

    支持前缀模糊匹配：platform="uis7870_3h10_car_native" 能匹配
    BAUDRATE_MAP 中的 "uis7870_3h10_car" 键。
    """
    if not platform:
        return 115200
    # 精确匹配
    if platform in BAUDRATE_MAP:
        return BAUDRATE_MAP[platform]
    # 前缀模糊匹配（键是 platform 的前缀，或 platform 是键的前缀）
    for key, baud in BAUDRATE_MAP.items():
        if platform.startswith(key) or key.startswith(platform):
            return baud
    return 115200


# ── 持久连接 ───────────────────────────────────────────────────
_serial_conn = None  # type: _pyserial.Serial | None
_current_port: str = ""
_current_baud: int = 0
_serial_lock = threading.Lock()


def _require_pyserial() -> None:
    if _pyserial is None:
        raise SerialError(
            "pyserial 未安装，无法操作串口。请 `pip install pyserial`。"
        )


# ── 公开接口 ───────────────────────────────────────────────────

def list_ports() -> str:
    """枚举本地所有可用串口，返回格式化文本。

    返回示例：
      可用串口 (2):
        [0] COM3   - USB Serial Port (COM3)        [USB\VID_10C4&PID_EA60...]
        [1] COM5   - Silicon Labs CP210x USB to UART Bridge (COM5)
    """
    _require_pyserial()
    ports = list(_list_ports.comports())
    if not ports:
        return "未检测到可用串口。请检查串口线是否连接、驱动是否安装。"

    lines = [f"可用串口 ({len(ports)}):"]
    for i, p in enumerate(ports):
        desc = p.description or p.device
        hwid = p.hwid or ""
        lines.append(f"  [{i}] {p.device:<10} - {desc}  [{hwid}]")
    return "\n".join(lines)


# ── 串口自动检测 ──────────────────────────────────────────────
# 常见 USB-to-UART 调试转接芯片的识别特征。
# 匹配策略: 先 VID（厂商 ID，最可靠），再描述关键词。
# 展锐/高通开发板的调试串口几乎都用这几款芯片。
#
# VID 映射:
#   1A86  → WCH（CH340/CH341/CH343/CH9101/CH9102）
#   10C4  → Silicon Labs（CP210x）
#   0403  → FTDI（FT232/FT2232/FT4232）
#   067B  → Prolific（PL2303）
_DEBUG_ADAPTER_VIDS: list[str] = [
    "1a86",   # WCH
    "10c4",   # Silicon Labs
    "0403",   # FTDI
    "067b",   # Prolific
]

_DEBUG_ADAPTER_HINTS: list[str] = [
    "CP210",        # Silicon Labs CP210x
    "CH340",        # WCH CH340/CH341
    "CH341",
    "CH343",        # WCH CH343
    "CH9101",       # WCH CH9101（展锐开发板常用）
    "CH9102",
    "FT232",        # FTDI FT232R/FT232H
    "FTDI",
    "PL2303",       # Prolific PL2303
    "USB Serial",   # 通用 USB Serial Port 描述
]

# 排除关键词 — 这些是设备自身暴露的 modem/AT 控制口，非调试串口
_DEBUG_EXCLUDE_HINTS: list[str] = [
    "SPRD",         # 展锐 AT/LTE/WCN 控制口
    "ACPI",         # 主板内置串口
    "Bluetooth",
]


def detect_serial_port() -> tuple[str, str]:
    """自动检测调试串口，返回 (port_name, detection_note)。

    检测策略:
      1. 0 个串口 → 抛 SerialError
      2. 1 个串口 → 直接选
      3. 多个 → 按 VID/PID/描述过滤已知调试转接芯片（CP210x/CH340/FTDI/PL2303）
      4. 过滤后仍多个 → 选第一个并提示
      5. 过滤后无匹配 → 选第一个并警告（可能是非标准转接芯片）

    返回:
        (port_name, note) — note 说明选中原因，供调用方展示
    """
    _require_pyserial()
    ports = list(_list_ports.comports())
    if not ports:
        raise SerialError(
            "未检测到可用串口。请检查串口线是否连接、驱动是否安装。"
        )

    # 仅 1 个串口 → 直接选
    if len(ports) == 1:
        p = ports[0]
        return p.device, f"仅检测到 1 个串口，自动选择: {p.device} ({p.description})"

    # 多个 → 过滤已知调试转接芯片
    matched = []
    for p in ports:
        searchable = f"{p.description} {p.hwid}".lower()

        # 排除 modem/AT 控制口和主板串口
        if any(ex.lower() in searchable for ex in _DEBUG_EXCLUDE_HINTS):
            continue

        # VID 匹配（最可靠）— hwid 格式如 "USB VID:PID=1A86:55D8"
        vid_match = any(vid in searchable for vid in _DEBUG_ADAPTER_VIDS)
        # 描述关键词匹配
        hint_match = any(hint.lower() in searchable for hint in _DEBUG_ADAPTER_HINTS)

        if vid_match or hint_match:
            matched.append(p)

    if len(matched) == 1:
        p = matched[0]
        return p.device, f"从 {len(ports)} 个串口中匹配调试转接芯片: {p.device} ({p.description})"

    if len(matched) > 1:
        p = matched[0]
        others = ", ".join(m.device for m in matched[1:])
        return p.device, (
            f"匹配到 {len(matched)} 个调试转接芯片，选第一个: {p.device} ({p.description})。"
            f"其他: {others}"
        )

    # 无匹配 → 选第一个并警告
    p = ports[0]
    return p.device, (
        f"⚠️ 未匹配到已知调试转接芯片，选第一个串口: {p.device} ({p.description})。"
        f"如不正确，请用 list_ports 查看后手动指定。"
    )


def serial_open(
    port: str = "",
    baudrate: int = 0,
    platform: str = "",
    timeout: float = 1.0,
) -> str:
    """打开串口，建立持久连接。后续 send/read 复用此连接。

    port: 串口设备名（如 COM3 / /dev/ttyUSB0）。空=自动检测（见 detect_serial_port）。
    baudrate: 波特率。0=自动（从 platform 查 BAUDRATE_MAP），默认 0。
              手动指定（如 115200/921600/460800）时忽略 platform。
    platform: 平台名（如 SRM965, uis7870_3h10_car_native）。
              baudrate=0 时从此查速率。展锐=921600，高通=115200。
              也影响返回消息（显示匹配的平台速率）。
    timeout: 读超时秒，默认 1.0。影响 send 读回显的等待时间。

    返回成功消息。串口已打开时会先关闭旧的再开新的。
    """
    _require_pyserial()
    global _serial_conn, _current_port, _current_baud

    # port 空 → 自动检测串口
    auto_note = ""
    if not port:
        port, auto_note = detect_serial_port()

    # baudrate=0 → 从平台查表；无平台 → 115200 默认
    if baudrate <= 0:
        baudrate = get_baudrate_for_platform(platform)

    with _serial_lock:
        # 先关闭已有连接
        if _serial_conn is not None:
            try:
                _serial_conn.close()
            except Exception:
                pass
            _serial_conn = None

        try:
            _serial_conn = _pyserial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=_pyserial.EIGHTBITS,
                parity=_pyserial.PARITY_NONE,
                stopbits=_pyserial.STOPBITS_ONE,
                timeout=timeout,       # 读超时
                write_timeout=3.0,     # 写超时
            )
            _current_port = port
            _current_baud = baudrate
            logger.info("串口已打开: %s @ %d baud", port, baudrate)
            note_suffix = f"\n   {auto_note}" if auto_note else ""
            return f"✅ 串口已打开: {port} @ {baudrate} baud (timeout={timeout}s){note_suffix}"
        except Exception as e:
            _serial_conn = None
            _current_port = ""
            raise SerialError(f"打开串口失败 {port}: {e}") from e


def serial_send(command: str, wait: float = 2.0) -> str:
    """发送命令并读取回显，返回回显文本。

    command: 要发送的命令（如 "reboot"、"getprop ro.build.type"）。
    wait: 发送后读回显的等待秒数，默认 2.0。复杂命令可加大。

    需先 serial_open 打开串口。内部写入 command + \\r\\n，然后循环读直到超时。
    """
    _require_pyserial()
    with _serial_lock:
        if _serial_conn is None:
            raise SerialError("串口未打开，请先调用 serial(action='open', ...)")

        try:
            # 清空输入缓冲区，避免读到之前的残留
            _serial_conn.reset_input_buffer()

            # 写入命令（带换行）
            data = (command + "\r\n").encode("utf-8", errors="replace")
            _serial_conn.write(data)
            _serial_conn.flush()

            # 读回显
            echo = _read_for_duration(wait)
            logger.debug("serial_send(%s) → %d chars", command, len(echo))
            return echo if echo else "(无回显)"
        except _pyserial.SerialTimeoutException:
            raise SerialError(f"串口写入超时: {command}") from None
        except Exception as e:
            raise SerialError(f"串口发送失败: {e}") from e


def serial_read(duration: int = 10) -> str:
    """持续读取串口输出，返回累积文本。用于抓内核日志/休眠唤醒日志。

    duration: 读取持续秒数，默认 10。可设大值如 60 抓长时间日志。

    需先 serial_open 打开串口。纯读模式，不发送任何命令。
    """
    _require_pyserial()
    with _serial_lock:
        if _serial_conn is None:
            raise SerialError("串口未打开，请先调用 serial(action='open', ...)")
        return _read_for_duration(duration)


def serial_close() -> str:
    """关闭串口连接。"""
    global _serial_conn, _current_port, _current_baud
    _require_pyserial()

    with _serial_lock:
        if _serial_conn is None:
            return "串口未打开，无需关闭"

        port = _current_port
        try:
            _serial_conn.close()
        except Exception:
            pass
        _serial_conn = None
        _current_port = ""
        _current_baud = 0
    logger.info("串口已关闭: %s", port)
    return f"✅ 串口已关闭: {port}"


def is_open() -> bool:
    """串口是否已打开。"""
    with _serial_lock:
        return _serial_conn is not None and _serial_conn.is_open


# ── 内部 ───────────────────────────────────────────────────────

def _read_for_duration(duration: float) -> str:
    """循环读取串口数据，持续 duration 秒，返回累积文本。"""
    if _serial_conn is None:
        return ""

    chunks: list[str] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        # in_waiting 返回缓冲区字节数
        n = _serial_conn.in_waiting
        if n > 0:
            raw = _serial_conn.read(n)
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("latin-1", errors="replace")
            chunks.append(text)
        else:
            # 缓冲区空，短暂 sleep 避免空转
            time.sleep(0.05)

    return "".join(chunks)
