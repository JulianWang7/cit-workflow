"""后台任务系统 — 长命令 setsid nohup 后台化 + PID 跟踪 + 轮询。

抄自 YWAgent 的后台任务模式：
  1. base64 编码命令（避免 shell 转义问题）
  2. echo b64 | base64 -d > script && setsid nohup bash script > log 2>&1 & echo PID:$!
  3. 捕获 PID → 持久化到 JSON（跨进程/重启恢复）
  4. 轮询：ps -p <pid> + tail log + 读 exit_code stamp
  5. 停止：kill -TERM -<pid>（杀进程组）

解决 SSH 长编译阻塞问题：26 分钟的 mmm 不再占着 SSH 通道，
提交后立即返回 task_id，agent 自行轮询直到完成。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import re

from bugflow.core.config import _config_dir
from bugflow.core.ssh import SSHError, ssh_exec

logger = logging.getLogger("bugflow.core.tasks")

# Android 构建失败标记（与 compile.py _BUILD_FAIL_MARKERS 保持一致）
# 用于后台任务全日志 grep 判定，比泛搜 "FAILED" 精确
_BUILD_FAIL_MARKERS = ("make: ***", "ninja: build stopped", "FAILED:")


# ── 数据结构 ──────────────────────────────────────────────────
@dataclass
class BuildTask:
    """一个后台编译任务的完整状态。"""

    task_id: str          # "build_<ts>_<hex6>"
    server: str           # "252"
    command: str          # 完整编译命令（日志/重启用）
    pid: str              # 远程 PID
    log_file: str         # /tmp/bugflow_build_<id>.log
    script_file: str      # /tmp/bugflow_build_<id>.sh
    exit_code_file: str   # /tmp/bugflow_build_<id>.exit_code
    start_time: float     # epoch
    status: str = "running"  # running / completed / failed / stopped
    exit_code: int | None = None
    module: str = ""      # 供 agent 识别
    last_log_size: int = 0  # 上次轮询的 log 字节数（停滞检测用）
    code_root: str = ""   # 源码树根（并发互斥检测用）


# ── 模块级状态 ────────────────────────────────────────────────
_tasks: dict[str, BuildTask] = {}
_tasks_lock = threading.RLock()


def _tasks_file() -> Path:
    """任务持久化文件路径。"""
    return _config_dir() / "build_tasks.json"


# ── 持久化 ────────────────────────────────────────────────────
def _save_tasks() -> None:
    """原子写 JSON（tempfile + os.replace），防写一半崩溃。"""
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    with _tasks_lock:
        data = {tid: asdict(t) for tid, t in _tasks.items()}
    tmp = d / "build_tasks.json.tmp"
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
        logger.warning("build_tasks.json 损坏，忽略")
        return
    with _tasks_lock:
        for tid, d in data.items():
            _tasks[tid] = BuildTask(**{k: d.get(k) for k in BuildTask.__dataclass_fields__})
    # 重新探测非终态任务（可能进程已结束）
    for tid, task in list(_tasks.items()):
        if task.status == "running":
            try:
                check_task(tid)
            except Exception:
                logger.debug("启动探测任务 %s 失败", tid, exc_info=True)


# ── 核心函数 ──────────────────────────────────────────────────
def submit_task(server: str, command: str, module: str = "", code_root: str = "") -> BuildTask:
    """提交后台任务。

    流程：base64 编码命令 → 写远程脚本 → setsid nohup 后台运行 → 捕获 PID → 持久化。

    Args:
        server: SSH 服务器名
        command: 要后台执行的完整命令
        module: 模块名（供 agent 识别，可选）
        code_root: 源码树根（并发互斥检测用，可选）

    Returns:
        BuildTask（含 task_id 和 pid）

    Raises:
        RuntimeError: 同一 server 已有编译任务在跑（P1-5: 并发 make 会损坏 out/）
    """
    # P1-5: 并发编译互斥 — 同一 server+code_root 不能同时跑两个 make
    # Android out/ 目录是共享的，并发 make 会互相覆盖 .o 导致产物损坏
    # 锁内预占位：SSH 提交可达数秒，释放锁后再提交会有 TOCTOU 窗口
    task_id = f"build_{int(time.time())}_{os.urandom(3).hex()}"
    with _tasks_lock:
        for tid, existing in _tasks.items():
            if existing.server != server or existing.status != "running":
                continue
            # code_root 空时保守拦截（同 server 即拦截）
            if code_root and existing.code_root and existing.code_root != code_root:
                continue
            raise RuntimeError(
                f"已有编译任务在跑 (task_id={tid}, module={existing.module})，"
                f"请先 compile_status(task_id='{tid}') 轮询完成或 compile_stop(task_id='{tid}')"
            )
        # 预占位 running 任务，关闭并发窗口
        _tasks[task_id] = BuildTask(
            task_id=task_id,
            server=server,
            command=command,
            pid="",
            log_file="",
            script_file="",
            exit_code_file="",
            start_time=time.time(),
            module=module,
            code_root=code_root,
        )
    _save_tasks()

    def _abort(msg: str) -> RuntimeError:
        """SSH 提交失败：移除占位任务并抛错。"""
        with _tasks_lock:
            _tasks.pop(task_id, None)
        _save_tasks()
        return RuntimeError(msg)

    script_file = f"/tmp/bugflow_{task_id}.sh"
    log_file = f"/tmp/bugflow_{task_id}.log"
    exit_code_file = f"/tmp/bugflow_{task_id}.exit_code"

    # 脚本内容：跑命令 → 记 exit code stamp → 追加完成标记到 log
    # 这样 check_task 能从 exit_code_file 判定结果
    script_content = f"{command}\necho $? > {shlex.quote(exit_code_file)}\n"

    # base64 编码脚本（避免 shell 转义问题，抄 YWAgent）
    b64 = base64.b64encode(script_content.encode("utf-8")).decode("ascii")

    # 远程命令分两步，用换行分隔（不能用 && + & 组合 — shell 优先级坑：
    # `cmd1 && cmd2 & echo $!` 中 $! 捕到的是 cmd1 的子壳 PID，不是 cmd2）
    # 步骤1: base64 解码写脚本
    # 步骤2: setsid bash script 后台运行（setsid 起独立进程组，$! 捕到 setsid PID）
    remote_cmd = (
        f"echo {b64} | base64 -d > {shlex.quote(script_file)}\n"
        f"setsid bash {shlex.quote(script_file)} > {shlex.quote(log_file)} 2>&1 & "
        f"echo PID:$!"
    )

    try:
        out, err, ec = ssh_exec(server, remote_cmd, timeout=30, killable=False)
    except SSHError as e:
        raise _abort(f"提交后台任务失败（SSH）: {e}") from e

    if ec != 0:
        raise _abort(f"提交后台任务失败 (exit={ec}): {err.strip() or out.strip()[:200]}")

    # 解析 PID
    pid = ""
    for line in out.strip().splitlines():
        if line.startswith("PID:"):
            pid = line[4:].strip()
            break
    if not pid:
        raise _abort(f"未捕获到 PID: {out.strip()[:200]}")

    # 回填占位任务的 PID / 文件路径
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is not None:
            task.pid = pid
            task.log_file = log_file
            task.script_file = script_file
            task.exit_code_file = exit_code_file
    _save_tasks()
    logger.info("后台任务已提交: %s (pid=%s, module=%s)", task_id, pid, module)
    return _tasks[task_id]


def check_task(task_id: str) -> dict:
    """轮询任务状态。

    流程：ps -p <pid> 判存活 → 不存活则读 exit_code stamp → tail log。

    Returns:
        {task_id, status, exit_code, log_tail, elapsed, module}
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "status": "not_found", "exit_code": None,
                "log_tail": "", "elapsed": 0, "module": ""}

    # 已终态 → 直接返回缓存（不重复 SSH）
    if task.status in ("completed", "failed", "stopped"):
        return {
            "task_id": task_id,
            "status": task.status,
            "exit_code": task.exit_code,
            "log_tail": "",
            "elapsed": time.time() - task.start_time,
            "module": task.module,
        }

    # 占位任务（pid 为空 — SSH 提交中，TOCTOU 窗口）
    if not task.pid:
        return {
            "task_id": task_id,
            "status": "running",
            "exit_code": None,
            "log_tail": "(提交中…)",
            "elapsed": time.time() - task.start_time,
            "module": task.module,
            "stagnant": False,
            "recommended_interval": recommend_poll_interval(task.command),
        }

    # 查存活 + exit_code stamp + log 尾部（一次 SSH 搞定）
    # P1-4: exit_code_file 是进程结束的最终凭证 — 即使 kill -0 报 ALIVE（PID 被复用），
    #        只要 stamp 文件已写入就说明进程已正常退出
    # P1-6: 进程结束后全日志 grep 失败标记（tail -200 可能漏掉 FAILED 行）
    # P3-9: 记录 log 文件大小用于停滞检测
    fail_pattern = "|".join(re.escape(m) for m in _BUILD_FAIL_MARKERS)
    check_cmd = (
        # 1) exit_code stamp — 最终凭证，放最前
        f"EXITCODE=$(cat {shlex.quote(task.exit_code_file)} 2>/dev/null); "
        f"echo \"EXITCODE:${{EXITCODE:-NO_EXIT_FILE}}\"; "
        # 2) 进程存活探测（stamp 不存在时的 fallback）
        f"kill -0 {shlex.quote(task.pid)} 2>/dev/null && echo 'ALIVE:yes' || echo 'ALIVE:no'; "
        # 3) 全日志失败标记计数（P1-6: 比 tail-200 + 泛搜 "FAILED" 精确）
        f"FAILCOUNT=$(grep -cE '{fail_pattern}' {shlex.quote(task.log_file)} 2>/dev/null || echo 0); "
        f"echo \"FAILCOUNT:$FAILCOUNT\"; "
        # 4) log 文件大小（P3-9: 停滞检测用）
        f"LOGSIZE=$(wc -c < {shlex.quote(task.log_file)} 2>/dev/null || echo 0); "
        f"echo \"LOGSIZE:$LOGSIZE\"; "
        # 5) log 尾部（进度展示 + 失败上下文）
        f"tail -n 200 {shlex.quote(task.log_file)} 2>/dev/null"
    )
    try:
        out, _, _ = ssh_exec(task.server, check_cmd, timeout=15, killable=False)
    except SSHError as e:
        logger.warning("轮询任务 %s SSH 失败: %s", task_id, e)
        return {
            "task_id": task_id,
            "status": "running",
            "exit_code": None,
            "log_tail": f"(轮询失败: {e})",
            "elapsed": time.time() - task.start_time,
            "module": task.module,
            "stagnant": False,
            "recommended_interval": recommend_poll_interval(task.command),
        }

    # 标记式解析（比位置解析更鲁棒 — 某段输出为空不会错位）
    exit_code_str = "NO_EXIT_FILE"
    alive = False
    fail_count = 0
    log_size = 0
    log_lines: list[str] = []
    for line in out.splitlines():
        if line.startswith("EXITCODE:"):
            exit_code_str = line[9:].strip()
        elif line.startswith("ALIVE:"):
            alive = line[6:].strip() == "yes"
        elif line.startswith("FAILCOUNT:"):
            try:
                fail_count = int(line[10:].strip())
            except ValueError:
                pass
        elif line.startswith("LOGSIZE:"):
            try:
                log_size = int(line[8:].strip())
            except ValueError:
                pass
        else:
            log_lines.append(line)
    log_tail = "\n".join(log_lines) if log_lines else ""

    # 判定状态
    has_exit_stamp = exit_code_str and exit_code_str != "NO_EXIT_FILE"

    if has_exit_stamp:
        # P1-4: exit_code_file 是最终凭证 — 即使 kill -0 报 ALIVE（PID 复用），
        # stamp 文件已写入 → 进程已结束
        try:
            exit_code = int(exit_code_str)
        except ValueError:
            exit_code = -1
        # P1-6: 全日志 grep 失败标记（tail -200 可能漏掉 FAILED 行）
        if exit_code == 0 and fail_count == 0:
            status = "completed"
        else:
            status = "failed"
            if fail_count > 0:
                log_tail = f"⚠️ 全日志检测到 {fail_count} 处失败标记\n{log_tail}"
    elif alive:
        status = "running"
        exit_code = None
    else:
        # stamp 不存在 + 进程不存活 → 被杀或异常退出
        status = "failed"
        exit_code = -1

    # P3-9: 停滞检测 — log 字节数未增长且进程仍在跑 → 可能卡死
    stagnant = False
    if status == "running" and task.last_log_size > 0 and log_size == task.last_log_size:
        stagnant = True
        logger.info("任务 %s 日志停滞 (size=%d, 上一轮=%d)",
                    task_id, log_size, task.last_log_size)

    # 更新状态
    elapsed = time.time() - task.start_time
    with _tasks_lock:
        task.status = status
        task.exit_code = exit_code
        task.last_log_size = log_size
        _tasks[task_id] = task
    _save_tasks()

    return {
        "task_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "log_tail": log_tail,
        "elapsed": elapsed,
        "module": task.module,
        "stagnant": stagnant,
        "recommended_interval": recommend_poll_interval(task.command, elapsed, stagnant),
    }


