# -*- coding: utf-8 -*-
"""批量解决进度持久化 — 断点续跑。

状态文件: ``~/.bugfix-flow/batch_state_{date}.json``（或 ``BUGFIX_CONFIG_DIR``
指定的目录）。原子写入（``.tmp`` + ``replace``），遵循 backfill_gerrit /
tasks 的同款先例。

orchestrator 每次 cron 触发时:
  1. ``load`` 当日状态 → 过滤掉已达终态的 Bug
  2. 只对"待处理"Bug 跑 Phase 1-5
  3. 每个关键节点 ``update`` 单 Bug 状态并落盘
  4. Phase 5 报告前 ``save`` 最终状态

终态定义（SKILL.md 的 fix_status 枚举）:
  - 已修复已验证 / 不能复现 / 需人工介入 / 已修复待push / 修复失败 / 已分析待修复 → 跳过
  - 可复现未修复 / 未记录 → 需处理
  - retry_count ≥ 3 时由调用方升级为"需人工介入"（终态，不再重试）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import _config_dir

logger = logging.getLogger("core.batch_state")

# ── 终态集合 ──────────────────────────────────────────────────────
# fix_status 达到这些值后不再重试（SKILL.md:25-34 的 5 态枚举）。
# "可复现未修复" 不是终态——中断后应重试。
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "已修复已验证",
    "不能复现",
    "需人工介入",
    "已修复待push",
    "修复失败",
    "已分析待修复",
})

# 单 Bug 记录的字段骨架（首次出现时填入）。
_BUG_FIELDS: dict[str, Any] = {
    "title": "",
    "module": "",
    "phase": "",            # attempted/reproduced/analyzed/fixed/built/reported
    "fix_status": "",       # SKILL.md 状态枚举，未尝试时为空
    "task_id": "",
    "reproduce_commands": [],
    "logcat_filter": "",
    "gerrit_url": "",
    "notes": "",
    "retry_count": 0,       # 失败次数，≥3 时由调用方升级为"需人工介入"
    "updated_at": "",
}


def _state_file(date: str) -> Path:
    """状态文件路径: <config_dir>/batch_state_{date}.json。"""
    return _config_dir() / f"batch_state_{date}.json"


def _empty_state(date: str) -> dict[str, Any]:
    """构造空骨架。"""
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "date": date,
        "created_at": now,
        "updated_at": now,
        "bugs": {},
        "current_phase": "",
    }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_batch_state(date: str) -> dict[str, Any]:
    """加载当日批量状态。

    缺失或损坏时返回空骨架（不抛异常），调用方据此判断是否首次运行。
    """
    p = _state_file(date)
    if not p.exists():
        logger.info("batch_state %s 不存在（首次运行）", date)
        return _empty_state(date)
    try:
        with p.open("r", encoding="utf-8") as f:
            state = json.load(f)
        # 补全可能缺失的字段（向前兼容）
        state.setdefault("date", date)
        state.setdefault("created_at", _now_iso())
        state.setdefault("updated_at", _now_iso())
        state.setdefault("bugs", {})
        state.setdefault("current_phase", "")
        return state
    except Exception as e:
        logger.warning("batch_state %s 损坏，返回空骨架: %s", date, e)
        return _empty_state(date)


def save_batch_state(state: dict[str, Any]) -> dict[str, Any]:
    """原子写入批量状态。

    刷新 ``updated_at``，通过 ``.tmp`` + ``replace`` 保证原子性。
    返回 ``{"success": bool, "path": str, "count": int}``。
    """
    date = state.get("date", "")
    if not date:
        return {"success": False, "error": "state 缺少 date 字段"}

    state["updated_at"] = _now_iso()
    count = len(state.get("bugs", {}))

    p = _state_file(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(p)  # 原子替换
        logger.info("batch_state %s 已保存 (%d bugs)", date, count)
        return {"success": True, "path": str(p), "count": count}
    except Exception as e:
        # 清理残留 tmp
        tmp.unlink(missing_ok=True)
        logger.error("batch_state %s 保存失败: %s", date, e)
        return {"success": False, "error": str(e)}


def get_pending_bugs(
    state: dict[str, Any],
    all_bug_ids: list[str],
) -> list[str]:
    """从全量 Bug ID 列表滤掉已达终态的，返回待处理 ID 列表。

    Args:
        state: load_batch_state 返回的状态字典。
        all_bug_ids: 本次 zentao_my_bugs 拉到的全量 ID 列表。

    Returns:
        待处理 Bug ID 列表（保持 all_bug_ids 顺序）。
    """
    bugs = state.get("bugs", {})
    pending: list[str] = []
    skipped: list[str] = []
    for bid in all_bug_ids:
        bid_str = str(bid)
        record = bugs.get(bid_str)
        if record and record.get("fix_status") in TERMINAL_STATUSES:
            skipped.append(bid_str)
        else:
            pending.append(bid_str)
    if skipped:
        logger.info(
            "batch_state: 跳过 %d 个终态 Bug (%s)，待处理 %d 个",
            len(skipped), ", ".join(skipped), len(pending),
        )
    return pending


def update_bug_state(
    state: dict[str, Any],
    bug_id: str,
    **fields: Any,
) -> dict[str, Any]:
    """更新单 Bug 的状态字段并返回更新后的 state。

    若 Bug 不在 state 中则自动创建骨架。调用方需自行调 save_batch_state
    落盘（或通过异步包装的 action="update" 自动落盘）。

    可更新字段: title/module/phase/fix_status/task_id/
    reproduce_commands/logcat_filter/gerrit_url/notes。
    """
    bid_str = str(bug_id)
    bugs = state.setdefault("bugs", {})
    if bid_str not in bugs:
        bugs[bid_str] = dict(_BUG_FIELDS)
    record = bugs[bid_str]
    for k, v in fields.items():
        if k in _BUG_FIELDS or k == "updated_at":
            record[k] = v
    record["updated_at"] = _now_iso()
    return state


def reset_batch_state(date: str) -> dict[str, Any]:
    """删除当日状态文件（强制全量重跑）。

    返回 ``{"success": bool, "existed": bool}``。
    """
    p = _state_file(date)
    existed = p.exists()
    if existed:
        p.unlink()
        logger.info("batch_state %s 已删除（reset）", date)
    return {"success": True, "existed": existed}


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """生成给 LLM 看的精简摘要（不含完整 Bug 细节）。"""
    bugs = state.get("bugs", {})
    done = [
        bid for bid, rec in bugs.items()
        if rec.get("fix_status") in TERMINAL_STATUSES
    ]
    in_progress = [
        bid for bid, rec in bugs.items()
        if rec.get("fix_status") not in TERMINAL_STATUSES
    ]
    return {
        "date": state.get("date", ""),
        "current_phase": state.get("current_phase", ""),
        "total": len(bugs),
        "done_count": len(done),
        "pending_count": len(in_progress),
        "done_bug_ids": done,
        "pending_bug_ids": in_progress,
        "retry_counts": {
            bid: bugs.get(bid, {}).get("retry_count", 0)
            for bid in in_progress
        },
        "updated_at": state.get("updated_at", ""),
    }
