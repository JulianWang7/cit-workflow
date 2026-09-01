# -*- coding: utf-8 -*-
"""从 85 SMB 共享同步知识库 SQLite 文件到本地配置目录。

依赖 Windows 凭据管理器缓存的 SMB 凭证，无需额外配置账号密码。
首次使用请在资源管理器访问 \\\\192.168.0.85 并勾选"记住凭据"。

与 scripts/build_kb_docs.py 的 _CORPUS_ROOT 同模式：直接用 Path 读 UNC 路径。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import _config_dir

# 85 共享上的 KB 分发目录（与 build_kb_docs._CORPUS_ROOT 同模式）
_KB_REMOTE_DIR = Path(r"\\192.168.0.85\软件部\yangwei")

# 6 个知识库文件：kb_type → 文件名
_KB_FILES: dict[str, str] = {
    "bug": "kb.sqlite",
    "docs": "kb_docs.sqlite",
    "aosp": "kb_aosp.sqlite",
    "wiki": "kb_wiki.sqlite",
    "tasks": "kb_tasks.sqlite",
    "cases": "kb_cases.sqlite",
}


def _fmt_size(n: int) -> str:
    """字节数 → 人类可读。"""
    if n >= 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024 / 1024:.1f}GB"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.0f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


def _share_reachable() -> bool:
    """探测 85 SMB 共享是否可达（依赖 Windows 缓存的凭据）。"""
    try:
        return _KB_REMOTE_DIR.exists()
    except OSError:
        return False


def kb_sync_status() -> str:
    """检查 85 可达性，显示 6 个知识库文件的本地/远程大小对比。

    不可达时返回引导提示；可达时返回对比表，标注需更新/最新/缺失。
    """
    if not _share_reachable():
        return (
            "⚠️ 85 服务器共享不可达：\\\\192.168.0.85\\软件部\\yangwei\n"
            "请在资源管理器地址栏输入 \\\\192.168.0.85，输入账号密码并勾选"
            '"记住凭据"，之后即可免配置自动访问。'
        )

    config_dir = _config_dir()
    lines = ["📦 知识库文件状态（本地 vs 85 共享）", ""]
    lines.append(f"{'文件名':<22} {'本地':>10}  {'远程':>10}  状态")
    lines.append("-" * 60)

    need_update: list[str] = []
    for kb_type, fname in _KB_FILES.items():
        local_path = config_dir / fname
        remote_path = _KB_REMOTE_DIR / fname

        local_sz = local_path.stat().st_size if local_path.exists() else 0
        try:
            remote_sz = remote_path.stat().st_size if remote_path.exists() else 0
        except OSError:
            remote_sz = 0

        local_str = _fmt_size(local_sz) if local_sz else "—"
        remote_str = _fmt_size(remote_sz) if remote_sz else "—"

        if remote_sz == 0:
            status = "⚠️ 远程缺失"
        elif local_sz == 0:
            status = "❌ 本地缺失"
            need_update.append(kb_type)
        elif remote_sz > local_sz + 1024:  # 允许 1KB 误差
            status = "⬆️ 需更新"
            need_update.append(kb_type)
        else:
            status = "✅ 最新"

        lines.append(f"{fname:<22} {local_str:>10}  {remote_str:>10}  {status}")

    lines.append("")
    if need_update:
        lines.append(f"💡 {len(need_update)} 个文件需更新：{', '.join(need_update)}")
        lines.append("   调用 kb_sync() 执行同步。")
    else:
        lines.append("✅ 所有知识库均为最新。")
    return "\n".join(lines)


def sync_kb(kb_type: str = "all") -> str:
    """从 85 共享复制知识库 SQLite 文件到本地配置目录。

    Args:
        kb_type: all(默认同步全部) / bug / docs / aosp / wiki / tasks / cases
    """
    kb_type_norm = (kb_type or "all").strip().lower()

    if kb_type_norm not in _KB_FILES and kb_type_norm != "all":
        valid = "all/" + "/".join(_KB_FILES.keys())
        return f"❌ 未知 kb_type「{kb_type}」。支持: {valid}"

    if not _share_reachable():
        return (
            "❌ 85 服务器共享不可达：\\\\192.168.0.85\\软件部\\yangwei\n"
            "请在资源管理器访问 \\\\192.168.0.85 并勾选\"记住凭据\"。"
        )

    targets = list(_KB_FILES.items()) if kb_type_norm == "all" else [(kb_type_norm, _KB_FILES[kb_type_norm])]

    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    lines = ["🔄 同步知识库文件", ""]
    copied = 0
    skipped = 0
    failed = 0

    for _kt, fname in targets:
        remote_path = _KB_REMOTE_DIR / fname
        local_path = config_dir / fname

        try:
            if not remote_path.exists():
                lines.append(f"  ⚠️ {fname} — 远程不存在，跳过")
                skipped += 1
                continue

            remote_sz = remote_path.stat().st_size
            local_sz = local_path.stat().st_size if local_path.exists() else 0

            if local_path.exists() and remote_sz <= local_sz + 1024:
                lines.append(f"  ✅ {fname} — 已最新 ({_fmt_size(local_sz)})，跳过")
                skipped += 1
                continue

            # SQLite 文件可能被本地进程打开（sqlite3 连接），先尝试删除旧文件
            if local_path.exists():
                try:
                    local_path.unlink()
                except PermissionError:
                    lines.append(f"  ⚠️ {fname} — 本地文件被占用，无法覆盖（请关闭占用进程）")
                    failed += 1
                    continue

            shutil.copy2(remote_path, local_path)
            lines.append(f"  📥 {fname} — {_fmt_size(local_sz)} → {_fmt_size(remote_sz)} ✅")
            copied += 1
        except Exception as e:
            lines.append(f"  ❌ {fname} — {e}")
            failed += 1

    lines.append("")
    lines.append(f"汇总：{copied} 复制 / {skipped} 跳过 / {failed} 失败")
    if copied > 0:
        lines.append("✅ 同步完成。")
    return "\n".join(lines)
