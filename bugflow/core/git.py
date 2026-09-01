"""Git 操作层 — 本地/SSH 双后端。

抄自 YWAgent git_tools._git_exec（自包含）：
  - 本地：subprocess argv 形式（shell=False），避免 Windows cmd 展开 %an/%h
  - SSH：cd repo && git <cmd>，shlex.quote repo_path（防单引号注入）
  - shlex.quote 全程

orchestrator 的 git_baseline 用这层做远程 stash/reset/diff；
submit 步骤用这层做 commit/push。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

from bugflow.core.ssh import ssh_exec, SSHError


# commit message 格式校验（禅道docID=120）
# 格式: <BugID|TaskID> [Description] 根因简述 [Solution] 修改简述
_COMMIT_MSG_RE = re.compile(r"^\d+\s+\[Description\].+\[Solution\].+")


def _is_local_path(path: str) -> bool:
    """路径在本地文件系统存在 → 本地 git。"""
    return bool(path) and os.path.isdir(path)


def git_exec(
    server: str,
    repo_path: str,
    git_cmd: str | list[str],
    timeout: int = 30,
) -> tuple[str, str, int]:
    """执行 git 命令，返回 (stdout, stderr, exit_code)。

    - server 空 或 repo_path 本地存在 → 本地 subprocess（argv，shell=False）
    - 否则 → SSH: cd repo && git <cmd>
    - 本地用 argv 形式：Windows cmd 不展开 --format 里的 %an/%h
    - git_cmd 传 list 时直接当 argv（跳过 shlex quote/split 往返，
      避免 Windows posix=False 不剥离 shlex.quote 单引号的问题）
    - SSH 路径 shlex.quote（防单引号注入）
    """
    # list → 直接当 argv（commit -m "含特殊字符的消息" 场景）
    if isinstance(git_cmd, (list, tuple)):
        argv_extra = list(git_cmd)
        ssh_cmd_str = " ".join(shlex.quote(str(x)) for x in argv_extra)
    else:
        argv_extra = shlex.split(git_cmd, posix=os.name != "nt")
        ssh_cmd_str = git_cmd

    if (not server) or _is_local_path(repo_path):
        try:
            argv = ["git"] + argv_extra
            result = subprocess.run(
                argv, shell=False, cwd=repo_path,
                capture_output=True, text=True, timeout=timeout, errors="replace",
            )
            return result.stdout or "", result.stderr or "", int(result.returncode)
        except subprocess.TimeoutExpired:
            return "", f"git 超时 ({timeout}s)", 124
        except (OSError, ValueError) as e:
            return "", str(e), 1

    # SSH 后端
    cmd = f"cd {shlex.quote(repo_path)} && git {ssh_cmd_str}"
    try:
        return ssh_exec(server, cmd, timeout=timeout)
    except SSHError as e:
        return "", str(e), 1


# ── 高层封装（orchestrator git_baseline 用）──────────────────
def reset_to_clean(server: str, repo_path: str, timeout: int = 60) -> dict:
    """把工作树恢复到干净状态：stash 任何改动 + reset --hard。

    每条 bug 处理前调，确保代码树干净。
    """
    # 先 stash（保留未提交改动以防万一，实际我们 reset 掉）
    stash_out, _, _ = git_exec(server, repo_path, "stash", timeout=timeout)
    reset_out, reset_err, ec = git_exec(server, repo_path, "reset --hard HEAD", timeout=timeout)
    clean_out, _, _ = git_exec(server, repo_path, "clean -fd", timeout=timeout)
    return {
        "success": ec == 0,
        "stash": stash_out.strip(),
        "reset": (reset_out + reset_err).strip(),
        "clean": clean_out.strip(),
    }


def get_diff(server: str, repo_path: str, timeout: int = 30) -> str:
    """取工作树 diff（未提交改动），供 reviewer 审查。"""
    out, err, ec = git_exec(server, repo_path, "diff HEAD", timeout=timeout)
    if ec != 0 and err.strip():
        return f"(diff 失败: {err.strip()})"
    return out.strip()


def get_status(server: str, repo_path: str, timeout: int = 15) -> str:
    """git status --short。"""
    out, _, _ = git_exec(server, repo_path, "status --short", timeout=timeout)
    return out.strip()


def commit(
    server: str,
    repo_path: str,
    message: str,
    add_all: bool = True,
    timeout: int = 60,
) -> dict:
    """提交改动。

    传 list 形式给 git_exec，跳过 shlex quote/split 往返 —
    Windows 上 posix=False 不剥离 shlex.quote 的单引号，会导致
    commit message 被字面包裹（'msg' 而非 msg）。
    add_all=True 先 git add -A。

    timeout 默认 60s（Gerrit commit-msg hook 可能慢）。

    返回 {success, nothing_to_commit, output, exit_code}:
    - ec=0 → 成功
    - ec=1 + "nothing to commit"/"nothing added to commit" → 无改动（success=True, nothing_to_commit=True）
    - 其他 → 失败
    """
    # ── commit message 格式校验（禅道docID=120）──
    first_line = message.strip().split("\n")[0]
    if not _COMMIT_MSG_RE.match(first_line):
        return {
            "success": False,
            "validation_error": True,
            "nothing_to_commit": False,
            "output": (
                "commit message 格式不符合规范（禅道docID=120）。\n"
                "要求: <BugID> [Description] 根因简述 [Solution] 修改简述\n"
                "示例: 94250 [Description] HCI超时导致BT崩溃 [Solution] ASSERT_LOG改为LOG_ERROR\n"
                "注意: 首行≤72字符，不要手写 Change-Id（hook 自动生成）\n"
                f"你的首行: {first_line[:80]}"
            ),
            "exit_code": -1,
        }

    if add_all:
        git_exec(server, repo_path, "add -A", timeout=timeout)
    out, err, ec = git_exec(server, repo_path, ["commit", "-m", message], timeout=timeout)
    output = (out + err).strip()
    nothing_to_commit = ec == 1 and (
        "nothing to commit" in output.lower()
        or "nothing added to commit" in output.lower()
    )
    return {
        "success": ec == 0 or nothing_to_commit,
        "nothing_to_commit": nothing_to_commit,
        "output": output,
        "exit_code": ec,
    }


def push(
    server: str,
    repo_path: str,
    push_target: str,
    timeout: int = 180,
) -> dict:
    """推送到远端（Gerrit code review）。

    push_target 是完整的 push 目标，如 "origin HEAD:refs/for/master"。
    由调用方（AI）确定 remote 和 branch。
    push_target 含空格分隔的多个参数（remote + refspec），
    用 shlex.split 拆成 argv 传 list 形式（与 commit 同理，跳过 Windows shlex 问题）。

    timeout 默认 180s（Gerrit push 可能慢）。

    返回 {success, already_exists, output, exit_code}:
    - ec=0 → 成功（含 Gerrit "no new changes" 即 Change 已存在的情况）
    - already_exists=True → Gerrit Change 已存在（no new changes）
    - ec≠0 → 失败
    """
    # push_target 格式固定为 "remote HEAD:refs/for/branch"，空格分隔，
    # 无 shell 元字符 — 用 split() 比 shlex.split(posix=True) 更安全
    # （posix=True 在 Windows 上会误切分含反斜杠的路径）
    out, err, ec = git_exec(
        server, repo_path, ["push"] + push_target.split(), timeout=timeout
    )
    output = (out + err).strip()
    already_exists = "no new changes" in output.lower()
    return {
        "success": ec == 0,
        "already_exists": already_exists,
        "output": output,
        "exit_code": ec,
    }


def get_current_branch(server: str, repo_path: str, timeout: int = 10) -> str:
    """当前分支名。"""
    out, _, _ = git_exec(server, repo_path, "rev-parse --abbrev-ref HEAD", timeout=timeout)
    return out.strip()
