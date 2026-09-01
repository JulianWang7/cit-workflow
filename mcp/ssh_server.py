"""ssh-mcp — SSH/源码/编译/git MCP server（薄入口）。

ZCode 插件自动启动此 server（plugin.json mcpServers.ssh-mcp）。
stdio JSON-RPC → bugflow.core 转发，所有能力逻辑在 pip 包的 core 层。

工具列表（对应 fixer 工作流步骤）：
  set_workspace       — 设当前 bug 目标（server/serial/code_root 从 env 读）
  search_code_tool    — rg 源码搜索（analyze）
  locate_files_tool   — fd 定位文件（analyze）
  read_file           — 远程读文件（analyze/review）
  edit_file           — 远程改文件（modify）
  write_file          — 远程建文件（modify）
  run_command         — 通用 SSH shell（analyze）
  compile_module      — mmm 单编（compile）
  compile_full_build  — 整编（compile）
  compile_status      — 查编译进度（compile）
  compile_list        — 列编译任务（compile）
  compile_stop        — 停编译任务（compile）
  list_build_scripts  — 列平台整编脚本（compile）
  compile_script_build — 调平台脚本整编（compile）
  pull_file           — SFTP 拉文件到本地（verify）
  git_op              — git status/diff/commit（submit）

环境变量（orchestrator 注入，ZCode 会话继承 → MCP server 读取）：
  BUGFIX_SERVER        — SSH 服务器名（如 252）
  BUGFIX_DEVICE_SERIAL — ADB 设备 serial（adb-mcp 用，此 server 不用）
  BUGFIX_CODE_ROOT     — Android 源码树根
"""

from __future__ import annotations

import os
import sys

# 确保 bugflow 包可 import（插件 PYTHONPATH 已设，但兜底）
_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_here)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp SDK 未安装。请 pip install 'bugflow[mcp]'", file=sys.stderr)
    sys.exit(1)

from bugflow.core.config import get_bugfix_env, save_workspace
from bugflow.core.ssh import ssh_exec, ssh_get_file, SSHError
from bugflow.core.search import search_code, locate_files
from bugflow.core.compile import (
    build_module, build_full, build_status, build_list, build_stop,
    detect_build_scripts, parse_build_script, build_script,
)
from bugflow.core.git import (
    git_exec as _git_exec,
    reset_to_clean,
    get_diff,
    get_status,
    commit as _git_commit,
    push as _git_push,
    get_current_branch,
)
from bugflow.core.files import (
    read_file as _read_file,
    edit_file as _edit_file,
    write_file as _write_file,
)


mcp = FastMCP("ssh-mcp")


# ── 辅助 ──────────────────────────────────────────────────────
def _server() -> str:
    return get_bugfix_env()["server"]


def _code_root() -> str:
    return get_bugfix_env()["code_root"]


def _require_server() -> str:
    s = _server()
    if not s:
        raise ValueError("BUGFIX_SERVER 未设置 — orchestrator 应注入此 env")
    return s


def _require_code_root() -> str:
    r = _code_root()
    if not r:
        raise ValueError("BUGFIX_CODE_ROOT 未设置 — orchestrator 应注入此 env")
    return r


# ── 工具：set_workspace ───────────────────────────────────────
@mcp.tool()
def set_workspace(server: str = "", code_root: str = "", device_serial: str = "") -> str:
    """设当前 bug 的工作区目标。不传则读 BUGFIX_SERVER/BUGFIX_CODE_ROOT env。

    device_serial 用于多设备场景：连接 2+ 台设备时，先调 device_state 列出可选
    serial，由用户选定后传入此参数写回工作区，后续 adb 工具自动使用。

    返回确认信息，后续工具调用自动用此目标。

    写入 workspace.json（持久化），MCP server 重启后自动恢复。
    """
    try:
        parts = []
        srv = server or _server()
        root = code_root or _code_root()
        # device_serial 传入则用传入值，否则保留当前已有的 serial
        serial = device_serial or get_bugfix_env()["device_serial"]
        # 持久化到文件 + 写 env（进程内即时生效 + 跨进程恢复）
        save_workspace(server=srv, code_root=root, device_serial=serial)
        parts.append(f"server={srv or '(未设)'}")
        parts.append(f"code_root={root or '(未设)'}")
        parts.append(f"device_serial={serial or '(未设)'}")
        return "工作区已设: " + ", ".join(parts)
    except Exception as e:
        return f"❌ 设置工作区失败: {e}"


