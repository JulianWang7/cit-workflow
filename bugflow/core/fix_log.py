# -*- coding: utf-8 -*-
"""修复结果落库 — append-only JSONL，供效率统计。

每次 bug 修复的关键节点（verify / commit）追加一行到 ``~/.bugfix-flow/fix_log.jsonl``。
写入失败不抛异常 — 日志不能影响主流程。

记录字段:
  timestamp / bug_id / root_cause_category / fix_files /
  symptom_gone / duration_s / human_intervention / error_step / notes
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .config import _config_dir

logger = logging.getLogger("core.fix_log")


def _log_file() -> Path:
    """fix_log.jsonl 路径: <config_dir>/fix_log.jsonl。"""
    return _config_dir() / "fix_log.jsonl"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _duration_from_env() -> int:
    """从 env 的 BUGFIX_FIX_START_TIME 算耗时秒数，缺失返回 0。"""
    start_str = os.environ.get("BUGFIX_FIX_START_TIME", "")
    if not start_str:
        return 0
    try:
        start = datetime.fromisoformat(start_str)
        return int((datetime.now() - start).total_seconds())
    except (ValueError, TypeError):
        return 0


def log_fix_outcome(
    bug_id: str,
    root_cause_category: str = "",
    fix_files: list[str] | None = None,
    symptom_gone: bool | None = None,
    duration_s: int = 0,
    human_intervention: bool = False,
    error_step: str = "",
    notes: str = "",
) -> None:
    """追加一行修复结果到 fix_log.jsonl。

    Args:
        bug_id: 禅道 Bug ID。
        root_cause_category: 根因子所属子系统（如 audio/framework/kernel），空则未填。
        fix_files: 修改的源码文件路径列表。
        symptom_gone: verify 判定 — True=症状消失 / False=仍有症状 / None=未验证。
        duration_s: 本次修复总耗时秒；传 0 则从 env 自动算。
        human_intervention: 是否需要人工介入。
        error_step: 卡在哪一步 — reproduce/analyze/compile/verify，空则成功。
        notes: 附注。
    """
    if not duration_s:
        duration_s = _duration_from_env()

    record = {
        "timestamp": _now_iso(),
        "bug_id": str(bug_id),
        "root_cause_category": root_cause_category,
        "fix_files": fix_files or [],
        "symptom_gone": symptom_gone,
        "duration_s": duration_s,
        "human_intervention": human_intervention,
        "error_step": error_step,
        "notes": notes,
    }
    try:
        p = _log_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("fix_log 已记录 bug #%s (symptom_gone=%s, %ds)",
                    bug_id, symptom_gone, duration_s)
    except Exception as e:
        logger.warning("fix_log 写入失败（不影响主流程）: %s", e)


def start_fix_tracking(bug_id: str) -> None:
    """记录修复开始时间到 env，供后续 log_fix_outcome 算耗时。"""
    os.environ["BUGFIX_CURRENT_BUG_ID"] = str(bug_id)
    os.environ["BUGFIX_FIX_START_TIME"] = _now_iso()


def clear_fix_tracking() -> None:
    """清除 env 中的修复追踪字段（流程结束后调用）。"""
    os.environ.pop("BUGFIX_CURRENT_BUG_ID", None)
    os.environ.pop("BUGFIX_FIX_START_TIME", None)


def current_bug_id() -> str:
    """读 env 中的当前 bug_id，空则未在修复流程中。"""
    return os.environ.get("BUGFIX_CURRENT_BUG_ID", "")
