# -*- coding: utf-8 -*-
"""Skill 自进化闭环 — 修复成功后将经验回流到案例知识库 (kb_cases.sqlite)。

主路径（MCP 工具直接调用）:
  1. agent 在 submit 步骤 push 成功后调用 ``kb_add_case`` MCP 工具
  2. 工具内部调 ``infer_subsystem`` 推断子系统 → ``kb_cases.add_case`` 写入 (status=pending)
  3. 工具内部调 ``kb_cases.review_single_case`` 自动 LLM 审核 → approved → 可检索

备选路径（文本解析，保留兼容）:
  1. agent 在提交修复后输出 ``CASE_FEEDBACK: {json}`` 行
  2. ``append_case_from_feedback`` 解析该行 → 推断子系统 → 写入 kb_cases.sqlite
  （此路径需外部调用方主动触发，当前无 hook 自动检测）

案例库路径: ~/.bugfix-flow/kb_cases.sqlite
表结构见 core/kb_cases.py — 含 status 质量门禁 + author 多人标记 + source 来源追踪。

多人维护:
  - 每人本地积累 case → 定期 export approved → 汇总到内网公共服务器
  - 其他人 import → 去重入库 (source=sync)
  - 统一通过 kb_search(kb_type="cases") 检索已审核案例
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("core.skill_feedback")

# ── 关键词 → 子系统映射 ──────────────────────────────────────────
# 从 bug 标题 + 修改文件路径推断所属子系统。
# 顺序敏感：更具体的放前面（如 "audio" 在 "framework" 前）。
_SUBSYSTEM_KEYWORDS: list[tuple[str, list[str]]] = [
    ("audio_subsystem", ["audio", "audioflinger", "audiopolicy", "mediacodec", "mediaplayer", "nuplayer", "mediaextractor"]),
    ("display_subsystem", ["surfaceflinger", "display", "hwc", "gralloc", "drm_hwcomposer", "graphics"]),
    ("camera_subsystem", ["camera", "cameraservice", "hal3"]),
    ("connectivity_subsystem", ["wifi", "wlan", "bluetooth", "bt", "netd", "wpa"]),
    ("power_subsystem", ["power", "wakelock", "suspend", "battery", "charger", "health"]),
    ("boot_subsystem", ["boot", "init.rc", "zygote", "system_server", "bootcomplete"]),
    ("kernel_subsystem", ["kernel", "dmesg", "panic", "oom", "driver", "fstab"]),
    ("security_subsystem", ["selinux", "avc", "denied", "permission", ".te", "sepolicy"]),
    ("stability_subsystem", ["anr", "crash", "tombstone", "watchdog", "native crash", "signal"]),
    ("usb_subsystem", ["usb", "gadget", "udc", "adb"]),
    ("modem_subsystem", ["modem", "ril", "telephony", "sim", "carrier", "qmi"]),
    ("build_subsystem", ["android.bp", "android.mk", "soong", "makefile", "build/"]),
    ("partition_subsystem", ["partition", "vbmeta", "dm-verity", "super.img", "ota", "update_engine"]),
    ("framework_subsystem", ["ams", "wms", "pms", "activitymanager", "packagemanager", "windowmanager", "frameworks/base"]),
]

# 默认兜底（无法匹配时）
_DEFAULT_SUBSYSTEM = "framework_subsystem"

# CASE_FEEDBACK 行的正则: CASE_FEEDBACK: {...json...}
_FEEDBACK_RE = re.compile(
    r"CASE_FEEDBACK\s*:\s*(\{.*\})",
    re.IGNORECASE,
)


def infer_subsystem(bug_title: str, fix_files: list[str]) -> str:
    """从 bug 标题 + 修改文件路径推断子系统名。"""
    text = (bug_title or "") + " " + " ".join(fix_files or [])
    text_lower = text.lower()
    for subsystem, keywords in _SUBSYSTEM_KEYWORDS:
        if any(kw in text_lower for kw in keywords):
            return subsystem
    return _DEFAULT_SUBSYSTEM


def append_case(
    subsystem: str,
    bug_id: str,
    bug_title: str,
    root_cause: str,
    fix_summary: str,
    fix_files: list[str],
) -> bool:
    """回流一条 case 到案例知识库 (kb_cases.sqlite, status=pending)。

    未知子系统名 → 回退到 framework_subsystem。
    失败不抛异常，返回 False。

    Returns:
        True 成功，False 失败。
    """
    known_subsystems = {name for name, _ in _SUBSYSTEM_KEYWORDS}
    if subsystem not in known_subsystems:
        logger.warning("未知子系统: %s，case 回流到 framework_subsystem", subsystem)
        subsystem = _DEFAULT_SUBSYSTEM

    try:
        from .kb_cases import add_case
        case_id = add_case(
            bug_id=bug_id,
            title=bug_title,
            subsystem=subsystem,
            root_cause=root_cause,
            fix_summary=fix_summary,
            fix_files=fix_files,
        )
        if case_id is not None:
            logger.info(
                "case 已入库 (bug #%s, %s, status=pending, id=%s)",
                bug_id, subsystem, case_id,
            )
            return True
        return False
    except Exception as e:
        logger.warning("case 回流失败（不影响主流程）: %s", e)
        return False


def extract_feedback(text: str) -> dict | None:
    """从 agent 输出文本中提取 CASE_FEEDBACK JSON。

    匹配 ``CASE_FEEDBACK: {"root_cause": "...", "fix_summary": "...", "fix_files": [...]}``。
    Returns:
        解析后的 dict，或 None（未找到/解析失败）。
    """
    m = _FEEDBACK_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        # 校验必要字段
        if not data.get("root_cause") or not data.get("fix_summary"):
            logger.warning("CASE_FEEDBACK 缺少 root_cause 或 fix_summary，跳过")
            return None
        data.setdefault("fix_files", [])
        data.setdefault("bug_title", "")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("CASE_FEEDBACK JSON 解析失败: %s", e)
        return None


def append_case_from_feedback(
    text: str,
    bug_id: str = "",
    bug_title: str = "",
) -> bool:
    """从 agent 输出提取 CASE_FEEDBACK 并回流 case。

    Args:
        text: agent 的最终输出文本。
        bug_id: 当前 bug ID（从 env 补充）。
        bug_title: bug 标题（从 env 补充）。

    Returns:
        True 成功回流，False 未找到 feedback 或失败。
    """
    feedback = extract_feedback(text)
    if not feedback:
        return False

    fix_files = feedback.get("fix_files", [])
    subsystem = infer_subsystem(bug_title or feedback.get("bug_title", ""), fix_files)

    return append_case(
        subsystem=subsystem,
        bug_id=bug_id or feedback.get("bug_id", ""),
        bug_title=bug_title or feedback.get("bug_title", ""),
        root_cause=feedback.get("root_cause", ""),
        fix_summary=feedback.get("fix_summary", ""),
        fix_files=fix_files,
    )
