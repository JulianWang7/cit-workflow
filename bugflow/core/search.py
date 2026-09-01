"""源码搜索层 — fd 定位 + rg 搜索，本地/SSH 双后端。

抄自 YWAgent tools/search_tools.py 的核心模式（自包含）：
  - _bundled_binary: vendor/bin 内置 fd+rg（dev 模式 + PyInstaller 模式）
  - _TYPE_MAP: file_type → (rg -t name, globs, fd -e exts)
  - _build_rg_type_args: -t 用于 rg 原生类型，-g glob 用于 Android 自定义类型
  - --no-ignore: Android .gitignore 排除真源码，必须关忽略
  - 本地走 subprocess，远程走 ssh_exec（fd/rg 经 PATH 注入或 ~/.androidagent/vendor/bin）

fd 和 rg 始终可用（vendor/bin 内置双平台二进制），无 find/grep fallback。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

from bugflow.core.ssh import ssh_exec



# ── 类型映射 ───────────────────────────────────────────────────
# type key → (rg -t name, space-separated globs, fd -e extensions)
# rg 原生知道的类型用 -t；bp/rc/conf/prop/cfg 等 rg 不知的用 -g glob。
_TYPE_MAP: dict[str, tuple[str, str, list[str]]] = {
    "java":   ("java", "*.java", ["java"]),
    "cpp":    ("cpp", "*.cpp *.cc *.cxx", ["cpp", "cc", "cxx"]),
    "c":      ("c", "*.c", ["c"]),
    "h":      ("c", "*.h *.hpp *.hxx", ["h", "hpp", "hxx"]),
    "python": ("py", "*.py", ["py"]),
    "py":     ("py", "*.py", ["py"]),
    "mk":     ("make", "*.mk", ["mk"]),
    "bp":     ("bp", "Android.bp *.bp", ["bp"]),
    "xml":    ("xml", "*.xml", ["xml"]),
    "kt":     ("kotlin", "*.kt", ["kt"]),
    "rc":     ("rc", "*.rc", ["rc"]),
    "proto":  ("protobuf", "*.proto", ["proto"]),
    "aidl":   ("aidl", "*.aidl", ["aidl"]),
    "conf":   ("conf", "*.conf", ["conf"]),
    "sh":     ("shell", "*.sh", ["sh"]),
    "prop":   ("prop", "*.prop", ["prop"]),
    "cfg":    ("cfg", "*.cfg", ["cfg"]),
    "json":   ("json", "*.json", ["json"]),
}

# rg 原生类型（跨版本安全，SSH 模式下不查远端 rg --type-list 直接用）
_UNIVERSAL_RG_TYPES = frozenset({
    "java", "cpp", "c", "h", "python", "py", "make", "mk", "xml",
    "kotlin", "sh", "json", "protobuf", "aidl", "css", "html", "js",
    "ts", "typescript", "go", "rust", "ruby", "swift", "scala",
    "dart", "lua", "perl", "php", "yaml", "toml", "markdown", "md",
})

LOCATE_MAX_DEPTH = 12
LOCATE_EXCLUDE_DIRS = ("out", ".repo", ".git", "prebuilts", ".ccache", "node_modules")


# ── 内置二进制 ─────────────────────────────────────────────────
def bundled_binary(name: str) -> str | None:
    """定位 vendor/bin/{name}（Linux/macOS）或 {name}.exe（Windows）。

    pip 开发模式: bugflow 包同级往上找 vendor/bin。
    PyInstaller 冻结模式: sys._MEIPASS/vendor/bin。
    """
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    candidates = []
    # 开发模式：bugflow/core/ → 上两级 → vendor/bin
    here = os.path.dirname(os.path.abspath(__file__))
    # bugflow/core/search.py → bugflow/core → bugflow → repo_root
    repo_root = os.path.dirname(os.path.dirname(here))
    candidates.append(os.path.join(repo_root, "vendor", "bin", f"{name}{exe_suffix}"))
    # PyInstaller 冻结
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "vendor", "bin", f"{name}{exe_suffix}"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _local_rg_bin() -> str:
    """本地 rg 二进制路径（bundled 优先，否则 PATH 里的 rg）。"""
    b = bundled_binary("rg")
    return b if b else "rg"


def _local_fd_bin() -> str:
    b = bundled_binary("fd")
    return b if b else "fd"


# ── 类型参数构建 ───────────────────────────────────────────────
def _parse_types(file_type: str) -> list[str]:
    """file_type 串 → type key 列表。空/`*`/`all` → 空（不过滤）。"""
    ft = (file_type or "").strip()
    if not ft or ft in ("*", "all"):
        return []
    return [t.strip() for t in ft.replace(",", " ").split() if t.strip()]


def _build_rg_type_args(types: list[str], ssh_mode: bool = False) -> list[str]:
    """构建 rg 类型参数：原生类型 -t，自定义类型 -g glob。

    ssh_mode=True 时不查远端 rg --type-list，直接用 _UNIVERSAL_RG_TYPES 判定。
    """
    valid = _UNIVERSAL_RG_TYPES if ssh_mode else _rg_valid_types_local()
    args: list[str] = []
    seen_globs: set[str] = set()
    for t in types:
        entry = _TYPE_MAP.get(t)
        if entry:
            rg_type_name, globs, _exts = entry
            if rg_type_name in valid:
                args.extend(["-t", rg_type_name])
            else:
                for g in globs.split():
                    if g not in seen_globs:
                        args.extend(["-g", g])
                        seen_globs.add(g)
        else:
            # 未知 key：是 rg 原生类型则 -t，否则当 glob
            if t in valid:
                args.extend(["-t", t])
            else:
                glob = f"*.{t}"
                if glob not in seen_globs:
                    args.extend(["-g", glob])
                    seen_globs.add(glob)
    return args


def _rg_valid_types_local() -> frozenset[str]:
    """查本地 rg --type-list（缓存）。查不到回退 _UNIVERSAL_RG_TYPES。"""
    try:
        result = subprocess.run(
            [_local_rg_bin(), "--type-list"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return frozenset(
                line.split(":")[0].strip()
                for line in result.stdout.splitlines() if ":" in line
            )
    except Exception:
        pass
    return _UNIVERSAL_RG_TYPES


def _type_fd_exts(types: list[str]) -> list[str]:
    """type keys → fd -e 扩展名列表。"""
    exts: list[str] = []
    for t in types:
        entry = _TYPE_MAP.get(t)
        if entry:
            exts.extend(entry[2])
        else:
            exts.append(t)
    # 去重保序
    seen = set()
    out = []
    for e in exts:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ── 本地/远程判定 ──────────────────────────────────────────────
def _is_local(server: str) -> bool:
    """无 server（本地工作区）或 server 为空 → 本地。"""
    return not (server or "").strip()


# ── rg 远程命令片段 ────────────────────────────────────────────
def _rg_setup_shell() -> str:
    """远端 rg 发现片段（优先用户目录，兼容无 sudo 安装）。

    顺序: $HOME/.local/bin/rg → $HOME/.androidagent/vendor/bin/rg → $HOME/bin/rg → PATH 中的 rg。
    """
    return (
        'rg_bin=""; '
        'for _cand in "$HOME/.local/bin/rg" "$HOME/.androidagent/vendor/bin/rg" "$HOME/bin/rg"; do '
        '  if [ -x "$_cand" ]; then rg_bin="$_cand"; break; fi; '
        'done; '
        'if [ -z "$rg_bin" ]; then '
        '  if command -v rg &>/dev/null; then rg_bin="$(command -v rg)"; else rg_bin="rg"; fi; '
        'fi; '
    )


def _fd_setup_shell() -> str:
    return (
        'fd_bin="fd"; '
        'command -v fd &>/dev/null || { command -v "$HOME/.local/bin/fd" &>/dev/null && fd_bin="$HOME/.local/bin/fd"; } || '
        '{ command -v "$HOME/.androidagent/vendor/bin/fd" &>/dev/null && fd_bin="$HOME/.androidagent/vendor/bin/fd"; }; '
    )


# ── search_code ───────────────────────────────────────────────
def search_code(
    pattern: str,
    dirs: list[str],
    server: str = "",
    file_type: str = "",
    max_results: int = 50,
    max_depth: int = 0,
    use_regex: bool = False,
    timeout: int = 15,
) -> str:
    """rg 搜索源码。本地走 subprocess，远程走 ssh_exec。

    - file_type: _TYPE_MAP 的 key（java/kt/bp/rc/...），空则不过滤
    - --no-ignore（Android .gitignore 排除真源码，不能跳过）
    - use_regex=False → -F 固定字符串（默认，防正则元字符误伤）
    """
    if not pattern or not dirs:
        return "(空 pattern 或 dirs)"

    types = _parse_types(file_type)

    if _is_local(server):
        return _rg_local(pattern, dirs, types, max_results, max_depth, use_regex, timeout)
    return _rg_remote(pattern, dirs, types, max_results, max_depth, use_regex, server, timeout)


def _rg_local(
    pattern: str, dirs: list[str], types: list[str],
    max_results: int, max_depth: int, use_regex: bool, timeout: int,
) -> str:
    cmd = [_local_rg_bin(), "-n", "--no-ignore"]
    cmd += _build_rg_type_args(types, ssh_mode=False)
    if not use_regex:
        cmd += ["-F"]
    if max_depth > 0:
        cmd += ["--max-depth", str(max_depth)]
    cmd += [pattern]
    cmd += dirs
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = result.stdout.strip()
        if not out:
            return "(0 匹配)"
        # head 限制行数
        lines = out.split("\n")
        if len(lines) > max_results:
            out = "\n".join(lines[:max_results]) + f"\n... ({len(lines)} 匹配，截断到 {max_results})"
        return out
    except subprocess.TimeoutExpired:
        return f"(rg 超时 {timeout}s)"
    except FileNotFoundError:
        return "(rg 未找到 — vendor/bin/rg 缺失)"


def _rg_remote(
    pattern: str, dirs: list[str], types: list[str],
    max_results: int, max_depth: int, use_regex: bool,
    server: str, timeout: int,
) -> str:
    type_flags = ""
    if types:
        type_args = _build_rg_type_args(types, ssh_mode=True)
        if type_args:
            type_flags = " " + " ".join(type_args)

    depth_flag = f" --max-depth {max_depth}" if max_depth > 0 else ""
    escaped_pattern = pattern.replace("'", "'\"'\"'")
    search_flag = "" if use_regex else " -F"
    paths = " ".join(shlex.quote(d) for d in dirs)

    # 错误日志写到 $HOME/tmp，避免无 sudo 时无法覆盖他人占用的 /tmp/bugflow_search_err.log
    cmd = (
        f"{_rg_setup_shell()}"
        'mkdir -p "$HOME/tmp"; _bf_err="$HOME/tmp/bugflow_search_err.log"; : > "$_bf_err"; '
        f'"$rg_bin" -n --no-ignore{type_flags}{search_flag} \'{escaped_pattern}\'{depth_flag} {paths} '
        f'2>"$_bf_err" | head -{max_results}; '
        'if [ -s "$_bf_err" ]; then cat "$_bf_err" >&2; fi'
    )
    try:
        out, err, ec = ssh_exec(server, cmd, timeout=timeout)
    except Exception as e:
        return f"(SSH 搜索失败: {e})"

    out = out.strip()
    if not out and err.strip():
        return f"(0 匹配, stderr: {err.strip()[:200]})"
    return out or "(0 匹配)"


# ── locate_files ──────────────────────────────────────────────
def locate_files(
    pattern: str,
    search_dir: str,
    server: str = "",
    file_type: str = "",
    max_hits: int = 40,
    max_depth: int = LOCATE_MAX_DEPTH,
    timeout: int = 25,
) -> str:
    """fd 按文件名定位。本地 subprocess / 远程 ssh_exec。

    - -g/--glob：*Foo* 当 glob 不当正则
    - --no-ignore：Android .gitignore 排除真源码
    - -i：大小写不敏感（*Volte*/*voLTE* 合并）
    - 排除 out/.repo/.git/prebuilts 等
    """
    if not pattern or not search_dir:
        return "(空 pattern 或 search_dir)"

    types = _parse_types(file_type)
    # pattern 自带扩展名 → 不叠加语言默认（*.java 不再 -e java）
    if _pattern_implies_extension(pattern):
        types = []

    if _is_local(server):
        return _fd_local(pattern, search_dir, types, max_hits, max_depth, timeout)
    return _fd_remote(pattern, search_dir, types, max_hits, max_depth, server, timeout)


def _pattern_implies_extension(pattern: str) -> bool:
    """pattern 含 .ext（如 *.java / Foo.c）→ True，不叠加默认类型。"""
    p = pattern.strip()
    if not p:
        return False
    # 取最后一段
    seg = p.split("/")[-1]
    return "." in seg.strip("*")


def _fd_local(
    pattern: str, search_dir: str, types: list[str],
    max_hits: int, max_depth: int, timeout: int,
) -> str:
    cmd = [_local_fd_bin(), "--no-ignore", "-i", "-g", "-t", "f"]
    depth = max(4, min(int(max_depth or LOCATE_MAX_DEPTH), 20))
    cmd += ["-d", str(depth)]
    for d in LOCATE_EXCLUDE_DIRS:
        cmd += ["-E", d]
    for e in _type_fd_exts(types):
        cmd += ["-e", e]
    cmd += [pattern, search_dir]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = result.stdout.strip()
        if not out:
            return "(0 文件)"
        lines = out.split("\n")
        if len(lines) > max_hits:
            out = "\n".join(lines[:max_hits]) + f"\n... ({len(lines)} 文件，截断到 {max_hits})"
        return out
    except subprocess.TimeoutExpired:
        return f"(fd 超时 {timeout}s)"
    except FileNotFoundError:
        return "(fd 未找到 — vendor/bin/fd 缺失)"


def _fd_remote(
    pattern: str, search_dir: str, types: list[str],
    max_hits: int, max_depth: int, server: str, timeout: int,
) -> str:
    fd_ext = ""
    if types:
        exts = _type_fd_exts(types)
        fd_ext = " " + " ".join(f"-e {e}" for e in exts)

    depth = max(4, min(int(max_depth or LOCATE_MAX_DEPTH), 20))
    fd_excl = " ".join(f"-E {d}" for d in LOCATE_EXCLUDE_DIRS)
    q_dir = shlex.quote(search_dir)
    q_pat = shlex.quote(pattern)

    cmd = (
        f"{_fd_setup_shell()}"
        f'"$fd_bin" --no-ignore -i -g -t f -d {depth} {fd_excl}{fd_ext} {q_pat} {q_dir} '
        f"2>/dev/null | head -{max_hits}"
    )
    try:
        out, _, _ = ssh_exec(server, cmd, timeout=timeout)
    except Exception as e:
        return f"(SSH 定位失败: {e})"
    return out.strip() or "(0 文件)"
