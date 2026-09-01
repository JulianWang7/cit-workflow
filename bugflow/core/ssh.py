"""SSH 执行层 — paramiko 连接池 + exec + 超时杀进程组。

抄自 YWAgent tools/ssh_tools.py 的 8 个踩坑经验（自包含，不依赖 YWAgent）：

  1. paramiko SSHClient.exec_command 非并发安全 → 每服务器一把 RLock 串行化
  2. exec channel 泄漏 → 成功/超时/异常所有路径都 close stdin/stdout/stderr
  3. 健康检查用 transport.is_active()（不开 channel、无网络往返），
     不用 exec_command("echo ok")（会干扰 agent 的 exec channel、瞬时抖动误判）
  4. 超时 kill 用 setsid + pid_file + kill -TERM -PID（杀进程组，否则 make 子进程杀不掉）
  5. 自由文本拼 SSH 命令前 shlex.quote（防注入）
  6. 连接池 dict + 熔断器（5 次连续失败→30s 冷却）+ 指数退避重试（3 次 1/2/4s）
  7. transport.set_keepalive(30)（防半开）
  8. 接口：get_connection / ssh_exec / ssh_disconnect_all

所有逻辑在连接池层，MCP server 和 orchestrator 共享。
"""

from __future__ import annotations

import logging
import os
import shlex
import socket
import tempfile
import threading
import time
import uuid
from typing import Optional

try:
    import paramiko

    _HAS_PARAMIKO = True
except ImportError:  # pragma: no cover - 环境缺依赖
    paramiko = None  # type: ignore[assignment]
    _HAS_PARAMIKO = False

from bugflow.core.config import load_server_config

logger = logging.getLogger("bugflow.core.ssh")


# ── 异常 ──────────────────────────────────────────────────────
class SSHError(Exception):
    """SSH 操作基类。"""


class SSHConnectionError(SSHError):
    """连接失败/不可用（含熔断冷却）。"""


class SSHCommandTimeout(SSHError):
    """命令超时（已尽力 kill 远程进程组）。"""


class ParamikoUnavailable(SSHError):
    """本机未装 paramiko。"""


def _require_paramiko() -> None:
    """确认 paramiko 可用。

    热检测：如果进程启动时 paramiko 未装（_HAS_PARAMIKO=False），
    每次调用时重新尝试 import — pip install 后无需重启进程即可生效。
    正常路径（已 True）只读一个 bool，零开销。
    """
    global _HAS_PARAMIKO, paramiko
    if _HAS_PARAMIKO:
        return  # 快路径：已确认可用
    # 慢路径：进程启动后可能已安装 paramiko，重新检测
    import importlib

    try:
        paramiko = importlib.import_module("paramiko")
        _HAS_PARAMIKO = True
        logger.info("paramiko 热检测成功（进程启动后安装的），SSH 工具恢复可用")
        return
    except ImportError:
        pass
    raise ParamikoUnavailable(
        "本机未安装 paramiko，SSH 工具不可用。请运行 `pip install paramiko` — "
        "安装后无需重启，下次调用会自动热检测恢复。"
    )


# ── 连接池常量 ─────────────────────────────────────────────────
CONN_MAX_FAILURES = 5     # 熔断：连续失败 N 次后
CONN_COOLDOWN_SEC = 30    # 熔断冷却秒数
CONN_HEALTH_INTERVAL = 60  # 距上次健康检查 >N 秒则预检
CONN_KEEPALIVE_SEC = 30   # TCP keepalive 间隔

# 踩坑 1+6：连接池 dicts + 一把池锁保护全部；exec 每服务器一把锁串行化
_connections: dict[str, "paramiko.SSHClient"] = {}
_conn_times: dict[str, float] = {}
_conn_failures: dict[str, int] = {}
_conn_cooldown: dict[str, float] = {}
_pool_lock = threading.RLock()
_ssh_exec_locks: dict[str, threading.RLock] = {}
_ssh_exec_locks_guard = threading.Lock()


