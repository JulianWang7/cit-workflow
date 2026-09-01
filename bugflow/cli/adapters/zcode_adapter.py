"""ZCode CLI 适配器 — subprocess 调用 zcode.cjs headless 模式。

封装 ZCode CLI 的 --target / --prompt headless 模式：
  - 从 credentials.py 获取 env 三元组
  - 构造命令行参数
  - subprocess 执行 + 超时控制
  - 解析输出中的 marker（PATCH_DONE / REVIEW_PASS / ...）
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bugflow.credentials import load_zcode_credentials
from bugflow.cli.adapters.base import AgentAdapter, AgentResult, SessionConfig


# ── ZCode CLI 二进制查找 ──────────────────────────────────────
def _scan_windows_install_dirs() -> list[Path]:
    """扫描常见 Windows ZCode 安装目录（env var 未设时的 fallback）。"""
    candidates: list[Path] = []
    # 注册表查 UninstallString（Electron app 通常注册）
    try:
        import winreg  # type: ignore[import-not-found]
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            sub_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, sub_name) as sub:
                                try:
                                    display = winreg.QueryValueEx(sub, "DisplayName")[0]
                                    if "ZCode" in str(display):
                                        loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                                        if loc:
                                            candidates.append(Path(loc))
                                except OSError:
                                    pass
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass  # 非 Windows

    # 常见硬编码路径
    for drive in ("C", "D", "E"):
        for base in (f"{drive}:/Program Files", f"{drive}:/Program Files (x86)",
                     f"{drive}:/Local/Programs", f"{drive}:/Programs"):
            candidates.append(Path(base) / "ZCode")

    return candidates


def _find_zcode_cjs() -> str | None:
    """查找 zcode.cjs 二进制路径。

    顺序:
    1. ZCODE_CLI_PATH 环境变量
    2. ZCODE_WINDOWS_APP_INSTALL_DIR/resources/glm/zcode.cjs (Windows 安装版)
    3. 注册表 / 常见安装目录 fallback (resources/glm/zcode.cjs)
    4. ~/.zcode/cli/zcode.cjs
    5. PATH 中的 zcode 命令
    """
    # 1. 环境变量
    env_path = os.environ.get("ZCODE_CLI_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    # 2. Windows 安装目录 (env var)
    install_dir = os.environ.get("ZCODE_WINDOWS_APP_INSTALL_DIR", "")
    if install_dir:
        candidate = Path(install_dir) / "resources" / "glm" / "zcode.cjs"
        if candidate.is_file():
            return str(candidate)

    # 3. 注册表 / 常见安装目录 fallback
    for base_dir in _scan_windows_install_dirs():
        candidate = base_dir / "resources" / "glm" / "zcode.cjs"
        if candidate.is_file():
            return str(candidate)

    # 4. ~/.zcode/cli
    home_candidate = Path.home() / ".zcode" / "cli" / "zcode.cjs"
    if home_candidate.is_file():
        return str(home_candidate)

    # 5. PATH
    which = shutil.which("zcode")
    if which:
        return which

    return None


# ── Marker 解析 ───────────────────────────────────────────────
# 所有可能的结束标记
_ALL_MARKERS = [
    "REPRODUCE_DONE", "REPRODUCE_FAIL",
    "ROOT_CAUSE_FOUND",
    "PATCH_DONE",
    "REVIEW_PASS", "REVIEW_FAIL",
    "COMPILE_DONE", "COMPILE_FAIL",
    "VERIFY_PASS", "VERIFY_FAIL", "VERIFY_WARN",
    "SUBMIT_DONE",
]


def _extract_marker(output: str) -> str:
    """从 agent 输出中提取结束标记。

    标记可能在行首（允许前导空格/ markdown 格式符），单独成行或行尾。
    自动剥离 markdown 格式符 (**、*、`、#) 后匹配。
    返回第一个匹配的 marker，无匹配返回 ""。
    """
    for line in output.split("\n"):
        # 剥离 markdown 格式符: **marker** → marker, `marker` → marker
        stripped = line.strip()
        # 去掉前后 **, *, `, #, > 等 markdown 修饰符
        cleaned = stripped.lstrip("*`#> ").rstrip("*`")
        for marker in _ALL_MARKERS:
            if cleaned == marker or cleaned.startswith(marker + ":"):
                return marker
    return ""


# ── ZCode 适配器 ──────────────────────────────────────────────
class ZCodeAdapter(AgentAdapter):
    """通过 subprocess 调用 ZCode CLI 的适配器。"""

    def __init__(self, zcode_cjs_path: str | None = None, provider_hint: str = ""):
        self._zcode_path = zcode_cjs_path or _find_zcode_cjs()
        self._provider_hint = provider_hint
        self._creds = load_zcode_credentials(provider_hint)

    def health_check(self) -> bool:
        """检查 CLI 二进制和凭证是否可用。"""
        if not self._zcode_path or not Path(self._zcode_path).is_file():
            return False
        if not self._creds.get("ZCODE_MODEL"):
            return False
        return True

    def run_session(self, config: SessionConfig) -> AgentResult:
        """执行一次 ZCode headless 会话。"""
        if not self.health_check():
            return AgentResult(
                success=False,
                output="",
                error=f"ZCode CLI 不可用: path={self._zcode_path}",
            )

        # 构造命令
        cmd = [
            "node",
            self._zcode_path,
        ]
        if config.mode == "prompt":
            cmd += ["--prompt", config.goal]
        else:  # target
            cmd += ["--target", config.goal]

        # 权限模式
        cmd += ["--mode", config.permission_mode]

        # 工作目录
        if config.cwd:
            cmd += ["--cwd", config.cwd]

        # 工具黑名单
        if config.disallowed_tools:
            cmd += ["--disallowed-tools", " ".join(config.disallowed_tools)]

        # 环境变量：ZCode 三元组 + orchestrator 注入的 BUGFIX_* 变量
        env = os.environ.copy()
        env.update(self._creds)
        env.update(config.env)

        # 执行
        start = time.time()

        # 429 自动重试（最多 2 次，间隔递增）
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=config.timeout_s,
                )
                elapsed = time.time() - start
                output = proc.stdout

                # 提取 marker
                marker = _extract_marker(output)

                # 错误分析
                error = ""
                if proc.returncode != 0:
                    stderr_tail = (proc.stderr or "").strip()
                    combined = output + stderr_tail

                    # 429 限流检测
                    if "429" in combined or "Too Many Requests" in combined:
                        if attempt < max_retries:
                            wait = 30 * (attempt + 1)
                            print(f"  ⏳ API 限流 (429)，{wait}s 后重试 (attempt {attempt+1}/{max_retries})...", file=sys.stderr)
                            time.sleep(wait)
                            continue  # 重试
                        else:
                            error = "API 限流 (429 Too Many Requests) — 请稍后重试或降低并发"

                    # 其他错误：取最后 500 字
                    elif stderr_tail:
                        error = stderr_tail[-500:] if len(stderr_tail) > 500 else stderr_tail
                    elif not output.strip():
                        error = f"进程退出码 {proc.returncode}，stdout/stderr 均空"

                # 判定成功
                success = proc.returncode == 0 and bool(marker or output.strip())

                return AgentResult(
                    success=success,
                    output=output,
                    marker=marker,
                    error=error,
                    elapsed_s=elapsed,
                )

            except subprocess.TimeoutExpired:
                elapsed = time.time() - start
                return AgentResult(
                    success=False,
                    output="",
                    error=f"会话超时 ({config.timeout_s}s)",
                    elapsed_s=elapsed,
                )
            except Exception as e:
                elapsed = time.time() - start
                return AgentResult(
                    success=False,
                    output="",
                    error=f"执行异常: {e}",
                    elapsed_s=elapsed,
                )

        # 不应到达此处
        return AgentResult(
            success=False,
            output="",
            error="重试次数耗尽",
            elapsed_s=time.time() - start,
        )
