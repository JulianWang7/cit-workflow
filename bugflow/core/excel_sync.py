# -*- coding: utf-8 -*-
"""P70 需求清单 Excel 回写 — AI 跟踪列更新。

P70 AI 工程化流水线 OUTPUT 层：submit 阶段完成后，将开发状态/Gerrit链接/验证结果/完成日期
回写到 `文档/P70_需求清单_AI版.xlsx`，供项目侧人工跟踪进度。

设计依据：E:\\P70项目\\P70_AI工程化流水线设计方案.md §2.2
  - 21 列结构，feature_id 为主键（P70-001~P70-798）
  - AI 跟踪列：开发状态 / Gerrit链接 / 验证结果 / 完成日期
  - xlsx 保留人工可读格式，JSON 投影（P70_features.json）由徐翔侧重新生成

依赖 openpyxl（与 excel_export.py 同，懒加载）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger("bugflow.core.excel_sync")

# AI 跟踪列 header（设计 §2.2 最终列结构）
# 用子串匹配容忍轻微空格差异（如 "Gerrit链接" vs "Gerrit 链接"）
_TRACKING_HEADERS: dict[str, list[str]] = {
    "status": ["开发状态"],
    "gerrit_url": ["Gerrit链接", "Gerrit 链接", "Gerrit URL"],
    "verify_result": ["验证结果"],
    "done_date": ["完成日期"],
}

# feature_id 列 header 候选
_FEATURE_ID_HEADERS = ["feature_id", "Feature ID", "feature_id"]


def _find_columns(ws) -> dict[str, int]:
    """扫描首行 header，定位 feature_id + 4 个 AI 跟踪列的列号。

    Returns:
        {key: col_number_1based}，key 为 feature_id/status/gerrit_url/verify_result/done_date
    Raises:
        ValueError: feature_id 列或任一跟踪列未找到
    """
    # 读首行所有单元格文本
    headers: list[str] = []
    for cell in ws[1]:
        headers.append(str(cell.value or "").strip())

    result: dict[str, int] = {}

    # feature_id 列
    for idx, h in enumerate(headers, 1):
        if h in _FEATURE_ID_HEADERS or h.lower() == "feature_id":
            result["feature_id"] = idx
            break
    if "feature_id" not in result:
        raise ValueError(
            f"未找到 feature_id 列（首行 headers: {headers[:10]}...）。"
            "请确认 Excel 为 P70 AI 版格式。"
        )

    # 4 个跟踪列（子串匹配）
    for key, candidates in _TRACKING_HEADERS.items():
        found = False
        for idx, h in enumerate(headers, 1):
            for cand in candidates:
                if cand.replace(" ", "") in h.replace(" ", ""):
                    result[key] = idx
                    found = True
                    break
            if found:
                break
        if not found:
            raise ValueError(
                f"未找到 '{candidates[0]}' 列（首行 headers: {headers}）。"
                f"请确认 Excel 含 AI 跟踪列。"
            )

    return result


def _normalize_feature_id(fid: str) -> str:
    """规范化 feature_id：去空白、大写。P70-001 → P70-001。"""
    return str(fid).strip().upper()


def sync_feature(
    xlsx_path: str,
    feature_id: str,
    status: str = "",
    gerrit_url: str = "",
    verify_result: str = "",
    done_date: str = "",
) -> dict:
    """回写单个 feature 的 AI 跟踪列到 P70 Excel。

    只更新非空字段（空字符串跳过，保留原值）。

    Args:
        xlsx_path: P70_需求清单_AI版.xlsx 路径
        feature_id: P70-xxx（主键）
        status: 开发状态（如 待开发/开发中/已提交/已验证/已交付）
        gerrit_url: Gerrit change 链接
        verify_result: 验证结果（如 通过/失败 + 简述）
        done_date: 完成日期 YYYY-MM-DD

    Returns:
        {"success": bool, "feature_id": str, "updated_fields": list, "row": int, "path": str}
    Raises:
        ValueError: Excel 格式不符 / feature_id 不存在
        ImportError: openpyxl 未安装
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {
            "success": False,
            "error": "openpyxl 未安装，无法回写 Excel。请 pip install openpyxl",
        }

    if not os.path.isfile(xlsx_path):
        return {"success": False, "error": f"Excel 文件不存在: {xlsx_path}"}

    updates = {
        "status": status,
        "gerrit_url": gerrit_url,
        "verify_result": verify_result,
        "done_date": done_date,
    }
    # 过滤空值
    updates = {k: v for k, v in updates.items() if str(v).strip()}
    if not updates:
        return {"success": False, "error": "所有字段为空，无数据可回写"}

    wb = load_workbook(xlsx_path)
    ws = wb.active

    try:
        cols = _find_columns(ws)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    target_fid = _normalize_feature_id(feature_id)
    fid_col = cols["feature_id"]

    # 遍历数据行查找匹配 feature_id
    matched_row = -1
    for row_idx in range(2, ws.max_row + 1):
        cell_val = str(ws.cell(row=row_idx, column=fid_col).value or "").strip()
        if _normalize_feature_id(cell_val) == target_fid:
            matched_row = row_idx
            break

    if matched_row < 0:
        return {
            "success": False,
            "error": f"feature_id '{feature_id}' 不存在于 Excel（扫描 {ws.max_row - 1} 行）",
        }

    # 写入非空字段
    updated_fields: list[str] = []
    for key, value in updates.items():
        col = cols[key]
        ws.cell(row=matched_row, column=col, value=str(value).strip())
        updated_fields.append(key)

    wb.save(xlsx_path)
    logger.info(
        "Excel 回写: %s row=%d fields=%s → %s",
        target_fid, matched_row, updated_fields, xlsx_path,
    )

    return {
        "success": True,
        "feature_id": target_fid,
        "updated_fields": updated_fields,
        "row": matched_row,
        "path": xlsx_path,
    }


