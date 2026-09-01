"""misc-mcp — 杂项工具 MCP server（薄入口）。

ZCode 插件自动启动此 server（plugin.json mcpServers.misc-mcp）。
stdio JSON-RPC → bugflow.core 转发，所有能力逻辑在 pip 包的 core 层。

工具列表：
  config_status       — 配置状态概览（哪些已配、哪些缺账号密码）
  config_save         — 写入配置 YAML（自动补预置地址，用户只需提供账号密码）
  plugin_diagnose     — 插件自诊断（检查配置/导入/知识库等常见问题）
  export_excel        — Bug 状态 Excel 导出（批量修复结果可视化）
  excel_sync          — P70 需求清单 Excel 回写（AI 跟踪列：开发状态/Gerrit/验证/完成日期）
  daily_report_submit — 日报提交/查询/删除（内网日报站）

环境变量（orchestrator 注入，ZCode 会话继承 → MCP server 读取）：
  BUGFIX_CONFIG_DIR — 配置目录（~/.bugfix-flow）
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

from bugflow.core.config import get_config_status, save_config
from bugflow.core.plugin_diag import diagnose as _diagnose
from bugflow.core.excel_export import export_bug_status as _export_bug_status
from bugflow.core.excel_sync import excel_sync as _excel_sync
from bugflow.core.daily_report import daily_report as _daily_report


mcp = FastMCP("misc-mcp")


# ── 工具：config_status ───────────────────────────────────────
@mcp.tool()
def config_status() -> str:
    """返回所有配置的状态概览（哪些已配、哪些缺账号密码）。

    检查项：zentao / gerrit / wiki / daily_report / servers / workspace /
    external_sources / embedding / models。

    AI 用此工具判断还缺什么配置，引导用户补充。
    """
    try:
        status = get_config_status()
    except Exception as e:
        return f"❌ 获取配置状态失败: {e}"

    lines = ["📋 配置状态概览\n"]

    # 服务配置
    service_labels = {
        "zentao": "禅道",
        "gerrit": "Gerrit",
        "wiki": "Wiki",
        "daily_report": "日报站",
    }
    for key, label in service_labels.items():
        s = status.get(key, {})
        icon = "✅" if s.get("ready") else "⚠️"
        base = s.get("base_url", "")
        parts = []
        if s.get("has_user"):
            parts.append("user ✓")
        else:
            parts.append("user ✗")
        if s.get("has_password"):
            parts.append("password ✓")
        else:
            parts.append("password ✗")
        if key == "zentao":
            parts.append("MCP token ✓" if s.get("has_mcp") else "MCP token ✗")
        lines.append(f"  {icon} {label} ({base}): {', '.join(parts)}")

    # SSH 服务器
    servers = status.get("servers", {})
    lines.append(f"\n  SSH 服务器 ({len(servers)} 个):")
    for name, cfg in servers.items():
        icon = "✅" if cfg.get("has_user") and cfg.get("has_key_or_password") else "⚠️"
        lines.append(f"    {icon} {name}: {cfg.get('host', '')} "
                     f"(user {'✓' if cfg.get('has_user') else '✗'}, "
                     f"key/pwd {'✓' if cfg.get('has_key_or_password') else '✗'})")

    # Workspace
    ws = status.get("workspace", {})
    icon = "✅" if ws.get("set") else "⚠️"
    lines.append(f"\n  {icon} 工作区: {ws.get('server', '(未设)')} → {ws.get('code_root', '(未设)')}")

    # 外部源码仓库
    ext = status.get("external_sources", {})
    if ext:
        lines.append(f"\n  外部源码仓库 ({len(ext)} 个):")
        for key, info in ext.items():
            icon = "✅" if info.get("configured") else "⚠️"
            lines.append(f"    {icon} {key}: {info.get('path', '(未配)')}")

    # Embedding
    emb = status.get("embedding", {})
    icon = "✅" if emb.get("ready") else "⚠️"
    lines.append(f"\n  {icon} Embedding: {emb.get('base_url', '(未配)')} "
                 f"(api_key {'✓' if emb.get('has_api_key') else '✗'}, "
                 f"model {'✓' if emb.get('has_model') else '✗'})")

    # 模型配置
    models = status.get("models", {})
    if models.get("available"):
        model_list = models.get("models", {})
        lines.append(f"\n  模型配置 ({len(model_list)} 个):")
        for mid, mc in model_list.items():
            ctx_ok = "✓" if mc.get("context_match") else "✗"
            comp_ok = "✓" if mc.get("compaction_ok") else "✗"
            reason_ok = mc.get("reasoning_ok")
            reason_icon = "✓" if reason_ok else ("?" if reason_ok is None else "✗")
            lines.append(f"    {mid}: ctx={mc.get('max_input_length', '?')} "
                         f"(match {ctx_ok}, compaction {comp_ok}), "
                         f"effort={mc.get('reasoning_effort', 'None')} "
                         f"(ok {reason_icon})")
    else:
        lines.append(f"\n  ⚠️ 模型配置: {models.get('error', '不可用')}")

    return "\n".join(lines)


# ── 工具：config_save ─────────────────────────────────────────
@mcp.tool()
def config_save(config_type: str, data: dict) -> str:
    """写入配置 YAML（自动补预置地址，用户只需提供账号密码）。

    内网地址（base_url 等）从 DEFAULTS 自动补入，用户 data 覆盖之。
    写后自动清缓存，后续读取立即生效。

    Args:
        config_type: "zentao" | "gerrit" | "wiki" | "daily_report" | "servers" | "external_source"
        data: 用户提供的字段。
              - zentao/gerrit/wiki/daily_report: {user, password, ...}（base_url 自动补）
              - servers: {"name": "252", "user": "...", "key_file": "...", ...}（host/port 自动补）
              - external_source: {"repo_key": "meiglink", "server": "252", "path": "/home/.../MeiGLink"}
    """
    try:
        result = save_config(config_type, data)
    except ValueError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ 保存配置失败: {e}"

    if result.get("ok"):
        cfg_type = result.get("config_type") or result.get("repo_key") or config_type
        fields = result.get("fields")
        if not fields:
            fields = [k for k in ("repo_key", "server", "path") if k in result]
        return (f"✅ 配置已保存\n"
                f"类型: {cfg_type}\n"
                f"路径: {result.get('path', '')}\n"
                f"字段: {', '.join(fields) if fields else '-'}")
    return f"❌ 保存失败: {result}"


# ── 工具：plugin_diagnose ─────────────────────────────────────
@mcp.tool()
def plugin_diagnose() -> str:
    """运行插件自诊断，返回格式化报告。

    检查项：
    1. 配置完整性：各服务是否 ready
    2. 配置文件字段名：user 不是 username
    3. Core 统一入口函数是否存在
    4. 知识库文件是否存在
    5. plugin.json 一致性
    6. 模块可导入性

    纯本地检查，不需要 SSH 或网络。
    """
    try:
        return _diagnose()
    except Exception as e:
        return f"❌ 诊断失败: {e}"


# ── 工具：export_excel ────────────────────────────────────────
@mcp.tool()
def export_excel(bug_results: list[dict], output_path: str = "") -> str:
    """生成 Bug 状态 Excel 表格（.xlsx）。

    Args:
        bug_results: Bug 结果列表，每项含 bug_id / title / module / repro_status /
                     fix_strategy / fix_status / gerrit_url / notes。
                     缺失字段用空字符串填充。
        output_path: 输出路径（.xlsx）。空则存到当前 workspace 的 reports/ 目录。

    Returns:
        导出结果：成功返回路径+数量，失败返回错误信息。
    """
    if not bug_results:
        return "❌ bug_results 为空，无数据可导出"

    try:
        result = _export_bug_status(bug_results, output_path=output_path)
    except Exception as e:
        return f"❌ 导出失败: {e}"

    if result.get("success"):
        return (f"✅ Excel 已导出\n"
                f"路径: {result['path']}\n"
                f"数量: {result['count']} 个 Bug")
    return f"❌ 导出失败: {result.get('error', '未知错误')}"


# ── 工具：excel_sync ───────────────────────────────────────────
@mcp.tool()
def excel_sync(
    xlsx_path: str,
    feature_id: str = "",
    status: str = "",
    gerrit_url: str = "",
    verify_result: str = "",
    done_date: str = "",
    batch_updates: list[dict] | None = None,
) -> str:
    """P70 需求清单 Excel 回写 — AI 跟踪列更新。

    submit 阶段完成后调用，将 开发状态/Gerrit链接/验证结果/完成日期 回写到
    P70_需求清单_AI版.xlsx。feature_id 为主键（P70-001~P70-798）。

    两种模式：
    - 单条：传 feature_id + status/gerrit_url/verify_result/done_date（非空字段才更新）
    - 批量：传 batch_updates 列表，每项含 feature_id + 4 字段

    Args:
        xlsx_path: P70_需求清单_AI版.xlsx 路径
        feature_id: 单条模式的 feature_id（P70-xxx）
        status: 开发状态（如 待开发/开发中/已提交/已验证/已交付）
        gerrit_url: Gerrit change 链接
        verify_result: 验证结果（如 通过/失败 + 简述）
        done_date: 完成日期 YYYY-MM-DD
        batch_updates: 批量模式更新列表（传此参数时忽略单条参数）
    """
    try:
        return _excel_sync(
            xlsx_path,
            feature_id=feature_id,
            status=status,
            gerrit_url=gerrit_url,
            verify_result=verify_result,
            done_date=done_date,
            batch_updates=batch_updates,
        )
    except Exception as e:
        return f"❌ Excel 回写失败: {e}"


# ── 工具：daily_report_submit ─────────────────────────────────
@mcp.tool()
def daily_report_submit(
    action: str = "submit",
    content: str = "",
    commits: str = "",
    blockers: str = "",
    project: str = "",
    hours: float = 8.0,
    date: str = "",
    date_end: str = "",
    report_id: str = "",
    limit: int = 20,
) -> str:
    """日报工具统一入口（提交/查询/删除/项目列表）。

    Args:
        action: "submit" | "list" | "query" | "delete"
        content: 工作内容（submit 必填）
        commits: 提交记录（submit 必填）
        blockers: 阻塞求助项（submit 用，默认"无"）
        project: 项目名（如"预研项目"）或 value（如"107"），默认"预研项目"
        hours: 工时（submit 用，默认 8）
        date: 日期 YYYY-MM-DD（submit 用默认今天；query 用作起始日期）
        date_end: 结束日期 YYYY-MM-DD（query 用，默认同 date）
        report_id: 日报 ID（delete 必填）
        limit: 最多返回条数（query 用，默认 20）

    action 说明:
    - submit: 提交日报（需 content, commits）
    - list: 查看日报站项目列表
    - query: 查询日报（date=起始, date_end=结束）
    - delete: 删除日报（需 report_id）
    """
    try:
        return _daily_report(
            action=action,
            content=content,
            commits=commits,
            blockers=blockers,
            project=project,
            hours=hours,
            date=date,
            date_end=date_end,
            report_id=report_id,
            limit=limit,
        )
    except Exception as e:
        return f"❌ 日报操作失败: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