# ── 工具：search_code ─────────────────────────────────────────
@mcp.tool()
def search_code_tool(
    pattern: str,
    dirs: list[str],
    file_type: str = "",
    max_results: int = 50,
    use_regex: bool = False,
) -> str:
    """rg 搜索源码。pattern=关键词/正则，dirs=搜索目录列表，file_type=java/kt/bp/rc/...

    本地（server 空）走 subprocess，远程走 SSH。--no-ignore（Android .gitignore 排除真源码）。
    """
    try:
        srv = _server()
        if not dirs:
            root = _code_root()
            if root:
                dirs = [root]
            else:
                return "错误: 未指定 dirs 且无 code_root"
        return search_code(
            pattern, dirs, server=srv, file_type=file_type,
            max_results=max_results, use_regex=use_regex,
        )
    except Exception as e:
        return f"❌ 搜索失败: {e}"


# ── 工具：locate_files ────────────────────────────────────────
@mcp.tool()
def locate_files_tool(
    pattern: str,
    search_dir: str = "",
    file_type: str = "",
    max_hits: int = 40,
) -> str:
    """fd 按文件名定位。pattern=*Foo*（glob），search_dir 不传则用 code_root。"""
    try:
        srv = _server()
        sd = search_dir or _code_root()
        if not sd:
            return "错误: 未指定 search_dir 且无 code_root"
        return locate_files(pattern, sd, server=srv, file_type=file_type, max_hits=max_hits)
    except Exception as e:
        return f"❌ 定位失败: {e}"


# ── 工具：read_file ───────────────────────────────────────────
@mcp.tool()
def read_file(path: str, max_lines: int = 2000) -> str:
    """读远程文件内容（前 max_lines 行）。path 相对 code_root 或绝对路径。"""
    srv = _require_server()
    full = _resolve_path(path)
    return _read_file(srv, full, max_lines=max_lines)


# ── 工具：edit_file ───────────────────────────────────────────
@mcp.tool()
def edit_file(path: str, old_text: str, new_text: str, bug_id: str = "",
              skip_marker: bool = False) -> str:
    """远程文件文本替换（old_text → new_text）。精确匹配，替换前自动备份。

    old_text 必须在文件中唯一出现，否则报错（防误改多处）。
    bug_id 传 Bug ID 时会校验修改标记（禅道docID=128），修改源码必须传。
    skip_marker=True 时跳过标记校验（用于配置类/非源码文件修改），返回中附 ⚠️ 警告提醒后续补标记。
    """
    srv = _require_server()
    full = _resolve_path(path)
    return _edit_file(srv, full, old_text, new_text, bug_id=bug_id, skip_marker=skip_marker)


# ── 工具：write_file ──────────────────────────────────────────
@mcp.tool()
def write_file(path: str, content: str) -> str:
    """远程创建/覆盖文件。content 为完整文件内容。"""
    srv = _require_server()
    full = _resolve_path(path)
    return _write_file(srv, full, content)


# ── 工具：run_command ─────────────────────────────────────────
@mcp.tool()
def run_command(command: str, timeout: int = 60) -> str:
    """在远程服务器执行任意 shell 命令。返回 stdout + stderr + exit_code。

    用于探路、ls、自定义 fd/rg 组合等。长命令（make/编译）请调 compile 工具。
    """
    try:
        srv = _require_server()
        out, err, ec = ssh_exec(srv, command, timeout=timeout)
        result = out
        if err.strip():
            result += f"\n[stderr]\n{err}"
        result += f"\n[exit={ec}]"
        return result
    except Exception as e:
        return f"❌ 命令执行失败: {e}"