def _server_exec_lock(server_name: str) -> threading.RLock:
    """每服务器一把锁：paramiko SSHClient.exec_command 非并发安全。"""
    with _ssh_exec_locks_guard:
        lock = _ssh_exec_locks.get(server_name)
        if lock is None:
            lock = threading.RLock()
            _ssh_exec_locks[server_name] = lock
        return lock


# ── 连接获取 ───────────────────────────────────────────────────
def _connect_new(server_name: str) -> "paramiko.SSHClient":
    """创建新连接。支持 key_file / password 双认证 + keepalive（踩坑 7）。"""
    _require_paramiko()
    try:
        srv = load_server_config(server_name)
    except ValueError as e:
        raise SSHConnectionError(str(e)) from e

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_file = srv.get("key_file", "")
    key_file = os.path.expanduser(key_file) if key_file else ""

    connect_kwargs: dict = {
        "hostname": srv["host"],
        "port": srv.get("port", 22),
        "username": srv["user"],
        "timeout": 10,
        "banner_timeout": 10,
    }

    # key 认证（优先）
    if key_file and os.path.isfile(key_file):
        for key_class in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
            try:
                connect_kwargs["pkey"] = key_class.from_private_key_file(key_file)
                break
            except (paramiko.SSHException, ValueError, OSError):
                continue
        else:
            connect_kwargs["key_filename"] = key_file

    # password 兜底
    password = srv.get("password", "")
    if password and "pkey" not in connect_kwargs and "key_filename" not in connect_kwargs:
        connect_kwargs["password"] = password

    try:
        client.connect(**connect_kwargs)
    except (socket.timeout, paramiko.SSHException, OSError, socket.gaierror) as e:
        try:
            client.close()
        except Exception:
            pass
        raise SSHConnectionError(f"无法连接到 {server_name}: {e}") from e

    # 踩坑 7：keepalive 防半开
    try:
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(CONN_KEEPALIVE_SEC)
    except Exception:
        pass  # best-effort

    return client


def get_connection(server_name: str) -> "paramiko.SSHClient":
    """取/建连接。含熔断器 + 健康预检 + 指数退避重试（踩坑 3+6）。

    - 熔断：连续失败 CONN_MAX_FAILURES 次 → 冷却 CONN_COOLDOWN_SEC
    - 健康预检：距上次检查 >CONN_HEALTH_INTERVAL 则 is_active() 探测
    - 重试：连接失败指数退避 3 次（1/2/4s）
    """
    _require_paramiko()
    now = time.time()

    # 熔断检查
    with _pool_lock:
        cooldown_until = _conn_cooldown.get(server_name, 0)
    if now < cooldown_until:
        remaining = int(cooldown_until - now)
        raise SSHConnectionError(
            f"SSH 连接暂时不可用 [{server_name}] — 连续失败过多，请在 {remaining}s 后重试"
        )

    # 健康预检（踩坑 3：is_active 不开 channel）
    with _pool_lock:
        client_ref = _connections.get(server_name)
        age = now - _conn_times.get(server_name, 0)
    if client_ref is not None:
        try:
            if age > CONN_HEALTH_INTERVAL:
                transport = client_ref.get_transport()
                if transport is None or not transport.is_active():
                    raise OSError("SSH transport not active")
            with _pool_lock:
                _conn_times[server_name] = now
                _conn_failures[server_name] = 0
            return client_ref
        except (socket.timeout, EOFError, paramiko.SSHException, OSError) as e:
            logger.warning("SSH 健康检查失败 %s (%s)，重连", server_name, type(e).__name__)
            try:
                client_ref.close()
            except (OSError, paramiko.SSHException):
                pass
            with _pool_lock:
                _connections.pop(server_name, None)

    # 连接 + 指数退避重试
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            client = _connect_new(server_name)
            with _pool_lock:
                _connections[server_name] = client
                _conn_times[server_name] = now
                _conn_failures[server_name] = 0
            return client
        except (
            socket.timeout,
            paramiko.SSHException,
            OSError,
            socket.gaierror,
            SSHConnectionError,
        ) as e:
            last_error = e
            if attempt < 2:
                delay = 2 ** attempt  # 1, 2, 4
                logger.warning(
                    "SSH 连接 %s 第 %d/3 次失败: %s，%ds 后重试", server_name, attempt + 1, e, delay
                )
                time.sleep(delay)

    # 重试耗尽 → 计入熔断
    with _pool_lock:
        _conn_failures[server_name] = _conn_failures.get(server_name, 0) + 1
        if _conn_failures[server_name] >= CONN_MAX_FAILURES:
            _conn_cooldown[server_name] = now + CONN_COOLDOWN_SEC
            logger.error(
                "SSH 熔断开启 %s（连续 %d 次失败），冷却 %ds",
                server_name, _conn_failures[server_name], CONN_COOLDOWN_SEC,
            )

    raise SSHConnectionError(f"无法连接到 {server_name}（重试 3 次后）: {last_error}") from last_error


