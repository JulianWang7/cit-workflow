# -*- coding: utf-8 -*-
"""远程文件操作层 — SSH 读/编辑/写文件。

从 ssh_server.py 下沉的业务逻辑。server 层负责 _require_server + _resolve_path，
core 层接收已解析的 server 和 full path，执行实际 SSH 操作。
"""

from __future__ import annotations

import base64
import shlex

from .ssh import ssh_exec


def read_file(server: str, full_path: str, max_lines: int = 2000) -> str:
    """读远程文件内容（前 max_lines 行）。

    Args:
        server: SSH 服务器标识
        full_path: 已解析的绝对路径（server 层负责 code_root 拼接）
        max_lines: 读取行数上限
    """
    try:
        cmd = f"head -n {max_lines} {shlex.quote(full_path)}"
        out, err, ec = ssh_exec(server, cmd, timeout=15)
        if ec != 0 and not out.strip():
            return f"读取失败 (exit={ec}): {err.strip()}"
        return out
    except Exception as e:
        return f"❌ 读取失败: {e}"


def edit_file(
    server: str,
    full_path: str,
    old_text: str,
    new_text: str,
    bug_id: str = "",
    skip_marker: bool = False,
) -> str:
    """远程文件文本替换（old_text → new_text）。精确匹配，替换前自动备份。

    old_text 必须在文件中唯一出现，否则报错（防误改多处）。
    bug_id 传 Bug ID 时会校验修改标记（禅道docID=128），修改源码必须传。
    skip_marker=True 时跳过标记校验，返回中附 ⚠️ 警告提醒后续补标记。
    """
    try:
        # ── 修改标记校验（禅道docID=128）──
        marker_warning = ""
        if bug_id:
            start_marker = f"add for bug {bug_id} start"
            end_marker = f"add for bug {bug_id} end"
            if start_marker not in new_text or end_marker not in new_text:
                if skip_marker:
                    marker_warning = (
                        f"⚠️ 跳过修改标记校验（skip_marker=True）— "
                        f"bug_id={bug_id} 但 new_text 缺少标记，请确保后续手动补标记:\n"
                        f"  // <!-- <name> {start_marker}\n"
                        f"  // <!-- <name> {end_marker} -->\n"
                        f"提示: 先 run_command(command='git config user.name') 获取 <name>"
                    )
                else:
                    return (
                        f"❌ 修改标记缺失（禅道docID=128）。bug_id={bug_id} 时 new_text 必须包含:\n"
                        f"  起始标记含: {start_marker}\n"
                        f"  结束标记含: {end_marker}\n"
                        f"格式（按语言注释符）:\n"
                        f"  // <!-- <name> {start_marker}\n"
                        f"  // oldCode = ...（旧代码注释保留，不可删除）\n"
                        f"  newCode = ...\n"
                        f"  // <!-- <name> {end_marker} -->\n"
                        f"提示: 先 run_command(command='git config user.name') 获取 <name>\n"
                        f"或传 skip_marker=True 跳过校验（用于配置类/非源码文件）"
                    )

        b64_path = base64.b64encode(full_path.encode("utf-8")).decode("ascii")
        b64_old = base64.b64encode(old_text.encode("utf-8")).decode("ascii")
        b64_new = base64.b64encode(new_text.encode("utf-8")).decode("ascii")

        script = (
            f"python3 -c \"\n"
            f"import sys, base64\n"
            f"path = base64.b64decode('{b64_path}').decode('utf-8')\n"
            f"old = base64.b64decode('{b64_old}').decode('utf-8')\n"
            f"new = base64.b64decode('{b64_new}').decode('utf-8')\n"
            f"with open(path, 'r') as f: content = f.read()\n"
            f"count = content.count(old)\n"
            f"if count == 0:\n"
            f"    print('NOT_FOUND'); sys.exit(1)\n"
            f"if count > 1:\n"
            f"    print(f'DUPLICATE:{{count}}'); sys.exit(1)\n"
            f"import shutil; shutil.copy2(path, path + '.bak')\n"
            f"content = content.replace(old, new, 1)\n"
            f"with open(path, 'w') as f: f.write(content)\n"
            f"print('OK')\n"
            f"\""
        )
        out, err, ec = ssh_exec(server, script, timeout=15)
        if ec != 0:
            if "NOT_FOUND" in out:
                return f"错误: old_text 在文件中未找到: {full_path}"
            if "DUPLICATE:" in out:
                n = out.split("DUPLICATE:")[1].strip().split()[0]
                return f"错误: old_text 在文件中出现 {n} 次（需唯一），请缩小匹配范围: {full_path}"
            return f"替换失败 (exit={ec}): {out.strip()} {err.strip()}"
        if "OK" not in out:
            return f"替换失败 (exit={ec}): {out.strip()} {err.strip()}"
        prefix = f"{marker_warning}\n" if marker_warning else ""
        return f"{prefix}已替换: {full_path}"
    except Exception as e:
        return f"❌ 编辑失败: {e}"


def write_file(server: str, full_path: str, content: str) -> str:
    """远程创建/覆盖文件。content 为完整文件内容。"""
    try:
        b64_path = base64.b64encode(full_path.encode("utf-8")).decode("ascii")
        b64_content = base64.b64encode(content.encode("utf-8")).decode("ascii")

        script = (
            f"python3 -c \"\n"
            f"import base64, os\n"
            f"path = base64.b64decode('{b64_path}').decode('utf-8')\n"
            f"content = base64.b64decode('{b64_content}').decode('utf-8')\n"
            f"_d = os.path.dirname(path)\n"
            f"if _d: os.makedirs(_d, exist_ok=True)\n"
            f"with open(path, 'w') as f: f.write(content)\n"
            f"print('OK')\n"
            f"\""
        )
        out, err, ec = ssh_exec(server, script, timeout=15)
        if ec != 0 or "OK" not in out:
            return f"写入失败 (exit={ec}): {out.strip()} {err.strip()}"
        return f"已写入: {full_path}"
    except Exception as e:
        return f"❌ 写入失败: {e}"
