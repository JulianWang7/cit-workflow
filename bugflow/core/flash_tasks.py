# -*- coding: utf-8 -*-
"""本地刷机后台任务管理 — 脱离 MCP 30s 超时限制。

展锐 CmdDloader.exe 和高通 xPCAT.exe 都是本地 Windows 进程，整刷耗时 3-10 分钟。
MCP stdio 通信层有 30s 硬超时，阻塞调用会被杀掉导致刷机中断。

本模块提供与 tasks.py（编译后台任务）类似的 submit/check/stop 模式，
但针对本地 subprocess 而非远程 SSH 进程：

  1. submit_flash_task(exe, args) → Popen 起进程，立即返回 task_id
  2. check_flash_task(task_id)    → poll() 判存活 + tail log
  3. stop_flash_task(task_id)     → kill 进程
  4. list_flash_tasks()           → 列全部任务

任务状态持久化到 flash_tasks.json，MCP server 重启后可恢复（重新探测进程存活）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from bugflow.core.config import _config_dir

logger = logging.getLogger(__name__)

# 刷机失败/成功标记（全日志 grep，比 tail 4KB 精确）
_FLASH_FAIL_MARKERS = ("fail", "error", "超时", "timeout", "aborted", "abort")
_FLASH_SUCCESS_MARKERS = ("success", "complete", "done", "完成", "executed successfully")


@dataclass
class FlashTask:
    """一个本地刷机后台任务的状态。"""

    task_id: str            # "flash_<ts>_<hex6>"
    exe: str                # CmdDloader.exe / xPCAT.exe 完整路径
    args_json: str          # JSON 编码的命令行参数列表（日志/重启用）
    pid: int                # 本地 OS 进程 PID
    log_file: str           # 本地日志文件路径
    platform: str           # "spd" / "qcom"（供 agent 识别）
    start_time: float       # epoch
    status: str = "running"  # running / completed / failed / stopped
    exit_code: int | None = None
    last_log_size: int = 0   # 上次轮询的 log 字节数


# ── 模块级状态 ────────────────────────────────────────────────
_tasks: dict[str, FlashTask] = {}
_tasks_lock = threading.RLock()

# 活跃 Popen 对象（进程重启后丢失，用 PID 检测兜底）
_procs: dict[str, subprocess.Popen] = {}
_procs_lock = threading.Lock()


def _tasks_file() -> Path:
    """任务持久化文件路径。"""
    return _config_dir() / "flash_tasks.json"


def _save_tasks() -> None:
    """原子写 JSON。"""
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    with _tasks_lock:
        data = {tid: asdict(t) for tid, t in _tasks.items()}
    tmp = d / "flash_tasks.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _tasks_file())


def _load_tasks() -> None:
    """启动时加载持久化任务，重新探测非终态任务。"""
    p = _tasks_file()
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("flash_tasks.json 损坏，忽略")
        return
    with _tasks_lock:
        for tid, d in data.items():
            _tasks[tid] = FlashTask(**{k: d.get(k) for k in FlashTask.__dataclass_fields__})
    # 重新探测非终态任务
    for tid, task in list(_tasks.items()):
        if task.status == "running":
            try:
                check_flash_task(tid)
            except Exception:
                logger.debug("启动探测刷机任务 %s 失败", tid, exc_info=True)


# ── 核心函数 ──────────────────────────────────────────────────
def submit_flash_task(exe: str, args: list[str], platform: str = "spd") -> FlashTask:
    """提交本地刷机后台任务。

    用 subprocess.Popen 起进程，stdout/stderr 重定向到日志文件，
    立即返回 task_id + pid，不阻塞。

    Args:
        exe: 可执行文件完整路径（CmdDloader.exe / xPCAT.exe）
        args: 命令行参数列表（不含 exe 本身）
        platform: "spd" / "qcom"（供 agent 识别）

    Returns:
        FlashTask（含 task_id 和 pid）

    Raises:
        RuntimeError: 已有刷机任务在执行（并发刷机可能损坏设备）
    """
    # 并发互斥 — 刷机工具独占 COM/USB 端口，并发会损坏设备
    with _tasks_lock:
        for tid, existing in _tasks.items():
            if existing.status == "running":
                raise RuntimeError(
                    f"已有刷机任务在执行 (task_id={tid}, platform={existing.platform})，"
                    f"请先 flash_status(task_id='{tid}') 轮询完成或 flash_stop(task_id='{tid}')"
                )

    task_id = f"flash_{int(time.time())}_{os.urandom(3).hex()}"
    log_dir = _config_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / f"{task_id}.log")

    cmd = [exe] + args
    logger.info("提交刷机后台任务: %s", " ".join(cmd))

    try:
        with open(log_file, "w", encoding="utf-8", errors="replace") as log_fh:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
    except Exception as e:
        raise RuntimeError(f"启动刷机进程失败: {e}") from e

    task = FlashTask(
        task_id=task_id,
        exe=exe,
        args_json=json.dumps(args, ensure_ascii=False),
        pid=proc.pid,
        log_file=log_file,
        platform=platform,
        start_time=time.time(),
    )
    with _tasks_lock:
        _tasks[task_id] = task
    with _procs_lock:
        _procs[task_id] = proc
    _save_tasks()
    logger.info("刷机后台任务已提交: %s (pid=%s, platform=%s)", task_id, proc.pid, platform)
    return task


def check_flash_task(task_id: str) -> dict:
    """轮询刷机任务状态。

    流程：psutil-free 方式判存活（os.kill(pid, 0) on Unix / OpenProcess on Windows）
    → 不存活则读 returncode → tail log。

    Returns:
        {task_id, status, exit_code, log_tail, elapsed, platform}
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "status": "not_found", "exit_code": None,
                "log_tail": "", "elapsed": 0, "platform": ""}

    # 已终态 → 直接返回缓存
    if task.status in ("completed", "failed", "stopped"):
        return {
            "task_id": task_id,
            "status": task.status,
            "exit_code": task.exit_code,
            "log_tail": "",
            "elapsed": time.time() - task.start_time,
            "platform": task.platform,
        }

    # 判进程状态 + 读日志（完整日志用于 grep，尾部用于展示）
    log_full = ""
    log_tail = ""
    try:
        with open(task.log_file, "r", encoding="utf-8", errors="replace") as f:
            log_full = f.read()
        log_tail = log_full[-4096:] if len(log_full) > 4096 else log_full
    except OSError:
        pass

    # 优先用 Popen 对象获取真实 returncode
    with _procs_lock:
        proc = _procs.get(task_id)
    if proc is not None:
        rc = proc.poll()
        if rc is not None:
            # 进程已结束 — 有真实 returncode（不再靠日志猜）
            exit_code = rc
            log_lower = log_full.lower()
            has_fail_marker = any(kw in log_lower for kw in _FLASH_FAIL_MARKERS)
            if exit_code == 0 and not has_fail_marker:
                status = "completed"
            else:
                status = "failed"
                if has_fail_marker:
                    log_tail = f"⚠️ 全日志检测到失败标记 (exit={exit_code})\n{log_tail}"
            # 清理 Popen 引用
            with _procs_lock:
                _procs.pop(task_id, None)
        else:
            status = "running"
            exit_code = None
    else:
        # Popen 不可用（进程重启后）— PID 存活检测 + 全日志 grep 兜底
        alive = _is_pid_alive(task.pid)
        if alive:
            status = "running"
            exit_code = None
        else:
            # 进程已结束但无 returncode — 全日志 grep 判断（比旧 tail 4KB 精确）
            log_lower = log_full.lower()
            has_fail = any(kw in log_lower for kw in _FLASH_FAIL_MARKERS)
            has_success = any(kw in log_lower for kw in _FLASH_SUCCESS_MARKERS)
            if has_fail:
                status = "failed"
                exit_code = 1
            elif has_success:
                status = "completed"
                exit_code = 0
            else:
                # 无任何标记 — 保守标记 failed
                status = "failed"
                exit_code = -1

    # 更新状态
    with _tasks_lock:
        task.status = status
        task.exit_code = exit_code
        task.last_log_size = len(log_tail)
        _tasks[task_id] = task
    if status != "running":
        _save_tasks()

    return {
        "task_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "log_tail": log_tail,
        "elapsed": time.time() - task.start_time,
        "platform": task.platform,
    }