# ── 工具：pull_file ───────────────────────────────────────────
@mcp.tool()
def pull_file(path: str, local_path: str = "") -> str:
    """从远程服务器 SFTP 拉文件到本地。

    编译产物在服务器上，adb 在本地。用此工具把服务器上的 .so/.apk 拉到本地临时目录，再用 adb flash 推到设备。

    Args:
        path: 远程文件路径（相对 code_root 或绝对路径）
        local_path: 本地保存路径，不传则自动用系统临时目录
    """
    srv = _require_server()
    full = _resolve_path(path)
    try:
        local = ssh_get_file(srv, full, local_path)
    except SSHError as e:
        return f"❌ 拉取失败: {e}"
    return f"✅ 已拉取: {full} → {local}"


# ── 工具：compile ─────────────────────────────────────────────
@mcp.tool()
def compile_module(
    module: str,
    variant: str = "",
    vendor_seg: str = "",
    timeout: int = 600,
    background: bool = True,
    clean_mode: str = "",
    prebuilt_out: str = "",
) -> str:
    """make 单模块增量编译。module=模块路径（如 packages/apps/Settings）或模块名。

    自动 source envsetup + lunch + make <module_name> -j32。
    background 默认 True：提交后台编译，返回 task_id，用 compile_status 轮询。
    background=False：阻塞等待编译完成（长编译可能 SSH 超时）。

    variant 默认空=自动检测（out/build-*.ninja → 设备 getprop → 报错），
    不再硬编码高通默认值（展锐树会失败）。

    clean_mode: 编译前清理力度（默认 "" 不清理，纯增量最快）:
        "" — 不碰 out，直接 make（90% 场景，推荐）
        "installclean" — make installclean，清 image 产物保留中间 .o
        "restore" — rm -rf out && cp -a <prebuilt_out> out，需配 prebuilt_out
    prebuilt_out: clean_mode="restore" 时指定的预构建 out 目录路径。
    """
    try:
        srv = _require_server()
        root = _require_code_root()
        result = build_module(
            srv, root, module,
            variant=variant, vendor_seg=vendor_seg,
            timeout=timeout, background=background,
            clean_mode=clean_mode, prebuilt_out=prebuilt_out,
        )
    except Exception as e:
        return f"❌ 编译提交失败: {e}"

    if background:
        if result["success"]:
            warning = result.get("warning", "")
            lines = [
                f"⏳ 编译已后台提交",
                f"任务ID: {result['task_id']}",
                f"用 compile_status(task_id='{result['task_id']}') 查看进度",
            ]
            if warning:
                lines.append(f"\n⚠️ {warning}")
            return "\n".join(lines)
        return f"❌ 后台提交失败: {result.get('message', '')}"

    # 阻塞模式
    if result["success"]:
        lines = [f"✅ 编译成功 (exit={result['exit_code']})"]
        if result.get("warning"):
            lines.append(f"⚠️ {result['warning']}")
        if result["artifacts"]:
            lines.append(f"📦 产物:\n{result['artifacts']}")
        return "\n".join(lines)
    return f"❌ 编译失败 (exit={result['exit_code']}):\n{result['output'][:3000]}"


@mcp.tool()
def compile_full_build(
    variant: str = "",
    vendor_seg: str = "",
    timeout: int = 600,
    background: bool = True,
) -> str:
    """全树编译（整编）。不指定模块，make -j32 编译整个代码树。

    variant 自动推断：不传时从设备 getprop 读取。
    用于批量修复完成后将所有模块改动编译到一个完整镜像。
    background 默认 True：提交后台编译，返回 task_id，用 compile_status 轮询。
    """
    try:
        srv = _require_server()
        root = _require_code_root()
        result = build_full(
            srv, root,
            variant=variant, vendor_seg=vendor_seg,
            timeout=timeout, background=background,
        )
    except Exception as e:
        return f"❌ 整编提交失败: {e}"

    if background:
        if result["success"]:
            return (
                f"⏳ 整编已后台提交\n"
                f"任务ID: {result['task_id']}\n"
                f"用 compile_status(task_id='{result['task_id']}') 查看进度"
            )
        return f"❌ 后台提交失败: {result.get('message', '')}"

    # 阻塞模式
    if result["success"]:
        return f"✅ 整编成功 (exit={result['exit_code']})"
    return f"❌ 整编失败 (exit={result['exit_code']}):\n{result['output'][:3000]}"