def sync_batch(xlsx_path: str, updates: list[dict]) -> dict:
    """批量回写多个 feature 的 AI 跟踪列。

    一次打开/保存 Excel，遍历 updates 列表逐行更新，性能优于多次调 sync_feature。

    Args:
        xlsx_path: P70_需求清单_AI版.xlsx 路径
        updates: [{"feature_id": "P70-001", "status": "...", "gerrit_url": "...",
                   "verify_result": "...", "done_date": "..."}, ...]

    Returns:
        {"success": bool, "total": int, "updated": int, "skipped": list, "path": str}
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {"success": False, "error": "openpyxl 未安装"}

    if not os.path.isfile(xlsx_path):
        return {"success": False, "error": f"Excel 文件不存在: {xlsx_path}"}
    if not updates:
        return {"success": False, "error": "updates 列表为空"}

    wb = load_workbook(xlsx_path)
    ws = wb.active

    try:
        cols = _find_columns(ws)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    fid_col = cols["feature_id"]

    # 构建 feature_id → row 索引（一次扫描）
    fid_to_row: dict[str, int] = {}
    for row_idx in range(2, ws.max_row + 1):
        cell_val = str(ws.cell(row=row_idx, column=fid_col).value or "").strip()
        if cell_val:
            fid_to_row[_normalize_feature_id(cell_val)] = row_idx

    updated_count = 0
    skipped: list[dict] = []
    field_keys = ["status", "gerrit_url", "verify_result", "done_date"]

    for upd in updates:
        fid = _normalize_feature_id(upd.get("feature_id", ""))
        if not fid:
            skipped.append({"feature_id": upd.get("feature_id", ""), "reason": "feature_id 为空"})
            continue
        row_idx = fid_to_row.get(fid)
        if row_idx is None:
            skipped.append({"feature_id": fid, "reason": "不存在于 Excel"})
            continue

        wrote_any = False
        for key in field_keys:
            value = str(upd.get(key, "")).strip()
            if value:
                ws.cell(row=row_idx, column=cols[key], value=value)
                wrote_any = True
        if wrote_any:
            updated_count += 1
        else:
            skipped.append({"feature_id": fid, "reason": "所有字段为空"})

    wb.save(xlsx_path)
    logger.info(
        "Excel 批量回写: %d/%d 更新, %d 跳过 → %s",
        updated_count, len(updates), len(skipped), xlsx_path,
    )

    return {
        "success": True,
        "total": len(updates),
        "updated": updated_count,
        "skipped": skipped,
        "path": xlsx_path,
    }


def _format_sync_result(result: dict) -> str:
    """格式化 sync_feature / sync_batch 结果为可读文本。"""
    if not result.get("success"):
        return f"❌ {result.get('error', '未知错误')}"
    if "updated" in result:  # batch
        lines = [
            f"✅ 批量回写完成",
            f"  总数: {result['total']}",
            f"  已更新: {result['updated']}",
            f"  路径: {result['path']}",
        ]
        skipped = result.get("skipped", [])
        if skipped:
            lines.append(f"  跳过 ({len(skipped)}):")
            for s in skipped[:10]:
                lines.append(f"    {s.get('feature_id', '?')}: {s.get('reason', '?')}")
            if len(skipped) > 10:
                lines.append(f"    ... 还有 {len(skipped) - 10} 条")
        return "\n".join(lines)
    else:  # single
        return (
            f"✅ 已回写 feature {result['feature_id']}\n"
            f"  行: {result['row']}\n"
            f"  字段: {', '.join(result['updated_fields'])}\n"
            f"  路径: {result['path']}"
        )


def excel_sync(
    xlsx_path: str,
    feature_id: str = "",
    status: str = "",
    gerrit_url: str = "",
    verify_result: str = "",
    done_date: str = "",
    batch_updates: list[dict] | None = None,
) -> str:
    """Excel 回写统一入口（单条 / 批量）。

    submit 阶段完成后调用，将 AI 跟踪列回写到 P70_需求清单_AI版.xlsx。
    - 单条：传 feature_id + status/gerrit_url/verify_result/done_date（非空字段才更新）
    - 批量：传 batch_updates 列表（每项含 feature_id + 4 字段）

    Args:
        xlsx_path: P70_需求清单_AI版.xlsx 路径
        feature_id: 单条模式的 feature_id（P70-xxx）
        status: 开发状态
        gerrit_url: Gerrit 链接
        verify_result: 验证结果
        done_date: 完成日期 YYYY-MM-DD
        batch_updates: 批量模式更新列表（传此参数时忽略单条参数）
    """
    if batch_updates:
        result = sync_batch(xlsx_path, batch_updates)
    else:
        if not feature_id.strip():
            return "❌ 请指定 feature_id（单条模式）或 batch_updates（批量模式）"
        result = sync_feature(
            xlsx_path,
            feature_id=feature_id,
            status=status,
            gerrit_url=gerrit_url,
            verify_result=verify_result,
            done_date=done_date,
        )
    return _format_sync_result(result)
