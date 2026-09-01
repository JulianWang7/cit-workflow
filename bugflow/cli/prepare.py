"""bugfix-prepare — 为 ZCode goal 模式准备 bug 修复上下文。

从禅道拉取 bug 详情 + 下载附件 → 打印一段可直接粘贴到 ZCode goal 模式的提示词。

ZCode 的 MCP 工具（ssh-mcp / adb-mcp / zentao-mcp）和 skill（bugfix-fixer）
已通过 plugin.json 注册，goal 模式下可直接使用。此命令只需准备好 bug 信息
+ 环境参数，生成一个自包含的 goal prompt。

Usage:
    bugfix-prepare --bug-id 95456 --server 252 --code-root /home7/yangwei/SRM936_auto
    bugfix-prepare --bug-id 95456 --server 252 --code-root /path --device-serial SERIAL
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bugflow.core.config import load_server_config, list_servers


# ── Goal prompt 生成 ──────────────────────────────────────────
def build_goal_prompt(
    bug_id: str,
    title: str,
    description: str,
    server: str,
    code_root: str,
    device_serial: str = "",
    attachments: list[dict] | None = None,
    code_root_hint: str = "",
) -> str:
    """构建 ZCode goal 模式提示词。

    与 _build_fixer_pre_prompt 不同：
    - 交互式而非 headless，不需要 marker 输出约束
    - 包含全流程指引（reproduce→analyze→modify→review→compile→verify→submit）
    - 列出可用的 MCP 工具和 skill
    - 提示附件位置
    """
    # 附件信息
    att_section = ""
    if attachments:
        att_lines = []
        for att in attachments:
            name = att.get("title", att.get("filename", "unknown"))
            path = att.get("local_path", "")
            att_lines.append(f"  - {name} → {path}")
        att_section = f"\n## 附件（已下载到本地）\n" + "\n".join(att_lines) + "\n"
    elif attachments is not None:
        att_section = "\n## 附件\n无附件\n"

    # 设备信息
    device_line = device_serial or "自动选择（adb-mcp 会自动选设备）"

    return (
        f"请修复 Android Bug #{bug_id}。\n\n"
        f"## Bug 信息\n"
        f"- ID: {bug_id}\n"
        f"- 标题: {title}\n"
        f"- 描述:\n{description}\n"
        f"- 源码提示: {code_root_hint or '无，需搜索定位'}\n"
        f"{att_section}\n"
        f"## 环境\n"
        f"- SSH 服务器: {server}\n"
        f"- 代码根目录: {code_root}\n"
        f"- 设备 serial: {device_line}\n\n"
        f"## 可用工具\n"
        f"- ssh-mcp: set_workspace / search_code_tool / locate_files_tool / read_file / edit_file / write_file / "
        f"run_command / compile_module / git_op\n"
        f"- adb-mcp: device_state / clear_logcat_buffer / reproduce / verify / flash / adb_shell / screenshot\n"
        f"- zentao-mcp: zentao_get_bug / zentao_search_bugs / zentao_my_bugs / zentao_download_attachment\n"
        f"- skill: bugfix-fixer（加载后提供完整工作流指引和编码规范）\n"
        f"- 子 Agent（通过 Agent 工具派发，上下文隔离）: bugfix-analyst（只读分析）/ bugfix-worker（读写修改）/ bugfix-reviewer（只读审查）\n\n"
        f"## 工作流\n\n"
        f"### 主会话职责：复现 + 编排 + 编译/验证/提交\n\n"
        f"1. 先调用 set_workspace 设置工作区（server={server}, code_root={code_root}）\n"
        f"2. 在设备上复现 bug，抓 logcat 证据（使用 reproduce 工具）\n"
        f"   - 复现步骤未提供，请根据 bug 标题和描述推导 adb shell 命令序列\n"
        f"   - 常见模式: app crash → am start; SELinux denied → 触发对应操作; "
        f"系统属性 → setprop + am start\n"
        f"   - 复现完成后，整理 logcat 关键行 + 初步搜索命中的 path:line，作为子 Agent 的「已知信息」\n\n"
        f"### 子 Agent 派发（analyze/modify/review 必须派发，不可用时才回退主会话执行）\n\n"
        f"3. 派发 bugfix-analyst 分析根因（必须用 Agent 工具派发，不要主会话自己分析）：\n"
        f"   Agent(subagent_type='bugfix-analyst', prompt=...)，prompt 内容：\n"
        f"   - 分析 bug #{bug_id} 的根因。Bug 标题: {title}。\n"
        f"   - 第一步（必须）：set_workspace(server='{server}', code_root='{code_root}')\n"
        f"   - 已知信息（主会话已调查，直接使用，不重复调查）：\n"
        f"     <粘贴：reproduce 日志摘要、logcat 关键行、初步搜索命中的 path:line>\n"
        f"   - 需独立调查：<列出需子 Agent 自行执行的 search_code/read_file 步骤>\n"
        f"   - 输出：结构化根因报告，最后一行输出 ROOT_CAUSE_FOUND\n"
        f"   → 收到 ROOT_CAUSE_FOUND 后进入下一步\n\n"
        f"4. 派发 bugfix-worker 修改代码（必须用 Agent 工具派发，不要主会话自己改）：\n"
        f"   Agent(subagent_type='bugfix-worker', prompt=...)，prompt 内容：\n"
        f"   - 修复 bug #{bug_id}。\n"
        f"   - 第一步（必须）：set_workspace(server='{server}', code_root='{code_root}')\n"
        f"   - 根因分析（来自 analyst，按此修复）：<粘贴 analyst 返回的根因报告>\n"
        f"   - 编码规范：最小改动，修改标记注释，先 read_file 确认上下文再 edit_file\n"
        f"   - 执行范围：只改码，输出 PATCH_DONE 即停（编译/验证/提交由主会话接管）\n"
        f"   → 收到 PATCH_DONE 后进入下一步\n"
        f"   ⚠️ 修改标记规范（禅道docID=128）: 格式 // <!-- <name> add for bug {bug_id} start/end -->\n"
        f"   - 旧代码注释保留，先 git config user.name 取 <name>，edit_file 会校验标记\n\n"
        f"5. 派发 bugfix-reviewer 审查 patch（必须用 Agent 工具派发，不要主会话自审）：\n"
        f"   Agent(subagent_type='bugfix-reviewer', prompt=...)，prompt 内容：\n"
        f"   - 审查 bug #{bug_id} 的 patch。\n"
        f"   - 第一步（必须）：set_workspace(server='{server}', code_root='{code_root}')\n"
        f"   - 然后 git_op(action='diff') 读取改动，审查并输出 REVIEW_PASS 或 REVIEW_FAIL\n"
        f"   → REVIEW_FAIL 回第 4 步重新修改；REVIEW_PASS 进入下一步\n\n"
        f"### 主会话职责：编译 + 验证 + 提交\n\n"
        f"6. 编译修改的模块（使用 compile_module）\n"
        f"7. 推补丁到设备验证（使用 verify 工具，verify_commands 应与复现步骤一致）\n"
        f"8. 验证通过后提交并生成 patch + 分析总结（加载 bugfix-submit skill 按其流程执行）:\n"
        f"   - git_op(action='commit', commit_message='...')\n"
        f"   - run_command(git format-patch -1 HEAD) 生成 patch 文件，pull_file 拉到本地 reports/patches/{bug_id}/\n"
        f"   - 生成分析总结 md（reports/{bug_id}-fix-summary.md），含根因/改动/验证/建议 push 命令\n"
        f"   ⚠️ 不直接 push 到 Gerrit——由人工审查 md 总结后手动推送（见 bugfix-submit skill 注意事项）\n"
        f"   ⚠️ commit message 规范（禅道docID=120）: 按 commit_rules.md 执行（已注入会话上下文）。\n"
        f"   - 格式: {bug_id} [Description] 根因简述 [Solution] 修改简述\n"
        f"   - 首行≤72字符，不要手写 Change-Id，git_op 工具会校验格式\n\n"
        f"## 注意\n"
        f"- 每步完成后简要说明发现和改动\n"
        f"- 开始前先加载 bugfix-fixer skill（/bugfix-fixer）获取完整子 Agent 派发规范和编码规范\n"
        f"- 不要 commit 到 master 分支，先创建修复分支\n"
        f"- ⚠️ Iron Law: 根因未定位不能改码(NO FIX WITHOUT ROOT_CAUSE)；验证未通过不能提交(NO SUBMIT WITHOUT VERIFY_PASS)；"
        f"3次循环失败标 NEEDS_HUMAN 终止；遇歧义自行决定不要停下来等用户\n"
        f"- 完整规范加载 /bugfix-fixer skill 获取（含因果链深度要求、二维验证、升级机制）\n"
        f"- 修复完成后不要在禅道自动发评论或改状态——评论会被后续 AI 分析（batch_analysis / bugfix-analyze）作为历史参考读取，"
        f"未经人工确认的评论会误导后续 AI 产生错误传播。禅道状态更新和评论等人工验证后再补充"
    )


# ── 附件下载 ──────────────────────────────────────────────────
def _download_attachments(bug_id: int, bug_data: dict, save_dir: str) -> list[dict]:
    """下载 bug 附件到 save_dir，返回 [{title, local_path}] 列表。"""
    from bugflow.core import zentao

    attachments = bug_data.get("attachments", []) or []
    if not attachments:
        return []

    results = []
    os.makedirs(save_dir, exist_ok=True)
    for att in attachments:
        file_id = att.get("id")
        title = att.get("title", f"file_{file_id}")
        if not file_id:
            continue
        try:
            result_str = zentao.download_attachment(bug_id, int(file_id), save_dir)
            # download_attachment 返回 "已下载: /path/to/file (size)"
            local_path = result_str.split("已下载: ", 1)[-1].split(" (")[0] if "已下载: " in result_str else ""
            results.append({"title": title, "local_path": local_path, "filename": Path(local_path).name if local_path else title})
            print(f"  ✅ 下载附件: {title}")
        except Exception as e:
            print(f"  ⚠️ 附件 {title} 下载失败: {e}", file=sys.stderr)
            results.append({"title": title, "local_path": "", "filename": title, "error": str(e)})
    return results


# ── CLI 入口 ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="bugfix-prepare",
        description="为 ZCode goal 模式准备 bug 修复上下文（拉禅道 + 下载附件 + 打印 goal prompt）",
    )
    parser.add_argument("--bug-id", required=True, help="禅道 Bug ID（单个）")
    parser.add_argument("--server", required=True, help="SSH 服务器名 (servers.yaml 中定义)")
    parser.add_argument("--code-root", required=True, help="Android 源码根目录")
    parser.add_argument("--device-serial", default="", help="ADB 设备 serial (不传自动选)")
    parser.add_argument(
        "--save-dir",
        default="",
        help="附件保存目录 (默认: ./zentao_bugs/{bug_id})",
    )
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        help="跳过附件下载",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="尝试将 goal prompt 复制到剪贴板 (需要 pyperclip 或 Windows clip)",
    )
    args = parser.parse_args()

    # 验证 server 配置
    try:
        load_server_config(args.server)
    except Exception as e:
        print(f"❌ 服务器 '{args.server}' 配置不可用: {e}", file=sys.stderr)
        print(f"   可用服务器: {list_servers()}", file=sys.stderr)
        sys.exit(1)

    # 解析 bug_id
    try:
        bug_id_int = int(args.bug_id.strip())
    except ValueError:
        print(f"❌ Bug ID 格式错误: {args.bug_id}（应为数字）", file=sys.stderr)
        sys.exit(1)

    # ── 拉取 bug 详情 ──
    from bugflow.core import zentao

    print(f"📋 从禅道拉取 Bug #{bug_id_int}...")
    try:
        bug_data = zentao.get_bug(bug_id_int)
    except Exception as e:
        print(f"❌ Bug #{bug_id_int} 拉取失败: {e}", file=sys.stderr)
        sys.exit(1)

    title = bug_data.get("title", f"Bug #{bug_id_int}")
    description = zentao._html_to_text(bug_data.get("steps", ""))
    code_root_hint = bug_data.get("module", "") or ""

    print(f"  ✅ #{bug_id_int}: {title}")

    # ── 下载附件 ──
    attachments = None
    if not args.no_attachments:
        save_dir = args.save_dir or os.path.join("zentao_bugs", str(bug_id_int))
        atts = bug_data.get("attachments", []) or []
        if atts:
            print(f"\n📎 下载 {len(atts)} 个附件到 {save_dir}...")
            attachments = _download_attachments(bug_id_int, bug_data, save_dir)
        else:
            print("  无附件")
            attachments = []

    # ── 生成 goal prompt ──
    prompt = build_goal_prompt(
        bug_id=str(bug_id_int),
        title=title,
        description=description,
        server=args.server,
        code_root=args.code_root,
        device_serial=args.device_serial,
        attachments=attachments,
        code_root_hint=code_root_hint,
    )

    # ── 输出 ──
    print(f"\n{'='*60}")
    print("  📋 Goal Prompt（复制以下内容粘贴到 ZCode goal 模式）")
    print(f"{'='*60}")
    print()
    print(prompt)
    print()
    print(f"{'='*60}")

    # 尝试复制到剪贴板
    if args.copy:
        _try_copy_to_clipboard(prompt)

    print(f"\n💡 提示:")
    print(f"  - 在 ZCode 中按 Ctrl+G 或点击 Goal 输入框")
    print(f"  - 粘贴上面的 prompt，开始交互式修复")
    print(f"  - 修复过程中可随时对话调整策略")


def _try_copy_to_clipboard(text: str) -> None:
    """best-effort 复制到剪贴板。"""
    # 方式 1: pyperclip
    try:
        import pyperclip
        pyperclip.copy(text)
        print("  ✅ 已复制到剪贴板 (pyperclip)")
        return
    except ImportError:
        pass
    except Exception:
        pass

    # 方式 2: Windows clip.exe
    try:
        import subprocess
        subprocess.run(["clip"], input=text.encode("utf-8"), check=True, shell=False)
        print("  ✅ 已复制到剪贴板 (clip.exe)")
        return
    except Exception:
        pass

    # 方式 3: macOS pbcopy
    try:
        import subprocess
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        print("  ✅ 已复制到剪贴板 (pbcopy)")
        return
    except Exception:
        pass

    print("  ⚠️ 无法自动复制到剪贴板，请手动选择并复制", file=sys.stderr)


if __name__ == "__main__":
    main()