def ssh_disconnect_all() -> None:
    """关闭池中所有连接（测试/退出用）。"""
    with _pool_lock:
        names = list(_connections.keys())
    for name in names:
        with _pool_lock:
            client = _connections.pop(name, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


# ── 超时杀进程组（踩坑 4）─────────────────────────────────────
def _kill_remote_pid(server_name: str, pid_file: str) -> None:
    """best-effort 杀远端进程组：kill -TERM -PID（进程组）再 kill -KILL 兜底。"""
    if not pid_file:
        return
    kill_cmd = (
        f"pid=$(cat {shlex.quote(pid_file)} 2>/dev/null); "
        f'if [ -n "$pid" ]; then '
        f"kill -TERM -$pid 2>/dev/null; kill -TERM $pid 2>/dev/null; "
        f"sleep 1; "
        f"kill -KILL -$pid 2>/dev/null; kill -KILL $pid 2>/dev/null; "
        f"fi; rm -f {shlex.quote(pid_file)}"
    )
    try:
        client = get_connection(server_name)
        ki, ko, ke = client.exec_command(kill_cmd, timeout=10)
        try:
            ko.read()
            ke.read()
        except Exception:
            pass
        # 踩坑 2：所有路径关 channel 文件
        for s in (ki, ko, ke):
            try:
                s.close()
            except Exception:
                pass
    except Exception:
        logger.debug("杀远端 pid 失败 %s (%s)", server_name, pid_file, exc_info=True)


# ── 命令执行 ───────────────────────────────────────────────────
def ssh_exec(
    server_name: str,
    command: str,
    timeout: int = 60,
    *,
    killable: bool = True,
) -> tuple[str, str, int]:
    """在服务器上执行命令，返回 (stdout, stderr, exit_code)。

    - 每服务器串行化（踩坑 1：paramiko exec 非并发安全）
    - killable=True 时用 setsid + pid_file 包裹（踩坑 4：超时杀进程组）
    - 所有路径关 channel 文件（踩坑 2：防泄漏）
    - 自由文本由调用方 shlex.quote（踩坑 5）
    """
    with _server_exec_lock(server_name):
        return _ssh_exec_locked(server_name, command, timeout, killable=killable)


def _ssh_exec_locked(
    server_name: str,
    command: str,
    timeout: int,
    *,
    killable: bool,
) -> tuple[str, str, int]:
    client = get_connection(server_name)

    pid_file = ""
    if killable:
        # 踩坑 4：setsid 起独立进程组 + pid_file 记 PID，超时能杀整组
        pid_file = f"/tmp/bugflow_pid_{uuid.uuid4().hex[:10]}"
        run_cmd = (
            f"setsid bash -c {shlex.quote(command)} & "
            f"echo $! > {pid_file}; "
            f"wait $(cat {pid_file}); "
            f"ec=$?; rm -f {pid_file}; exit $ec"
        )
    else:
        run_cmd = command

    # 设 transport 超时（开 channel 前）
    transport = client.get_transport()
    old_timeout = transport.gettimeout() if hasattr(transport, "gettimeout") else None
    try:
        transport.settimeout(timeout)
    except Exception:
        pass

    try:
        stdin, stdout, stderr = client.exec_command(run_cmd)
    except Exception as e:
        raise SSHCommandTimeout(f"SSH 通道打开失败 ({timeout}s): {e}") from e
    finally:
        try:
            if old_timeout is not None:
                transport.settimeout(old_timeout)
        except Exception:
            pass

    stdout.channel.settimeout(timeout)

    try:
        poll_interval = 0.5
        waited = 0.0
        while not stdout.channel.exit_status_ready():
            if waited >= timeout:
                try:
                    stdout.channel.close()
                except (OSError, paramiko.SSHException):
                    pass
                if pid_file:
                    _kill_remote_pid(server_name, pid_file)
                raise SSHCommandTimeout(f"SSH 命令超时 ({timeout}s): {command[:100]}")
            time.sleep(poll_interval)
            waited += poll_interval

        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        # 踩坑 2：成功路径也关 channel 文件
        for s in (stdin, stdout, stderr):
            try:
                s.close()
            except Exception:
                pass
        return out, err, exit_code
    except socket.timeout:
        try:
            stdout.channel.close()
        except (OSError, paramiko.SSHException):
            pass
        if pid_file:
            _kill_remote_pid(server_name, pid_file)
        raise SSHCommandTimeout(f"SSH 读取超时 ({timeout}s): {command[:100]}")
    except SSHCommandTimeout:
        raise
    except Exception:
        # 踩坑 2：任意异常路径都关 channel（连接断开抛 SSHException/EOFError）
        for s in (stdin, stdout, stderr):
            try:
                s.close()
            except Exception:
                pass
        raise


# ── SFTP 文件传输 ─────────────────────────────────────────────────
def ssh_get_file(
    server_name: str,
    remote_path: str,
    local_path: str = "",
    timeout: int = 120,
) -> str:
    """通过 SFTP 从远程服务器拉文件到本地。

    复用连接池中已认证的 SSHClient（open_sftp 走同一 transport，无需重新认证）。
    与 exec 共享 transport channel，故复用 _server_exec_lock 串行化（防冲突）。

    Args:
        server_name: SSH 服务器名（如 "252"）
        remote_path: 远程文件路径（如 /home/user/out/.../gps.polaris.so）
        local_path: 本地保存路径，空则自动用 tempfile 临时目录
        timeout: 传输超时秒，默认 120（.so 文件通常 <50MB，足够）

    Returns:
        本地文件路径（供后续 adb push 用）

    Raises:
        SSHError: SFTP 传输失败（文件不存在/权限/超时）
    """
    if not local_path:
        basename = os.path.basename(remote_path.rstrip("/")) or "remote_file"
        local_path = os.path.join(
            tempfile.gettempdir(), f"bugfix_pull_{basename}_{os.getpid()}",
        )

    with _server_exec_lock(server_name):
        client = get_connection(server_name)
        sftp = None
        old_timeout = None
        chan = None
        try:
            sftp = client.open_sftp()
            # 在 SFTP channel 上设超时（Transport 本身无 settimeout）
            chan = sftp.get_channel()
            old_timeout = chan.gettimeout()
            chan.settimeout(timeout)
            # 自动创建本地父目录（与 write_file 远程侧 os.makedirs 对称）
            local_dir = os.path.dirname(os.path.abspath(local_path))
            if local_dir and not os.path.isdir(local_dir):
                os.makedirs(local_dir, exist_ok=True)
            sftp.get(remote_path, local_path)
            logger.info("SFTP 拉取 %s:%s → %s (%d bytes)",
                        server_name, remote_path, local_path,
                        os.path.getsize(local_path) if os.path.isfile(local_path) else 0)
            return local_path
        except (socket.timeout, OSError, paramiko.SSHException) as e:
            # 传输失败时清理可能的不完整本地文件
            try:
                if os.path.isfile(local_path):
                    os.remove(local_path)
            except OSError:
                pass
            raise SSHError(f"SFTP 拉取失败 {remote_path} → {local_path}: {e}") from e
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            if chan is not None and old_timeout is not None:
                try:
                    chan.settimeout(old_timeout)
                except Exception:
                    pass