@mcp.tool()
def compile_status(task_id: str, code_root: str = "") -> str:
    """轮询后台编译状态。

    返回 running/completed/failed + log 尾部 + 产物（如完成）。
    code_root 传入则编译完成时自动查产物；不传用当前 workspace 的 code_root。
    """
    try:
        root = code_root or _code_root()
        result = build_status(task_id, code_root=root)
    except Exception as e:
        return f"❌ 查询编译状态失败: {e}"

    status = result["status"]
    elapsed = result.get("elapsed", 0)
    icon = {"running": "⏳", "completed": "✅", "failed": "❌",
            "stopped": "🛑", "not_found": "❓"}.get(status, "?")

    if status == "not_found":
        return f"❓ 任务不存在: {task_id}"

    if status == "running":
        return (
            f"⏳ 编译进行中... ({elapsed:.0f}s)\n"
            f"--- log 尾部 ---\n{result.get('log_tail', '')}"
        )

    # 终态
    lines = [f"{icon} 编译{status} (exit={result.get('exit_code')}, {elapsed:.0f}s)"]
    if status == "completed" and result.get("artifacts"):
        lines.append(f"📦 产物:\n{result['artifacts']}")
    if result.get("log_tail"):
        lines.append(f"--- log 尾部 ---\n{result['log_tail']}")
    return "\n".join(lines)


@mcp.tool()
def compile_list() -> str:
    """列出所有编译任务（含历史）。"""
    try:
        return build_list()
    except Exception as e:
        return f"❌ 列出编译任务失败: {e}"


@mcp.tool()
def compile_stop(task_id: str) -> str:
    """停止后台编译任务。"""
    try:
        return build_stop(task_id)
    except Exception as e:
        return f"❌ 停止编译任务失败: {e}"


@mcp.tool()
def list_build_scripts() -> str:
    """列出当前 code_root 下所有可用整编脚本（展锐/高通自动识别）。

    返回脚本列表 + 平台类型 + 参数提示，供选择后调 compile_script_build。
    展锐: code_root/*.sh（含 meig/userdebug 关键词）
    高通: code_root/LA.VENDOR.*/*.sh（含 build 关键词）
    """
    try:
        srv = _require_server()
        root = _require_code_root()
    except RuntimeError as e:
        return f"❌ {e}"

    try:
        scripts = detect_build_scripts(srv, root)
    except Exception as e:
        return f"❌ 搜索整编脚本失败: {e}"

    if not scripts:
        return (
            f"未在 {root} 找到整编脚本。\n"
            f"展锐脚本应在根目录下（如 *_meig_userdebug.sh），\n"
            f"高通脚本应在 LA.VENDOR.*/ 下（如 *_build*.sh）。"
        )

    # 对每个脚本解析参数
    lines = [f"找到 {len(scripts)} 个整编脚本:"]
    for i, s in enumerate(scripts):
        try:
            parsed = parse_build_script(srv, s["script_path"])
        except Exception:
            parsed = {"platform": s["platform"], "boards": [], "build_types": [],
                      "default_build_num": "", "modes": []}

        platform_label = {"sprd": "展锐", "qcom": "高通"}.get(
            parsed.get("platform", s["platform"]), parsed.get("platform", "?"))
        lines.append(f"\n[{i}] [{platform_label}] {s['filename']}")
        lines.append(f"    路径: {s['script_path']}")
        if parsed.get("default_build_num"):
            lines.append(f"    默认版本号: {parsed['default_build_num']}")
        if parsed.get("boards"):
            lines.append(f"    板级: {', '.join(parsed['boards'])}")
        if parsed.get("modes"):
            modes_str = "full" if parsed["modes"] == ["full"] else \
                "full, " + ", ".join(f"--{m}" for m in parsed["modes"])
            lines.append(f"    模式: {modes_str}")

    lines.append(f"\n用 compile_script_build(script='<文件名>') 调用。")
    return "\n".join(lines)


