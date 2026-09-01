# -*- coding: utf-8 -*-
"""Bug 状态 Excel 导出（依赖 openpyxl）。

由 orchestrator 在批量解决流程 Phase 5 调用，将每个 Bug 的状态
（不能复现 / 可复现未修复 / 已修复已验证 / 已修复待push / 需人工介入）
写入带样式的 .xlsx 文件，供第二天人工查看。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("core.excel_export")

# 列定义：(key, header, width)
_COLUMNS: list[tuple[str, str, int]] = [
    ("bug_id", "Bug ID", 12),
    ("title", "标题", 40),
    ("module", "模块", 30),
    ("repro_status", "复现状态", 12),
    ("fix_strategy", "修复策略", 14),
    ("fix_status", "修复状态", 18),
    ("gerrit_url", "Gerrit链接", 30),
    ("notes", "备注", 40),
]

# 按修复状态着色（ARGB hex，无 #）
_STATUS_FILLS: dict[str, str] = {
    "已修复已验证": "FFC6EFCE",   # 绿色
    "已修复待push": "FFD9EAD3",   # 浅绿
    "可复现未修复": "FFFFEB9C",   # 黄色
    "不能复现": "FFD9D9D9",       # 灰色
    "需人工介入": "FFFFC7CE",     # 红色
}

_HEADER_FILL = "FF4472C4"  # 蓝色底
_HEADER_FONT_COLOR = "FFFFFFFF"  # 白色字


def export_bug_status(bug_results: list[dict], output_path: str = "") -> dict:
    """生成 Bug 状态 Excel 表格。

    Args:
        bug_results: Bug 结果列表，每项含 bug_id / title / module / repro_status /
                     fix_strategy / fix_status / gerrit_url / notes。
                     缺失字段用空字符串填充。
        output_path: 输出路径（.xlsx）。空则存到当前 workspace 的 reports/ 目录。

    Returns:
        {"success": bool, "path": str, "count": int}

    Raises:
        ImportError: openpyxl 未安装
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return {"success": False, "path": "", "count": 0,
                "error": "openpyxl 未安装，无法导出 Excel。请 pip install openpyxl"}

    if not bug_results:
        return {"success": False, "path": "", "count": 0,
                "error": "bug_results 为空"}

    # 确定输出路径
    if not output_path:
        from .config import get_bugfix_env
        workspace = get_bugfix_env().get("workspace_dir", "")
        if workspace:
            reports_dir = os.path.join(workspace, "reports")
        else:
            reports_dir = os.path.join(os.getcwd(), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d")
        output_path = os.path.join(reports_dir, f"{date_str}-solve.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "Bug 修复状态"

    # 样式对象
    header_font = Font(name="微软雅黑", bold=True, color=_HEADER_FONT_COLOR, size=11)
    header_fill = PatternFill(start_color=_HEADER_FILL, end_color=_HEADER_FILL,
                              fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_font = Font(name="微软雅黑", size=10)
    data_align = Alignment(vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin", color="FFBFBFBF"),
        right=Side(style="thin", color="FFBFBFBF"),
        top=Side(style="thin", color="FFBFBFBF"),
        bottom=Side(style="thin", color="FFBFBFBF"),
    )

    # 写标题行
    for col_idx, (_, header, width) in enumerate(_COLUMNS, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.row_dimensions[1].height = 28

    # 写数据行
    for row_idx, bug in enumerate(bug_results, 2):
        fix_status = str(bug.get("fix_status", ""))
        fill_color = _STATUS_FILLS.get(fix_status)
        row_fill = (PatternFill(start_color=fill_color, end_color=fill_color,
                                fill_type="solid")
                    if fill_color else None)

        for col_idx, (key, _, _) in enumerate(_COLUMNS, 1):
            value = bug.get(key, "")
            # bug_id 转为 int 显示（如果是数字字符串）
            if key == "bug_id" and value:
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    pass
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = data_font
            cell.alignment = data_align
            cell.border = thin_border
            if row_fill:
                cell.fill = row_fill

        ws.row_dimensions[row_idx].height = 22

    # 冻结首行
    ws.freeze_panes = "A2"

    # 保存（确保目录存在）
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    wb.save(output_path)
    logger.info("Excel 已导出: %s (%d 个 Bug)", output_path, len(bug_results))

    return {"success": True, "path": output_path, "count": len(bug_results)}