def stop_flash_task(task_id: str) -> dict:
    """停止刷机任务（kill 进程）。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "status": "not_found", "exit_code": None}

    if task.status in ("completed", "failed", "stopped"):
        return {
            "task_id": task_id,
            "status": task.status,
            "exit_code": task.exit_code,
            "pid": task.pid,
            "message": "任务已结束，无需停止",
        }

    # kill 进程
    _kill_pid(task.pid)
    time.sleep(1)

    # 清理 Popen 引用
    with _procs_lock:
        proc = _procs.pop(task_id, None)
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass

    with _tasks_lock:
        task.status = "stopped"
        task.exit_code = -1
        _tasks[task_id] = task
    _save_tasks()

    return {"task_id": task_id, "status": "stopped", "exit_code": -1,
            "pid": task.pid, "message": "已终止刷机进程"}


def list_flash_tasks() -> list[dict]:
    """列出所有刷机任务。"""
    with _tasks_lock:
        tasks = list(_tasks.values())
    now = time.time()
    return [
        {
            "task_id": t.task_id,
            "platform": t.platform,
            "status": t.status,
            "pid": t.pid,
            "elapsed": int(now - t.start_time),
            "exe": os.path.basename(t.exe),
        }
        for t in sorted(tasks, key=lambda x: x.start_time, reverse=True)
    ]


# ── 辅助函数 ──────────────────────────────────────────────────
def _is_pid_alive(pid: int) -> bool:
    """检查进程是否存活（跨平台）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows: OpenProcess
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            # Unix: signal 0
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def _kill_pid(pid: int) -> None:
    """终止进程（跨平台）。"""
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, 9)
    except Exception as e:
        logger.warning("终止进程 %s 失败: %s", pid, e)


# 启动时加载持久化任务
_load_tasks()
