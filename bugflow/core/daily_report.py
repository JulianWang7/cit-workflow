# -*- coding: utf-8 -*-
"""日报工具 — HTTP 登录 + 表单提交/查询/删除到内部日报站。

真实站点结构（经 2026-08-09 端到端探查确认）:
- 登录页: GET /report_new.php （未登录返回含 <form action="login.php"> 的登录表单）
- 登录:   POST /login.php   字段 name / pswd / effect(hidden) / submit="登入"
- 新建:   GET /report_new.php → 提取 effect + project 下拉选项（329 个）
- 提交:   POST /report_new.php  字段 date / project(value=数字) / cost / content / comit / block / complete / effect
- 查询:   POST /report_query.php 字段 date_begin / date_end / project / user(=DB id) / MM_insert="form1"
- 删除:   GET /report_delete.php?report_id=N  → alert('日报删除成功')

注意: 站点字段 comit（非 commits）、cost（非 hours）、block（非 blockers）。
HttpClient 用 auth_mode="basic"，避免 form 模式自动 POST /login/ 404。

配置: ~/.bugfix-flow/daily_report.yaml
  daily_report:
    base_url: http://192.168.0.18/daily
    user: your-username
    password: your-password
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .http_client import HttpClient
from .config import load_daily_report_config

logger = logging.getLogger("core.daily_report")


class DailyReportError(Exception):
    """日报操作错误。"""


# 单例 client
_client: HttpClient | None = None


def _get_client() -> HttpClient:
    global _client
    if _client is not None:
        return _client
    cfg = load_daily_report_config()
    _client = HttpClient(
        base_url=cfg.get("base_url", "http://192.168.0.18/daily").rstrip("/"),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        auth_mode="basic",          # 不让 HttpClient 自动 POST /login/（Gerrit 式，日报站 404）
        verify_ssl=False,
        cookie_file="daily_report",
    )
    return _client


def _is_login_form(html: str) -> bool:
    """页面是否为登录表单（未登录标志）。"""
    return "login.php" in html and "pswd" in html


def _ensure_login() -> bool:
    """登录日报站：GET /report_new.php → 提取 effect → POST /login.php → 验证。

    cookie 过期自动恢复：若 _logged_in=True（cookie 加载）但实际 GET 到登录表单，
    重置 _logged_in 并重新走登录流程。
    """
    client = _get_client()
    if not client.user or not client.password:
        raise DailyReportError(
            "日报站未配置。请在 ~/.bugfix-flow/daily_report.yaml 中配置:\n"
            "  daily_report:\n"
            "    base_url: \"http://192.168.0.18/daily\"\n"
            "    user: \"your-username\"\n"
            "    password: \"your-password\""
        )
    try:
        # 1. GET 新建页（未登录返回登录表单，含 hidden effect）
        page = client.get("/report_new.php", timeout=15)
        effect = _extract_hidden(page, "effect")

        if not _is_login_form(page):
            # 已登录（cookie 有效），直接放行
            client.logged_in = True
            client.save_cookies()
            return True

        # cookie 过期或未登录 → 重置标志，走登录流程
        client.logged_in = False

        # 2. POST 登录
        resp = client.post_form("/login.php", fields={
            "name": client.user,
            "pswd": client.password,
            "effect": effect,
            "submit": "登入",
        }, timeout=15)

        # 3. 验证：再 GET /report_new.php，若仍是登录表单则失败
        verify = client.get("/report_new.php", timeout=15)
        if _is_login_form(verify):
            raise DailyReportError("日报站登录失败: 用户名或密码错误")

        client.logged_in = True
        client.save_cookies()
        return True

    except DailyReportError:
        raise
    except Exception as e:
        raise DailyReportError(f"日报站登录失败: {e}") from e


# ── HTML 解析辅助 ────────────────────────────────────────────────


def _extract_hidden(html: str, name: str) -> str:
    """提取 <input type="hidden" name="X" value="V"> 的 value。"""
    m = re.search(
        rf'<input\s+[^>]*type=["\']?hidden["\']?[^>]*name=["\']?{re.escape(name)}["\']?[^>]*value=["\']?([^"\'>\s]*)',
        html, re.I,
    )
    if not m:
        m = re.search(
            rf'<input\s+[^>]*name=["\']?{re.escape(name)}["\']?[^>]*type=["\']?hidden["\']?[^>]*value=["\']?([^"\'>\s]*)',
            html, re.I,
        )
    return m.group(1) if m else ""


def _parse_project_options(html: str) -> dict[str, str]:
    """解析 <select name="project"> 的 <option value="N">名称</option>。

    Returns: {名称: value}
    """
    options = re.findall(
        r'<option\s+value=["\']?([^"\'>\s]+)["\']?[^>]*>([^<]+)</option>',
        html, re.I,
    )
    return {name.strip(): val for val, name in options if name.strip()}


def _resolve_project(project: str, html: str) -> str:
    """project 参数解析：传 value(数字)直接用；传名称查映射表。

    Args:
        project: 项目名称（如"预研项目"）或 value（如"107"）
        html: 当前页面 HTML（用于解析 options，每次从实际页面解析，避免跨页面缓存污染）
    Returns: project value（数字字符串）
    """
    if not project:
        project = "预研项目"
    # 纯数字 → 直接当 value
    if project.isdigit():
        return project
    # 每次从传入的 HTML 解析（不缓存，避免 query 页和 new 页的 option 列表不一致）
    proj_map = _parse_project_options(html)
    val = proj_map.get(project)
    if val:
        return val
    # 宽松匹配：包含
    for name, v in proj_map.items():
        if project in name or name in project:
            return v
    # 找不到，返回原值让站点报错（便于排查）
    return project


def _strip_tags(s: str) -> str:
    """去 HTML 标签 + 折叠空白。"""
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


# ── 项目列表 ────────────────────────────────────────────────────


def list_projects() -> str:
    """获取日报站项目下拉列表。"""
    _ensure_login()
    client = _get_client()
    try:
        page = client.get("/report_new.php", timeout=15)
        proj_map = _parse_project_options(page)
        if not proj_map:
            return "⚠️ 无法解析项目列表，请检查日报站页面结构。"

        lines = [f"日报项目列表 ({len(proj_map)} 个):\n"]
        for i, (name, val) in enumerate(proj_map.items()):
            if i >= 50:
                lines.append(f"  … 另有 {len(proj_map) - 50} 个")
                break
            lines.append(f"  - {val}: {name}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 获取项目列表失败: {e}"


# ── 提交日报 ────────────────────────────────────────────────────


def submit_report(
    date: str = "",
    project: str = "",
    hours: float = 8.0,
    content: str = "",
    commits: str = "",
    blockers: str = "",
) -> str:
    """提交日报。

    Args:
        date: 日期 YYYY-MM-DD，空则用今天
        project: 项目名（如"预研项目"）或 value（如"107"），默认"预研项目"
        hours: 工时（提交到 cost 字段）
        content: 工作内容（必填）
        commits: 提交记录（必填，提交到 comit 字段）
        blockers: 阻塞求助项（提交到 block 字段，无则填"无"）

    Returns:
        提交结果消息
    """
    # 必填校验在登录之前（避免无效请求浪费网络往返）
    if not content:
        return "❌ 工作内容为必填项"
    if not commits:
        return "❌ 提交记录为必填项"
    # 工时合理性校验（站点会拒负数/0）
    try:
        hours_val = float(hours)
    except (TypeError, ValueError):
        return f"❌ 工时必须是数字，收到: {hours}"
    if hours_val <= 0 or hours_val > 24:
        return f"❌ 工时需大于 0 且不超过 24，收到: {hours_val}"

    _ensure_login()
    client = _get_client()

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    if not blockers:
        blockers = "无"

    try:
        # GET 新建页 → 提取 effect + 解析 project
        new_page = client.get("/report_new.php", timeout=15)
        effect = _extract_hidden(new_page, "effect")
        project_val = _resolve_project(project, new_page)

        # 构建提交表单（真实字段名: cost/comit/block/complete）
        fields = {
            "effect": effect,
            "date": date,
            "project": project_val,
            "cost": str(hours),
            "complete": "100",
            "content": content,
            "comit": commits,
            "block": blockers,
        }

        resp = client.post_form("/report_new.php", fields=fields, timeout=15)

        # 成功判定：站点提交后返回新建页（空表单）或含"成功/插入"
        if _is_login_form(resp):
            return f"❌ 日报提交失败: 登录态丢失，请重新配置"
        if any(kw in resp for kw in ("成功", "插入", "insert", "success")):
            return f"✅ 日报已提交 ({date})\n项目: {project or '预研项目'}\n工时: {hours}h"

        # 报错判定
        if any(kw in resp for kw in ("失败", "错误", "error", "alert('")):
            # 提取 alert 消息
            alert_m = re.search(r"alert\(['\"]([^'\"]+)['\"]\)", resp)
            if alert_m:
                return f"❌ 日报提交失败: {alert_m.group(1)}"
            err_m = re.search(r'(?:失败|错误|error)[:：]?\s*([^\n<]+)', resp, re.I)
            err_msg = err_m.group(1).strip() if err_m else "未知错误"
            return f"❌ 日报提交失败: {err_msg}"

        # 提交成功后站点通常回到新建页（含 report_new.php 表单）。
        # 不用 "content" in resp 判定（表单回显页也含 content，过宽会误判）。
        if "report_new.php" in resp:
            return f"✅ 日报已提交 ({date})\n项目: {project or '预研项目'}\n工时: {hours}h"

        # 不确定
        plain = _strip_tags(resp)[:500]
        return f"⚠️ 日报提交结果不确定。响应片段:\n{plain or '(空响应)'}"

    except DailyReportError:
        raise
    except Exception as e:
        return f"❌ 日报提交失败: {e}"


def submit_daily_report(
    content: str,
    commits: str = "",
    blockers: str = "无",
    project: str = "",
    hours: float = 8.0,
    date: str = "",
) -> str:
    """提交日报（统一入口，content 为必填）。

    Args:
        content: 工作内容（必填）
        commits: 提交记录（必填，可空但建议填）
        blockers: 阻塞求助项（默认"无"）
        project: 项目名（默认"预研项目"）
        hours: 工时（默认 8）
        date: 日期 YYYY-MM-DD（默认今天）
    """
    return submit_report(
        date=date, project=project, hours=hours,
        content=content, commits=commits, blockers=blockers,
    )


# ── 查询日报 ────────────────────────────────────────────────────


def _post_query_form(
    date_begin: str = "", date_end: str = "", project: str = "",
) -> str:
    """查询日报的公共 POST 逻辑：GET 查询页 → 提取 hidden → POST 查询。

    query_reports（格式化文本）与 list_report_ids（id 列表）共用此函数，
    避免重复实现 GET/extract/POST 序列。

    Returns: 查询结果页 HTML。
    """
    _ensure_login()
    client = _get_client()

    if not date_begin:
        date_begin = datetime.now().strftime("%Y-%m-%d")
    if not date_end:
        date_end = date_begin

    qpage = client.get("/report_query.php", timeout=15)
    user_id = _extract_hidden(qpage, "user")
    # user_id 找不到时仍提交（站点可能默认当前用户）
    proj_val = _resolve_project(project, qpage) if project else ""
    fields = {
        "effect": _extract_hidden(qpage, "effect"),
        "date_begin": date_begin,
        "date_end": date_end,
        "project": proj_val,
        "orderby": "",
        "show_repeat": "",
        "user": user_id,
        "MM_insert": "form1",
    }
    return client.post_form("/report_query.php", fields=fields, timeout=15)


def query_reports(
    date_begin: str = "",
    date_end: str = "",
    project: str = "",
    limit: int = 20,
) -> str:
    """查询日报列表。

    Args:
        date_begin: 起始日期 YYYY-MM-DD（默认今天）
        date_end: 结束日期 YYYY-MM-DD（默认今天）
        project: 项目名过滤（空=全部）。传 value 或名称均可
        limit: 最多返回条数

    Returns:
        格式化日报列表: report_id | 日期 | 项目 | 工时 | 内容摘要
    """
    try:
        resp = _post_query_form(date_begin, date_end, project)

        if _is_login_form(resp):
            return "❌ 查询失败: 登录态丢失"

        rows = _parse_query_rows(resp)
        if not rows:
            return f"📭 {date_begin}~{date_end} 无日报记录"

        lines = [f"📋 日报列表 ({date_begin} ~ {date_end})，共 {len(rows)} 条:\n"]
        for i, row in enumerate(rows[:limit]):
            rid = row.get("report_id", "?")
            date = row.get("date", "")
            proj = row.get("project", "")
            cost = row.get("cost", "")
            content = row.get("content", "")[:40]
            lines.append(f"  [{rid}] {date} | {proj} | {cost}h | {content}")
        if len(rows) > limit:
            lines.append(f"  … 另有 {len(rows) - limit} 条")
        return "\n".join(lines)

    except DailyReportError:
        raise
    except Exception as e:
        return f"❌ 查询日报失败: {e}"


def _parse_query_rows(html: str) -> list[dict]:
    """解析查询结果表格行。

    每行结构: report_id(从删除链接) + td[0-10]
    td[3]=日期, td[4]=项目, td[5]=工时, td[7]=内容, td[8]=提交记录, td[9]=阻塞
    """
    rows = []
    # 按 <tr 分块
    trs = re.split(r'<tr[^>]*>', html, flags=re.I)
    for tr in trs:
        if "report_delete" not in tr:
            continue
        rid_m = re.search(r'report_delete\.php\?report_id=(\d+)', tr)
        if not rid_m:
            continue
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.I | re.S)
        if len(tds) < 8:
            continue
        row = {
            "report_id": rid_m.group(1),
            "user": _strip_tags(tds[0]) if len(tds) > 0 else "",
            "name": _strip_tags(tds[1]) if len(tds) > 1 else "",
            "date": _strip_tags(tds[3]) if len(tds) > 3 else "",
            "project": _strip_tags(tds[4]) if len(tds) > 4 else "",
            "cost": _strip_tags(tds[5]) if len(tds) > 5 else "",
            "content": _strip_tags(tds[7]) if len(tds) > 7 else "",
            "commits": _strip_tags(tds[8]) if len(tds) > 8 else "",
            "blockers": _strip_tags(tds[9]) if len(tds) > 9 else "",
        }
        rows.append(row)
    return rows


def list_report_ids(
    date_begin: str = "",
    date_end: str = "",
    project: str = "",
) -> list[str]:
    """查询并返回 report_id 列表（供删除/验证用）。

    Returns: ["329773", "329774", ...]
    """
    try:
        resp = _post_query_form(date_begin, date_end, project)
        return [r["report_id"] for r in _parse_query_rows(resp)]
    except DailyReportError:
        raise
    except Exception as e:
        logger.warning("list_report_ids 失败: %s", e)
        return []


# ── 删除日报 ────────────────────────────────────────────────────


def delete_report(report_id) -> str:
    """删除指定日报。

    Args:
        report_id: 日报 ID（int 或 str）

    Returns:
        删除结果消息
    """
    rid = str(report_id).strip()
    if not rid.isdigit():
        return f"❌ 无效的 report_id: {report_id}"

    _ensure_login()
    client = _get_client()
    try:
        resp = client.get(f"/report_delete.php?report_id={rid}", timeout=15)
        # 登录态丢失优先判定
        if _is_login_form(resp):
            return f"❌ 删除失败: 登录态丢失"
        # 明确报错关键词优先判定（避免"删除失败...成功处理"等含双关键词的误判）
        if any(kw in resp for kw in ("失败", "错误", "error", "不存在")):
            alert_m = re.search(r"alert\(['\"]([^'\"]+)['\"]\)", resp)
            msg = alert_m.group(1) if alert_m else "未知错误"
            return f"❌ 日报 {rid} 删除失败: {msg}"
        # 明确成功关键词
        if "删除成功" in resp:
            return f"✅ 日报 {rid} 已删除"
        # 宽松判定：无报错也无登录表单 → 视为成功（站点删除后通常返回空页或 alert）
        return f"✅ 日报 {rid} 已删除"
    except DailyReportError:
        raise
    except Exception as e:
        return f"❌ 日报 {rid} 删除失败: {e}"


# ── 统一入口 ────────────────────────────────────────────────────


def daily_report(
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
    """统一日报工具入口（供 tools/daily_report_tools.py 调用）。

    action:
    - submit: 提交日报（需 content, commits; 可选 project, hours, blockers, date）
    - list: 查看日报站项目列表
    - query: 查询日报（date=起始日期, date_end=结束日期; 可选 project, limit）
    - delete: 删除日报（需 report_id）
    """
    act = (action or "").strip().lower()
    if act == "submit":
        return submit_daily_report(
            content=content, commits=commits, blockers=blockers or "无",
            project=project or "预研项目", hours=hours, date=date,
        )
    if act == "list":
        return list_projects()
    if act == "query":
        return query_reports(date_begin=date, date_end=date_end, project=project, limit=limit)
    if act == "delete":
        return delete_report(report_id)
    return f"❌ 未知 action: {action}。支持: submit, list, query, delete"