def list_tasks() -> list[dict]:
    """列出所有任务摘要。"""
    with _tasks_lock:
        tasks = list(_tasks.values())
    now = time.time()
    return [
        {
            "task_id": t.task_id,
            "status": t.status,
            "module": t.module,
            "pid": t.pid,
            "elapsed": now - t.start_time if t.status == "running" else None,
        }
        for t in sorted(tasks, key=lambda x: x.start_time, reverse=True)
    ]


def stop_task(task_id: str) -> dict:
    """停止后台任务：kill -TERM -<pid>（杀进程组）→ 更新状态。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "status": "not_found", "stopped": False}

    if task.status in ("completed", "failed", "stopped"):
        return {"task_id": task_id, "status": task.status, "stopped": True,
                "message": "任务已结束"}

    # 占位任务（SSH 提交中，pid 为空）— 直接移除占位，无需 kill
    if not task.pid:
        with _tasks_lock:
            _tasks.pop(task_id, None)
        _save_tasks()
        return {"task_id": task_id, "status": "stopped", "stopped": True,
                "message": "占位任务已取消（SSH 提交阶段中止）"}

    # 杀进程组（TERM → sleep 1 → KILL 兜底）
    kill_cmd = (
        f"kill -TERM -{shlex.quote(task.pid)} 2>/dev/null; "
        f"kill -TERM {shlex.quote(task.pid)} 2>/dev/null; "
        f"sleep 1; "
        f"kill -KILL -{shlex.quote(task.pid)} 2>/dev/null; "
        f"kill -KILL {shlex.quote(task.pid)} 2>/dev/null; "
        f"echo DONE"
    )
    try:
        ssh_exec(task.server, kill_cmd, timeout=10, killable=False)
    except SSHError as e:
        logger.warning("停止任务 %s SSH 失败: %s", task_id, e)

    # 清理脚本文件
    try:
        ssh_exec(task.server, f"rm -f {shlex.quote(task.script_file)}", timeout=5, killable=False)
    except SSHError:
        pass

    with _tasks_lock:
        task.status = "stopped"
        task.exit_code = -2
        _tasks[task_id] = task
    _save_tasks()

    return {"task_id": task_id, "status": "stopped", "stopped": True}


def cleanup_task(task_id: str) -> None:
    """删除已终态的任务记录 + 远程临时文件。"""
    with _tasks_lock:
        task = _tasks.pop(task_id, None)
    if task is None:
        return
    # 清理远程临时文件
    rm_cmd = (
        f"rm -f {shlex.quote(task.script_file)} {shlex.quote(task.log_file)} "
        f"{shlex.quote(task.exit_code_file)} 2>/dev/null"
    )
    try:
        ssh_exec(task.server, rm_cmd, timeout=5, killable=False)
    except SSHError:
        pass
    _save_tasks()


# ── 轮询间隔建议 ──────────────────────────────────────────────
def recommend_poll_interval(command: str, elapsed: float = 0, stagnant: bool = False) -> int:
    """根据命令类型推荐轮询间隔。

    - make/mmm/编译 → 60s（长任务，频繁查浪费 SSH）
    - git → 20s
    - 其他 → 15s
    - stagnant（日志无变化）→ 额外 +15~30s
    """
    cmd_lower = command.lower()
    if any(k in cmd_lower for k in ("make", "mmm", "mma", "build")):
        base = 60
    elif "git" in cmd_lower:
        base = 20
    else:
        base = 15

    if stagnant:
        # 停滞越久间隔越长，但不超过 120s
        base = min(base + 30, 120)
    return base


# 模块加载时恢复持久化任务
_load_tasks()