@mcp.tool()
def compile_script_build(
    build_type: str = "userdebug",
    build_num: str = "",
    script: str = "",
    board: str = "",
    mode: str = "",
    ota: bool = False,
    clean: bool = False,
    background: bool = True,
    timeout: int = 600,
) -> str:
    """调用平台整编脚本编译完整镜像（自动检测展锐/高通脚本）。

    展锐: code_root/*.sh → bash <script> userdebug [ota] [build_num]
    高通: code_root/LA.VENDOR.*/*.sh → bash <script> userdebug <board> <build_num> [--mode] [--clean]

    script 空=自动检测；多脚本时返回列表让用户指定。
    background=True 后台提交，用 compile_status 轮询。
    先用 list_build_scripts 查看可用脚本及参数提示。
    """
    try:
        srv = _require_server()
        root = _require_code_root()
    except RuntimeError as e:
        return f"❌ {e}"

    try:
        result = build_script(
            srv, root,
            script=script,
            build_type=build_type,
            build_num=build_num,
            board=board,
            mode=mode,
            ota=ota,
            clean=clean,
            background=background,
            timeout=timeout,
        )
    except Exception as e:
        return f"❌ 整编脚本执行失败: {e}"

    # 多脚本：返回列表让用户选
    if not result.get("success") and result.get("scripts"):
        return result.get("error", "检测到多个脚本，请指定 script 参数。")

    if not result.get("success"):
        return f"❌ {result.get('error', '整编失败')}"

    if background:
        if result["success"]:
            return (
                f"⏳ 平台整编已后台提交\n"
                f"任务ID: {result['task_id']}\n"
                f"平台: {result.get('platform', '?')}, "
                f"脚本: {result.get('script', '?')}\n"
                f"用 compile_status(task_id='{result['task_id']}') 查看进度"
            )
        return f"❌ 后台提交失败: {result.get('message', '')}"

    # 阻塞模式
    if result["success"]:
        return f"✅ 整编成功 (exit={result['exit_code']})"
    return f"❌ 整编失败 (exit={result['exit_code']}):\n{result['output'][:3000]}"


# ── 工具：git_op ──────────────────────────────────────────────
@mcp.tool()
def git_op(
    action: str,
    commit_message: str = "",
    push_target: str = "",
) -> str:
    """Git 操作。action: status / diff / reset / commit / push / branch。

    commit 需传 commit_message。push 需传 push_target（如 "origin HEAD:refs/for/master"）。
    reset 会 stash + reset --hard + clean -fd（恢复干净状态）。
    """
    try:
        srv = _require_server()
        root = _require_code_root()

        if action == "status":
            return get_status(srv, root) or "(干净)"
        elif action == "diff":
            return get_diff(srv, root) or "(无改动)"
        elif action == "reset":
            result = reset_to_clean(srv, root)
            return f"reset {'成功' if result['success'] else '失败'}:\n{result['reset']}"
        elif action == "commit":
            if not commit_message:
                return "错误: commit 需传 commit_message"
            result = _git_commit(srv, root, commit_message)
            if result.get("validation_error"):
                return f"❌ {result['output']}"
            if result.get("nothing_to_commit"):
                return "ℹ️ 无需提交（工作树无改动）"
            return f"commit {'成功' if result['success'] else '失败'}:\n{result['output']}"
        elif action == "push":
            if not push_target:
                return "错误: push 需传 push_target（如 'origin HEAD:refs/for/master'）"
            result = _git_push(srv, root, push_target)
            if result.get("already_exists"):
                return "ℹ️ Change 已存在于 Gerrit（no new changes），非新失败"
            return f"push {'成功' if result['success'] else '失败'}:\n{result['output']}"
        elif action == "branch":
            return get_current_branch(srv, root) or "(unknown)"
        else:
            return f"未知 action: {action}。支持: status/diff/reset/commit/push/branch"
    except Exception as e:
        return f"❌ git 操作失败: {e}"


# ── 辅助：路径解析 ────────────────────────────────────────────
def _resolve_path(path: str) -> str:
    """相对路径 → 拼 code_root。绝对路径原样返回。"""
    if path.startswith("/"):
        return path
    root = _code_root()
    if root:
        return f"{root}/{path}"
    return path


if __name__ == "__main__":
    mcp.run(transport="stdio")
