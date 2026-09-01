# -*- coding: utf-8 -*-
"""Gerrit 工具 — 搜索提交、查看 Change、Gitiles 远程读码、发表评论。

从 YWAgent tools/gerrit.py 移植，去掉 YWAgent 框架依赖。
使用 core/http_client.HttpClient 替代 YWAgent 的 HttpClient。

Gerrit REST API:
- 搜索 Changes: GET /changes/?q=<query>&n=<limit>&o=CURRENT_REVISION
- Change 详情:  GET /changes/{change-id}/detail
- 变更文件:     GET /changes/{change-id}/revisions/current/files
- 文件 diff:    GET /changes/{change-id}/revisions/current/files/{path}/diff
- Patch:        GET /changes/{change-id}/revisions/current/patch (base64)
- 评论/打分:    POST /changes/{change-id}/revisions/current/review

Gitiles（不依赖本地拉码）:
- 列目录: GET /plugins/gitiles/{repo}/+/refs/heads/{branch}/{path}?format=JSON
- 读文件: GET /plugins/gitiles/{repo}/+/refs/heads/{branch}/{path}?format=TEXT (base64)

配置: ~/.bugfix-flow/gerrit.yaml
  gerrit:
    base_url: https://192.168.0.240
    user: your-username
    password: 'your-password'
    auth_mode: basic_session  # HTTP 鉴权 Gerrit 默认
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
import urllib.error
from urllib.parse import quote

from .http_client import HttpClient
from .config import load_gerrit_config


# 远程文件读取限制
_CAT_DEFAULT_MAX_CHARS = 60000
_CAT_HARD_MAX_CHARS = 120000
_LS_MAX_ENTRIES = 200
_DIFF_DEFAULT_CONTEXT = 3

# 单例 client
_client_cache: HttpClient | None = None
_client_cache_key: tuple | None = None


class GerritError(Exception):
    """Gerrit 操作错误。"""


def _is_http_status(e: BaseException, code: int) -> bool:
    """精确判断 HTTP 错误码，替代脆弱的 '401' in str(e) 字符串匹配。"""
    return isinstance(e, urllib.error.HTTPError) and e.code == code


def _curl_bin() -> str:
    import shutil
    import sys
    if sys.platform.startswith("win"):
        return shutil.which("curl.exe") or "curl.exe"
    return shutil.which("curl") or "curl"


def _get_client() -> HttpClient:
    """构建/复用 Gerrit HttpClient。"""
    global _client_cache, _client_cache_key
    cfg = load_gerrit_config()
    base_url = cfg.get("base_url", "")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    auth_mode = (cfg.get("auth_mode") or "basic_session").strip() or "basic_session"
    key = (base_url, user, password, auth_mode)
    if _client_cache is not None and _client_cache_key == key:
        return _client_cache
    _client_cache = HttpClient(
        base_url=base_url,
        user=user,
        password=password,
        auth_mode=auth_mode,
        verify_ssl=False,
        cookie_file="gerrit",
        # Gerrit HTTP Credentials 站点 REST 须走 /a/ 前缀：直连 401 时自动重试
        auto_a_prefix=True,
    )
    _client_cache_key = key
    return _client_cache


def _require_client() -> HttpClient:
    client = _get_client()
    if not client.base_url:
        raise GerritError(
            "Gerrit 未配置。请在 ~/.bugfix-flow/gerrit.yaml 中配置:\n"
            "  gerrit:\n"
            "    base_url: \"https://your-gerrit-server.com\"\n"
            "    user: \"your-username\"\n"
            "    password: \"your-password\"\n"
            "    auth_mode: \"basic_session\""
        )
    return client


_GERRIT_401_HINT = (
    "⚠️ Gerrit 认证失败 (401)，已自动尝试直连与 /a/ 前缀均被拒。\n"
    "   请检查 gerrit.yaml 中的 user/password：\n"
    "   - HTTP Credentials（Settings→HTTP Credentials 生成的随机串）需配合 /a/ 前缀（已自动尝试）\n"
    "   - nginx/LDAP 账号密码走直连；确认密码未过期、账号未被锁"
)


def _test_gerrit_auth(client: HttpClient) -> str:
    """快速连通性测试。"""
    try:
        client.get_json("/changes/", params={"q": "status:open", "n": "1"}, timeout=10)
        return ""
    except Exception as e:
        msg = str(e)
        if "getaddrinfo" in msg.lower() or "Name or service" in msg.lower():
            return f"⚠️ 无法解析 Gerrit 地址: {client.base_url}"
        if "Connection refused" in msg.lower() or "Connection reset" in msg.lower():
            return f"⚠️ 无法连接 Gerrit: {client.base_url}"
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        if _is_http_status(e, 403):
            return f"⚠️ Gerrit 403 Forbidden，用户 {client.user} 可能无权限"
        return f"⚠️ Gerrit 连接失败: {msg[:200]}"


# ── URL helpers ────────────────────────────────────────────────

def _quote_project(repo: str) -> str:
    return quote(repo.strip().strip("/"), safe="")


def _normalize_ref(branch: str) -> str:
    b = (branch or "").strip().strip("/")
    if not b:
        return ""
    if b.startswith("refs/") or len(b) >= 40:
        return b
    return f"refs/heads/{b}"


def _gitiles_api_path(repo: str, ref: str, path: str = "") -> str:
    repo_q = _quote_project(repo)
    ref_q = quote(ref, safe="/")
    path = (path or "").strip().strip("/")
    if path:
        return f"/plugins/gitiles/{repo_q}/+/{ref_q}/{quote(path, safe='/')}"
    return f"/plugins/gitiles/{repo_q}/+/{ref_q}/"


def _strip_xssi(text: str) -> str:
    return text[4:] if text.startswith(")]}'") else text


# ── Query normalization ────────────────────────────────────────

_SCOPE_FILTER_RE = re.compile(
    r"\b((?:branch|project|status|file|after|before|topic|label):(?:\"[^\"]+\"|\S+))",
    re.I,
)
_OR_SCOPE_FIX_RE = re.compile(
    r"^(.+\sOR\s.+?)(\s+(?:branch|project|status|file|after|before|topic|label):\S.*)$",
    re.I,
)


def _normalize_gerrit_query(query: str) -> tuple[str, str | None]:
    q = " ".join((query or "").split())
    if not q or " OR " not in q:
        return q, None
    m = _OR_SCOPE_FIX_RE.match(q)
    if not m:
        return q, None
    left, right = m.group(1).strip(), m.group(2)
    if left.startswith("(") and left.endswith(")"):
        return q, None
    normalized = f"({left}){right}"
    return normalized, f"已自动改写查询（OR 须加括号）: {q} → {normalized}"


def _query_wanted_scopes(query: str) -> tuple[set[str], set[str]]:
    branches: set[str] = set()
    projects: set[str] = set()
    for raw in _SCOPE_FILTER_RE.findall(query or ""):
        key, _, val = raw.partition(":")
        val = val.strip().strip('"')
        k = key.lower()
        if k == "branch" and val:
            branches.add(val)
        elif k == "project" and val:
            projects.add(val)
    return branches, projects


def _change_scope_mismatch(change: dict, branches: set[str], projects: set[str]) -> bool:
    if branches and (change.get("branch") or "") not in branches:
        return True
    if projects and (change.get("project") or "") not in projects:
        return True
    return False


# ── Formatting ─────────────────────────────────────────────────

def _format_reviewers(reviewers: dict | None) -> str:
    """格式化 reviewers dict（REVIEWER/CC → 账号列表）为简短串。"""
    if not isinstance(reviewers, dict) or not reviewers:
        return ""
    parts = []
    for role, accounts in reviewers.items():
        if not isinstance(accounts, list) or not accounts:
            continue
        names = [a.get("name") or a.get("username") or a.get("_account_id", "?") for a in accounts]
        tag = "审查" if role == "REVIEWER" else ("CC" if role == "CC" else role)
        parts.append(f"{tag}: {', '.join(str(n) for n in names)}")
    return " | ".join(parts)


def _format_messages(messages: list | None, limit: int = 5) -> list[str]:
    """格式化 change 评论历史为多行（取最近 limit 条）。"""
    if not isinstance(messages, list) or not messages:
        return []
    out = []
    recent = messages[-limit:] if len(messages) > limit else messages
    for m in recent:
        if not isinstance(m, dict):
            continue
        author = (m.get("author") or {}).get("name", "?")
        msg = (m.get("message") or "").strip().replace("\n", " ")
        if len(msg) > 120:
            msg = msg[:117] + "…"
        out.append(f"  • [{author}] {msg}")
    return out


def _format_labels(labels: dict | None) -> str:
    """格式化 Gerrit labels 为简短状态串，如 'Code-Review:+2 Verified:+1'。

    DETAILED_LABELS 选项下 label info 含 approved/recommended/disliked/rejected
    字段，分别对应 +2/+1/-1/-2（最高分优先）。
    """
    if not isinstance(labels, dict) or not labels:
        return ""
    parts = []
    for name, info in labels.items():
        if not isinstance(info, dict):
            parts.append(str(name))
            continue
        score = ""
        if info.get("approved"):
            score = "+2"
        elif info.get("recommended"):
            score = "+1"
        elif info.get("disliked"):
            score = "-1"
        elif info.get("rejected"):
            score = "-2"
        parts.append(f"{name}:{score}" if score else str(name))
    return " ".join(parts)


def _format_change(change: dict, *, branch_mismatch: bool = False) -> str:
    warn = " ⚠分支不符" if branch_mismatch else ""
    lines = [
        f"Change {change.get('_number', '?')}: {change.get('subject', '无标题')}{warn}",
        f"  状态: {change.get('status', '?')} | 分支: {change.get('branch', '?')}",
        f"  项目: {change.get('project', '?')}",
        f"  作者: {change.get('owner', {}).get('name', '?')} ({change.get('owner', {}).get('email', '?')})",
        f"  更新时间: {change.get('updated', '?')}",
    ]
    if change.get("insertions") or change.get("deletions"):
        lines.append(f"  变更: +{change.get('insertions', 0)}/-{change.get('deletions', 0)}")
    label_str = _format_labels(change.get("labels"))
    if label_str:
        lines.append(f"  审查: {label_str}")
    return "\n".join(lines)


def _fetch_change_files(client: HttpClient, change_id: str) -> dict[str, Any]:
    cid_enc = quote(str(change_id), safe="")
    try:
        data = client.get_json(f"/changes/{cid_enc}/revisions/current/files")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_line_range(lines: str, total_lines: int) -> tuple[int | None, int | None]:
    spec = (lines or "").strip().replace(" ", "")
    if not spec:
        return None, None
    m = re.match(r"^(\d+)[,:\-](\d+)?$", spec)
    if not m:
        return None, None
    start = max(1, int(m.group(1)))
    end = int(m.group(2)) if m.group(2) else (total_lines or start)
    if total_lines > 0:
        start = min(start, total_lines)
        end = min(end, total_lines)
    return start, max(start, end)


# ── curl fallback ──────────────────────────────────────────────

def _curl_request(
    target_url: str, cred_cfg: str, cookie_file: Path, timeout: int,
) -> tuple[str, int]:
    """单次 curl 请求（basic auth + cookie），返回 (body, http_code)。

    用 -w 追加 HTTPCODE 行以取状态码，便于区分 401（需 /a/ 重试）与其它失败。
    """
    curl = _curl_bin()
    try:
        result = subprocess.run(
            [curl, "-k", "-s", "-w", "\\nHTTPCODE:%{http_code}", "--config", "-",
             "-c", str(cookie_file), "-b", str(cookie_file), target_url],
            input=cred_cfg,
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        return "", 0
    if result.returncode != 0:
        return "", 0
    out = result.stdout or ""
    m = re.search(r"\nHTTPCODE:(\d+)$", out)
    code = int(m.group(1)) if m else 0
    text = out[: m.start()] if m else out
    return text.strip(), code


def _curl_candidate_paths(path: str) -> list[str]:
    """curl 要尝试的路径：直连 + /a/ 前缀（与 HttpClient._a_prefix_for 一致）。

    /plugins/（Gitiles）不走 /a/（插件不识别 /a/，会 404）；已带 a/ 的不重复。
    """
    p = (path or "").lstrip("/")
    cands = [f"/{p}"]
    if p and not p.startswith("a/") and not p.startswith("plugins/"):
        cands.append(f"/a/{p}")
    return cands


def _curl_gerrit_api(path: str, params: dict | None = None, timeout: int = 30) -> dict | None:
    """curl fallback：subprocess 调 Gerrit REST API。

    与 HttpClient 的 auto_a_prefix 一致：直连 401 → 自动用 /a/ 前缀重试。
    HTTP Credentials 站点 /login/ 必 401（login 只认 LDAP 账号），故不走 login，
    直接 basic auth 打 REST，凭据经 --config - 从 stdin 传入（避免 argv 明文）。
    """
    cfg = load_gerrit_config()
    base_url = cfg.get("base_url", "")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    if not base_url or not user:
        return None
    cookie_file = Path(tempfile.gettempdir()) / "bugflow_gerrit_curl_cookies.txt"
    cred_cfg = f"-u {user}:{password}\n"
    from urllib.parse import urlencode
    qs = f"?{urlencode(params, doseq=True)}" if params else ""
    for cand in _curl_candidate_paths(path):
        text, code = _curl_request(
            f"{base_url.rstrip('/')}{cand}{qs}", cred_cfg, cookie_file, timeout)
        if code == 401:
            continue  # 直连 401 → 试 /a/ 前缀（HTTP Credentials 站点）
        if not text:
            continue
        if text.startswith(")]}'"):
            text = text[4:]
        try:
            return json.loads(text)
        except Exception:
            continue
    return None


def _curl_gerrit_patch(change_id: str, timeout: int = 45) -> str | None:
    """curl fallback：下载 Change patch（base64）。直连 401 → /a/ 重试。"""
    cfg = load_gerrit_config()
    base_url = cfg.get("base_url", "")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    if not base_url or not user:
        return None
    cookie_file = Path(tempfile.gettempdir()) / "bugflow_gerrit_curl_cookies.txt"
    cred_cfg = f"-u {user}:{password}\n"
    cid_enc = quote(str(change_id).strip(), safe="")
    patch_path = f"/changes/{cid_enc}/revisions/current/patch"
    for cand in _curl_candidate_paths(patch_path):
        text, code = _curl_request(
            f"{base_url.rstrip('/')}{cand}", cred_cfg, cookie_file, timeout)
        if code == 401:
            continue
        if not text:
            continue
        raw = _strip_xssi(text).strip()
        if raw.lstrip().startswith("<html"):
            continue  # HTML 错误页，非 patch
        try:
            decoded = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            continue  # 非 base64（错误页等），试下一个候选路径
        if decoded.lstrip().startswith("<html"):
            continue
        return decoded
    return None


# ═══════════════════════════════════════════════════════════════
# 核心操作函数
# ═══════════════════════════════════════════════════════════════

def gerrit_search(query: str, limit: int = 10) -> str:
    """搜索 Gerrit Changes。"""
    client = _require_client()
    limit = max(1, min(limit, 25))
    orig_query = (query or "").strip()
    query, rewrite_note = _normalize_gerrit_query(orig_query)
    wanted_branches, wanted_projects = _query_wanted_scopes(query)

    try:
        data = client.get_json("/changes/", params={
            "q": query, "n": str(limit),
            "o": ["CURRENT_REVISION", "DETAILED_LABELS"],
        })
    except Exception as e:
        msg = str(e)
        # 401：直连与 /a/ 均已自动重试且失败，curl 也必 401 → 直接给提示，省冗余请求
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        # 非 401：curl fallback 兜底（其内部也会按需 /a/ 重试）
        fallback = _curl_gerrit_api("/changes/", params={
            "q": query, "n": str(limit),
            "o": ["CURRENT_REVISION", "DETAILED_LABELS"],
        })
        if fallback and isinstance(fallback, list):
            data = fallback
        else:
            auth_err = _test_gerrit_auth(client)
            if auth_err:
                return auth_err
            raise GerritError(f"Gerrit 请求失败: {e}") from e

    if not data:
        msg = f"Gerrit 搜索 '{query}' 无结果。"
        if rewrite_note:
            msg = f"{rewrite_note}\n{msg}"
        return msg

    header = f"Gerrit 搜索 '{query}' ({len(data)} 条):\n"
    if rewrite_note:
        header = f"{rewrite_note}\n{header}"

    mismatch_n = sum(
        1 for c in data[:limit]
        if _change_scope_mismatch(c, wanted_branches, wanted_projects)
    )
    lines = [header]
    if (wanted_branches or wanted_projects) and mismatch_n:
        lines.append(
            f"⚠ 其中 {mismatch_n} 条 branch/project 与查询约束不符，已标注 ⚠分支不符。\n"
        )

    for change in data[:limit]:
        bad = _change_scope_mismatch(change, wanted_branches, wanted_projects)
        lines.append(_format_change(change, branch_mismatch=bad))
        lines.append("---")
    return "\n".join(lines)


def gerrit_get_change(change_id: str) -> str:
    """获取 Change 详情 + 变更文件列表。"""
    client = _require_client()
    cid = str(change_id).strip()

    try:
        data = client.get_json(f"/changes/{quote(cid, safe='')}/detail")
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        # 非 401：curl fallback 兜底（与 gerrit_search 一致）
        fallback = _curl_gerrit_api(f"/changes/{quote(cid, safe='')}/detail")
        if fallback and isinstance(fallback, dict):
            data = fallback
        else:
            auth_err = _test_gerrit_auth(client)
            if auth_err:
                return auth_err
            raise GerritError(f"Gerrit 请求失败: {e}") from e

    lines = [_format_change(data)]
    project = data.get("project", "")
    branch = data.get("branch", "")
    if project:
        lines.append(f"  提示: 可用 gerrit_search(action=cat|ls, repo={project}, branch={branch}) 读代码")

    reviewers_str = _format_reviewers(data.get("reviewers"))
    if reviewers_str:
        lines.append(f"  审查人: {reviewers_str}")

    msgs = _format_messages(data.get("messages"))
    if msgs:
        total = len(data.get("messages") or [])
        header = f"\n评论历史 ({total} 条，显示最近 {len(msgs)}):"
        lines.append(header)
        lines.extend(msgs)

    files = _fetch_change_files(client, cid)
    real_files = {k: v for k, v in files.items() if k != "/COMMIT_MSG"}
    if real_files:
        lines.append(f"\n变更文件 ({len(real_files)} 个):")
        for fname, finfo in list(real_files.items())[:40]:
            inserted = finfo.get("lines_inserted", 0) if isinstance(finfo, dict) else 0
            deleted = finfo.get("lines_deleted", 0) if isinstance(finfo, dict) else 0
            status = finfo.get("status", "M") if isinstance(finfo, dict) else "M"
            lines.append(f"  [{'📝' if inserted or deleted else '📄'}] [{status}] {fname} (+{inserted}/-{deleted})")
        if len(real_files) > 40:
            lines.append(f"  … 另有 {len(real_files) - 40} 个文件未列出")

    return "\n".join(lines)


def gerrit_my_changes(status: str = "open", limit: int = 10) -> str:
    """查询当前用户的提交。"""
    client = _require_client()
    user = client.user
    if not user:
        raise GerritError(
            "Gerrit 用户名未配置，无法查询'我的提交'。"
            "请调 config(action='save', config_type='gerrit', data={'user': '...', 'password': '...'})"
        )
    if status == "all":
        query = f"owner:{user}"
    else:
        query = f"owner:{user} status:{status}"
    return gerrit_search(query, limit)


def gerrit_list_projects(query: str = "", limit: int = 50) -> str:
    """搜索/列出 Gerrit 仓库名。"""
    client = _require_client()
    limit = max(1, min(int(limit or 50), 100))
    params: dict[str, str] = {"n": str(limit)}
    if query and query.strip():
        params["m"] = query.strip()
    try:
        data = client.get_json("/projects/", params=params)
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        auth_err = _test_gerrit_auth(client)
        if auth_err:
            return auth_err
        raise GerritError(f"列出仓库失败: {e}") from e
    if not isinstance(data, dict) or not data:
        return f"未找到匹配仓库（query={query!r}）。"
    names = sorted(data.keys())[:limit]
    lines = [f"Gerrit 仓库 ({len(names)} 个，query={query!r}):"]
    lines.extend(f"  📦 {n}" for n in names)
    return "\n".join(lines)


def gerrit_list_branches(repo: str = "", limit: int = 80) -> str:
    """列出某仓库的分支。"""
    client = _require_client()
    if not (repo or "").strip():
        return "❌ list_branches 需要 repo"
    limit = max(1, min(int(limit or 80), 200))
    try:
        data = client.get_json(f"/projects/{_quote_project(repo)}/branches/")
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        raise GerritError(f"列出分支失败: {e}") from e
    if not isinstance(data, list):
        return f"仓库 {repo} 分支列表异常。"
    branches = sorted({
        b.get("ref", "")[len("refs/heads/"):] if isinstance(b, dict) and b.get("ref", "").startswith("refs/heads/") else ""
        for b in data
    } - {""})
    shown = branches[:limit]
    lines = [f"仓库 {repo} 分支 ({len(branches)} 个，显示 {len(shown)}):"]
    lines.extend(f"  🌿 {b}" for b in shown)
    return "\n".join(lines)


def gerrit_ls(repo: str = "", branch: str = "", path: str = "") -> str:
    """Gitiles 列目录。"""
    client = _require_client()
    if not (repo or "").strip() or not (branch or "").strip():
        return "❌ ls 需要 repo 与 branch"
    ref = _normalize_ref(branch)
    api = _gitiles_api_path(repo, ref, path)
    try:
        text = client.get(api, params={"format": "JSON"}, timeout=45)
        data = json.loads(_strip_xssi(text))
        if not isinstance(data, dict):
            return f"❌ 无法解析目录: {repo} {branch} {path}"
        if "entries" not in data and "id" in data:
            return f"路径是文件而非目录: {path}\n请用 gerrit_search(action=cat, ...)"
        entries = sorted(
            data.get("entries") or [],
            key=lambda e: (e.get("type") != "tree", (e.get("name") or "").lower()),
        )
        display_path = path.strip("/") or "/"
        lines = [f"📂 {repo} @ {branch} : {display_path}", f"共 {len(entries)} 项:\n"]
        for e in entries[:_LS_MAX_ENTRIES]:
            etype = e.get("type", "")
            icon = "📁" if etype == "tree" else "📄"
            kind = "dir" if etype == "tree" else "file"
            lines.append(f"  {icon} [{kind}] {e.get('name', '?')}")
        if not entries:
            lines.append("  （空目录或路径不存在）")
        return "\n".join(lines)
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            raise GerritError(
                f"Gitiles 访问被拒 (401)：{repo} {branch} {path}\n"
                f"  Gitiles 需 nginx/LDAP 账号密码，Gerrit HTTP Credentials 无法访问。\n"
                f"  → 搜代码请用 search_code_tool（代码服务器本地 rg）\n"
                f"  → 读文件需在 gerrit.yaml 配置可过 nginx 的密码"
            ) from e
        raise GerritError(f"Gitiles ls 失败 ({repo} {branch} {path}): {e}") from e


def gerrit_cat(
    repo: str = "", branch: str = "", path: str = "",
    max_chars: int = _CAT_DEFAULT_MAX_CHARS, lines: str = "",
) -> str:
    """Gitiles 读文件内容。"""
    client = _require_client()
    if not (repo or "").strip() or not (branch or "").strip() or not (path or "").strip():
        return "❌ cat 需要 repo、branch、path"
    ref = _normalize_ref(branch)
    api = _gitiles_api_path(repo, ref, path)
    try:
        raw = client.get(api, params={"format": "TEXT"}, timeout=45)
        raw = _strip_xssi(raw).strip()
        if raw.lstrip().startswith("<html"):
            return f"❌ Gitiles 返回了 HTML 页面而非文件内容: {repo} {branch} {path}"
        try:
            content = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            # 非 base64 → 非 TEXT 格式响应（错误页/重定向），不当文件内容展示
            return f"❌ Gitiles 响应无法解析为文件内容: {repo} {branch} {path}"
        if content.lstrip().startswith("<html"):
            return f"❌ Gitiles 返回了 HTML 页面而非文件内容: {repo} {branch} {path}"

        all_lines = content.splitlines()
        total_lines = len(all_lines)
        start_line, end_line = _parse_line_range(lines, total_lines)
        if start_line is not None:
            sliced = all_lines[start_line - 1: end_line]
            body_text = "\n".join(sliced)
            line_base = start_line
            range_note = f"行 {start_line}-{min(end_line or total_lines, total_lines)} / 共 {total_lines} 行"
        else:
            body_text = content
            line_base = 1
            range_note = f"共 {total_lines} 行"

        cap = max(2000, min(int(max_chars or _CAT_DEFAULT_MAX_CHARS), _CAT_HARD_MAX_CHARS))
        truncated = len(body_text) > cap
        body = body_text[:cap]
        header = [
            f"📄 {repo} @ {branch}",
            f"路径: {path.strip('/')}",
            f"范围: {range_note}",
            f"大小: {len(body_text)} 字符" + (f"（已截断至 {cap}）" if truncated else ""),
            "",
        ]
        if truncated:
            header.append("… 截断提示: 请用 lines='起始,结束' 分段读取\n")
        numbered = []
        for i, line in enumerate(body.splitlines()):
            numbered.append(f"{line_base + i:5d}| {line}")
            if i + 1 >= 2000:
                numbered.append("     | … 行号显示上限 2000 行")
                break
        return "\n".join(header) + "\n".join(numbered)
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            raise GerritError(
                f"Gitiles 访问被拒 (401)：{repo} {branch} {path}\n"
                f"  Gitiles 需 nginx/LDAP 账号密码，Gerrit HTTP Credentials 无法访问。\n"
                f"  → 搜代码请用 search_code_tool（代码服务器本地 rg）\n"
                f"  → 读文件需在 gerrit.yaml 配置可过 nginx 的密码"
            ) from e
        raise GerritError(f"Gitiles cat 失败 ({repo} {branch} {path}): {e}") from e


def _collapse_ab_lines(ab: list, context: int) -> list[str]:
    ctx = max(0, int(context or 0))
    if ctx <= 0 or len(ab) <= ctx * 2:
        return [f" {ln}" for ln in ab]
    if len(ab) <= ctx * 2 + 4:
        return [f" {ln}" for ln in ab]
    head = [f" {ln}" for ln in ab[:ctx]]
    tail = [f" {ln}" for ln in ab[-ctx:]]
    skipped = len(ab) - ctx * 2
    return head + [f" @@ … {skipped} unchanged lines omitted @@"] + tail


def _format_file_diff_json(data: dict, path: str, max_chars: int, context: int = _DIFF_DEFAULT_CONTEXT) -> str:
    meta_a = data.get("meta_a") or {}
    meta_b = data.get("meta_b") or {}
    change_type = data.get("change_type") or ""
    lines_out = [
        f"📄 Diff: {path}" + (f" ({change_type})" if change_type else ""),
        f"  a: {meta_a.get('name', path)}",
        f"  b: {meta_b.get('name', path)}",
        "",
    ]
    content = data.get("content") or []
    added = deleted = 0
    for chunk in content:
        if not isinstance(chunk, dict):
            continue
        if "ab" in chunk:
            lines_out.extend(_collapse_ab_lines(list(chunk.get("ab") or []), context))
        if "a" in chunk:
            for ln in chunk.get("a") or []:
                lines_out.append(f"-{ln}")
                deleted += 1
        if "b" in chunk:
            for ln in chunk.get("b") or []:
                lines_out.append(f"+{ln}")
                added += 1
    lines_out.insert(3, f"  +/-: +{added} / -{deleted}  (context={context})")
    text = "\n".join(lines_out)
    cap = max(2000, min(int(max_chars or _CAT_DEFAULT_MAX_CHARS), _CAT_HARD_MAX_CHARS))
    if len(text) > cap:
        return text[:cap] + f"\n\n… 已截断至 {cap} 字符"
    return text


def gerrit_download_patch(change_id: str, save_to: str = "") -> str:
    """下载 Change 的完整 unified patch 到本地文件（不截断）。

    复用 /revisions/current/patch（base64）+ curl fallback，与 gerrit_diff 同源，
    但不做 max_chars 截断，直接落盘。save_to 省略时存临时目录。
    """
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ download_patch 需要 change_id"
    cid_enc = quote(cid, safe="")

    patch = ""
    try:
        raw = client.get(f"/changes/{cid_enc}/revisions/current/patch", timeout=60)
        raw = _strip_xssi(raw).strip()
        try:
            patch = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            patch = ""  # 非 base64（错误响应等），不走 raw → 走 curl fallback
        if not patch or patch.lstrip().startswith("<html"):
            patch = ""
    except Exception as e:
        # 401：直连与 /a/ 均已自动重试且失败，curl 也必 401 → 直接给提示，省冗余 curl
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        patch = ""

    # curl fallback
    if not patch:
        fb = _curl_gerrit_patch(cid, timeout=60)
        if fb:
            patch = fb
    if not patch:
        files = _fetch_change_files(client, cid)
        names = [k for k in (files or {}) if k != "/COMMIT_MSG"]
        hint = "、".join(names[:8]) if names else "(无文件列表)"
        return (
            f"❌ 无法获取 Change {cid} 的 patch。\n"
            f"变更文件: {hint}"
        )

    # 落盘
    if save_to and save_to.strip():
        out = Path(os.path.expanduser(save_to.strip()))
    else:
        out = Path(tempfile.gettempdir()) / f"gerrit_change_{cid}.patch"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(patch, encoding="utf-8")

    # 统计 patch 内容便于确认
    file_count = sum(1 for ln in patch.splitlines() if ln.startswith("diff --git "))
    return (
        f"✅ Change {cid} patch 已保存\n"
        f"  文件: {out}\n"
        f"  大小: {len(patch)} 字符 | 含 {file_count} 个文件改动\n"
        f"  应用: cd <repo> && git apply {out}  或  git am {out}"
    )


def gerrit_diff(
    change_id: str = "", path: str = "",
    max_chars: int = _CAT_DEFAULT_MAX_CHARS,
    context: int = _DIFF_DEFAULT_CONTEXT,
) -> str:
    """获取 Change 的 unified patch / 单文件 diff。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ diff 需要 change_id"
    cid_enc = quote(cid, safe="")
    cap = max(2000, min(int(max_chars or _CAT_DEFAULT_MAX_CHARS), _CAT_HARD_MAX_CHARS))
    file_path = (path or "").strip().lstrip("/")
    try:
        ctx = max(0, min(int(context) if context is not None else _DIFF_DEFAULT_CONTEXT, 200))
    except (TypeError, ValueError):
        ctx = _DIFF_DEFAULT_CONTEXT

    try:
        if file_path:
            f_enc = quote(file_path, safe="")
            data = client.get_json(
                f"/changes/{cid_enc}/revisions/current/files/{f_enc}/diff",
                params={"context": str(ctx)}, timeout=45,
            )
            if not isinstance(data, dict):
                return f"❌ 无法解析文件 diff: {file_path}"
            return f"Change {cid} 文件 diff\n" + _format_file_diff_json(data, file_path, cap, context=ctx)

        # Whole-change patch (base64)
        raw = client.get(f"/changes/{cid_enc}/revisions/current/patch", timeout=45)
        raw = _strip_xssi(raw).strip()
        try:
            patch = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            patch = ""  # 非 base64（错误响应等），不当 patch 展示 → 走下方错误提示
        if not patch or patch.lstrip().startswith("<html"):
            # curl fallback（与 download_patch 一致：主通道 200 但非 base64 也试 curl）
            fb = _curl_gerrit_patch(cid, timeout=45)
            if fb:
                header = [f"Change {cid} unified patch (curl fallback)",
                          f"大小: {len(fb)} 字符" + (f"（已截断至 {cap}）" if len(fb) > cap else ""), ""]
                body = fb if len(fb) <= cap else fb[:cap] + "\n\n… 已截断"
                return "\n".join(header) + body
            files = _fetch_change_files(client, cid)
            names = [k for k in (files or {}) if k != "/COMMIT_MSG"]
            hint = "、".join(names[:8]) if names else "(无文件列表)"
            return (
                f"❌ 无法获取 Change {cid} 的 patch。\n"
                f"变更文件: {hint}\n"
                f"请改用 gerrit_search(action='diff', change_id='{cid}', path='某文件路径')"
            )
        header = [
            f"Change {cid} unified patch",
            f"大小: {len(patch)} 字符" + (f"（已截断至 {cap}）" if len(patch) > cap else ""),
            "",
        ]
        body = patch if len(patch) <= cap else patch[:cap] + "\n\n… 已截断；请加 path= 只看单文件"
        return "\n".join(header) + body
    except Exception as e:
        # 401：直连与 /a/ 均已自动重试且失败，curl 也必 401 → 直接给提示，省冗余 curl
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        if not file_path:
            fb = _curl_gerrit_patch(cid, timeout=45)
            if fb:
                header = [f"Change {cid} unified patch (curl fallback)",
                          f"大小: {len(fb)} 字符" + (f"（已截断至 {cap}）" if len(fb) > cap else ""), ""]
                body = fb if len(fb) <= cap else fb[:cap] + "\n\n… 已截断"
                return "\n".join(header) + body
        raise GerritError(f"获取 Change {cid} diff 失败: {e}") from e


def _validate_score(score: int) -> tuple[str | None, int]:
    """校验 Code-Review 打分范围（Gerrit 只认 -2~+2）。

    返回 (错误提示 or None, 规范化后的整数 score)。
    非法输入时 score_norm=0，调用方应直接用返回的 err 终止。
    """
    try:
        s = int(score)
    except (TypeError, ValueError):
        return f"score 必须是整数，收到: {score}", 0
    if s < -2 or s > 2:
        return f"score 需在 -2~+2 之间（Gerrit Code-Review 限制），收到: {s}", 0
    return None, s


def gerrit_review(change_id: str, message: str = "", score: int = 0) -> str:
    """在 Change 上发表评论/打分。"""
    client = _require_client()
    cid = str(change_id).strip()

    body: dict[str, Any] = {}
    if message:
        body["message"] = message
    if score != 0:
        err, score = _validate_score(score)
        if err:
            return f"❌ {err}"
        body["labels"] = {"Code-Review": score}
    if not body:
        return "❌ 请至少提供 message 或 score"
    body["tag"] = "autogenerated:bugfix-flow"

    # post() 已带 auto_a_prefix：直连 401 → 自动 /a/ 重试，无需显式列 /a/ 路径
    try:
        client.post_json(f"/changes/{quote(cid, safe='')}/revisions/current/review", body, timeout=20)
        parts = []
        if message:
            parts.append("评论已发表")
        if score != 0:
            parts.append(f"打分: {score:+d}")
        return f"✅ Gerrit {cid}: {', '.join(parts)}"
    except Exception as e:
        err = str(e)
        hint = ""
        if _is_http_status(e, 401):
            hint = "\n提示: 写接口失败。可在 Gerrit Settings → HTTP Credentials 生成密码后写入 gerrit.yaml"
        return f"❌ 发表评论失败: {err}{hint}"


# ═══════════════════════════════════════════════════════════════
# 扩展操作：list_files / list_comments / commit_message
#           abandon / add_reviewer / review_line / set_topic
# ═══════════════════════════════════════════════════════════════


def gerrit_list_files(change_id: str) -> str:
    """列出 Change 的变更文件（状态 + 增删行）。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ list_files 需要 change_id"
    cid_enc = quote(cid, safe="")
    try:
        files = client.get_json(f"/changes/{cid_enc}/revisions/current/files")
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        fallback = _curl_gerrit_api(f"/changes/{cid_enc}/revisions/current/files")
        if fallback and isinstance(fallback, dict):
            files = fallback
        else:
            auth_err = _test_gerrit_auth(client)
            if auth_err:
                return auth_err
            raise GerritError(f"获取 Change {cid} 文件列表失败: {e}") from e

    if not isinstance(files, dict):
        return f"❌ 无法解析 Change {cid} 文件列表"
    real = {k: v for k, v in files.items() if k != "/COMMIT_MSG"}
    if not real:
        return f"Change {cid} 无变更文件。"
    lines = [f"Change {cid} 变更文件 ({len(real)} 个):"]
    for fpath, finfo in real.items():
        if not isinstance(finfo, dict):
            lines.append(f"  [?] {fpath}")
            continue
        status = finfo.get("status", "M")
        ins = finfo.get("lines_inserted", 0)
        dele = finfo.get("lines_deleted", 0)
        lines.append(f"  [{status}] {fpath} (+{ins}/-{dele})")
    return "\n".join(lines)


def gerrit_list_comments(change_id: str) -> str:
    """列出 Change 上的行级评论。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ list_comments 需要 change_id"
    cid_enc = quote(cid, safe="")
    try:
        comments_by_file = client.get_json(f"/changes/{cid_enc}/comments")
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        fallback = _curl_gerrit_api(f"/changes/{cid_enc}/comments")
        if fallback and isinstance(fallback, dict):
            comments_by_file = fallback
        else:
            auth_err = _test_gerrit_auth(client)
            if auth_err:
                return auth_err
            raise GerritError(f"获取 Change {cid} 评论失败: {e}") from e

    if not isinstance(comments_by_file, dict) or not comments_by_file:
        return f"Change {cid} 无行级评论。"

    total = sum(len(v) for v in comments_by_file.values() if isinstance(v, list))
    lines = [f"Change {cid} 评论 ({total} 条):"]
    for fpath, comments in comments_by_file.items():
        if not isinstance(comments, list) or not comments:
            continue
        lines.append(f"  📁 {fpath}")
        for c in comments:
            if not isinstance(c, dict):
                continue
            author = (c.get("author") or {}).get("name", "?")
            line = c.get("line", "?")
            updated = c.get("updated", "?")
            msg = (c.get("message") or "").strip().replace("\n", " ")
            if len(msg) > 120:
                msg = msg[:117] + "…"
            unresolved = c.get("unresolved", False)
            tag = "❓未解决" if unresolved else "✅已解决"
            lines.append(f"    L{line} [{author}] ({updated[:10]}) {tag}")
            lines.append(f"      {msg}")
    return "\n".join(lines)


def gerrit_commit_message(change_id: str) -> str:
    """获取 Change 的 commit message。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ commit_message 需要 change_id"
    cid_enc = quote(cid, safe="")
    try:
        data = client.get_json(f"/changes/{cid_enc}/revisions/current/commit")
    except Exception as e:
        msg = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        fallback = _curl_gerrit_api(f"/changes/{cid_enc}/revisions/current/commit")
        if fallback and isinstance(fallback, dict):
            data = fallback
        else:
            auth_err = _test_gerrit_auth(client)
            if auth_err:
                return auth_err
            raise GerritError(f"获取 Change {cid} commit message 失败: {e}") from e

    if not isinstance(data, dict):
        return f"❌ 无法解析 Change {cid} commit message"
    subject = data.get("subject", "无标题")
    full_msg = data.get("message", "(无 message)")
    lines = [
        f"Change {cid} Commit Message:",
        f"主题: {subject}",
        "",
        "完整 Message:",
        "─" * 40,
        full_msg,
        "─" * 40,
    ]
    return "\n".join(lines)


def gerrit_abandon(change_id: str, message: str = "") -> str:
    """Abandon 一个 Change（可带原因）。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ abandon 需要 change_id"
    body: dict[str, Any] = {"message": message}
    try:
        client.post_json(
            f"/changes/{quote(cid, safe='')}/abandon", body, timeout=20)
        return f"✅ Change {cid} 已 abandon" + (
            f"（{message}）" if message else "")
    except Exception as e:
        err = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        return f"❌ abandon 失败: {err}"


def gerrit_add_reviewer(change_id: str, reviewer: str, state: str = "REVIEWER") -> str:
    """给 Change 添加 reviewer 或 CC。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ add_reviewer 需要 change_id"
    reviewer = (reviewer or "").strip()
    if not reviewer:
        return "❌ add_reviewer 需要 reviewer"
    state = (state or "REVIEWER").strip().upper()
    if state not in ("REVIEWER", "CC"):
        return f"❌ state 只支持 REVIEWER 或 CC，收到: {state}"
    body: dict[str, Any] = {"reviewer": reviewer, "state": state}
    try:
        client.post_json(
            f"/changes/{quote(cid, safe='')}/reviewers", body, timeout=20)
        return f"✅ Change {cid}: 已添加 {reviewer} 为 {state}"
    except Exception as e:
        err = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        return f"❌ 添加 reviewer 失败: {err}"


def gerrit_review_line(change_id: str, path: str, line: int,
                       message: str, score: int = 0) -> str:
    """在 Change 的指定文件行上发表行级评论（可附带打分）。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ review_line 需要 change_id"
    file_path = (path or "").strip().lstrip("/")
    if not file_path:
        return "❌ review_line 需要 path"
    if not message:
        return "❌ review_line 需要 message"
    try:
        line_num = int(line)
    except (TypeError, ValueError):
        return f"❌ line 必须是整数，收到: {line}"
    if line_num < 1:
        return f"❌ line 必须 >= 1，收到: {line_num}"

    body: dict[str, Any] = {
        "comments": {file_path: [{"line": line_num, "message": message, "side": "REVISION"}]},
        "tag": "autogenerated:bugfix-flow",
    }
    if score != 0:
        err, score = _validate_score(score)
        if err:
            return f"❌ {err}"
        body["labels"] = {"Code-Review": score}
    try:
        client.post_json(
            f"/changes/{quote(cid, safe='')}/revisions/current/review",
            body, timeout=20)
        parts = [f"行级评论已发表 ({file_path}:L{line_num})"]
        if score != 0:
            parts.append(f"打分: {score:+d}")
        return f"✅ Change {cid}: {', '.join(parts)}"
    except Exception as e:
        err = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        return f"❌ 行级评论失败: {err}"


def gerrit_set_topic(change_id: str, topic: str) -> str:
    """设置（或清空）Change 的 topic。"""
    client = _require_client()
    cid = str(change_id or "").strip()
    if not cid:
        return "❌ set_topic 需要 change_id"
    topic = (topic or "").strip()
    body: dict[str, Any] = {"topic": topic}
    try:
        client.put_json(
            f"/changes/{quote(cid, safe='')}/topic", body, timeout=20)
        if topic:
            return f"✅ Change {cid}: topic 已设为 {topic}"
        return f"✅ Change {cid}: topic 已清除"
    except Exception as e:
        err = str(e)
        if _is_http_status(e, 401):
            return _GERRIT_401_HINT
        return f"❌ 设置 topic 失败: {err}"


# ═══════════════════════════════════════════════════════════════
# 统一入口 gerrit_search(action=...)
# ═══════════════════════════════════════════════════════════════

def gerrit(
    action: str = "",
    query: str = "",
    change_id: str = "",
    status: str = "open",
    limit: int = 10,
    repo: str = "",
    branch: str = "",
    path: str = "",
    max_chars: int = _CAT_DEFAULT_MAX_CHARS,
    lines: str = "",
    message: str = "",
    score: int = 0,
    save_to: str = "",
    topic: str = "",
    reviewer: str = "",
    state: str = "REVIEWER",
    line: int = 0,
    context: int = _DIFF_DEFAULT_CONTEXT,
) -> str:
    """统一 Gerrit 工具入口。

    action:
    - search: 搜 Change（需 query）
    - get_change: Change 详情（需 change_id）
    - list_files: Change 变更文件列表（需 change_id）
    - list_comments: Change 行级评论列表（需 change_id）
    - commit_message: Change 的 commit message（需 change_id）
    - diff: Change patch / 单文件 diff（需 change_id）
    - download_patch: 下载完整 patch 到本地文件（需 change_id, 可选 save_to）
    - my_changes: 当前用户提交
    - review: 发表 Change 级评论/打分（需 change_id）
    - review_line: 发表行级评论（需 change_id, path, line, message; 可选 score）
    - add_reviewer: 添加 reviewer/CC（需 change_id, reviewer; 可选 state）
    - abandon: Abandon Change（需 change_id; 可选 message）
    - set_topic: 设置/清除 topic（需 change_id; topic=空串清除）
    - list_projects / list_branches / ls / cat: Gitiles 远程读码
    """
    act = (action or "").strip().lower().replace("-", "_")
    lim = limit or 10

    if act == "search":
        if not query:
            return "❌ search 需要 query"
        return gerrit_search(query, limit=lim)
    if act in ("get_change", "get", "detail"):
        if not change_id:
            return "❌ get_change 需要 change_id"
        return gerrit_get_change(change_id)
    if act in ("list_files", "files"):
        if not change_id:
            return "❌ list_files 需要 change_id"
        return gerrit_list_files(change_id)
    if act in ("list_comments", "comments"):
        if not change_id:
            return "❌ list_comments 需要 change_id"
        return gerrit_list_comments(change_id)
    if act in ("commit_message", "commit"):
        if not change_id:
            return "❌ commit_message 需要 change_id"
        return gerrit_commit_message(change_id)
    if act in ("diff", "patch"):
        if not change_id:
            return "❌ diff 需要 change_id"
        return gerrit_diff(change_id=change_id, path=path, max_chars=max_chars, context=context)
    if act in ("download_patch", "download", "save_patch"):
        if not change_id:
            return "❌ download_patch 需要 change_id"
        return gerrit_download_patch(change_id=change_id, save_to=save_to)
    if act in ("my_changes", "mine", "my"):
        return gerrit_my_changes(status=status or "open", limit=lim)
    if act in ("review", "comment", "vote"):
        if not change_id:
            return "❌ review 需要 change_id"
        return gerrit_review(change_id, message=message, score=score)
    if act in ("review_line", "line_comment"):
        if not change_id:
            return "❌ review_line 需要 change_id"
        return gerrit_review_line(change_id, path=path, line=line, message=message, score=score)
    if act == "add_reviewer":
        if not change_id:
            return "❌ add_reviewer 需要 change_id"
        return gerrit_add_reviewer(change_id, reviewer=reviewer, state=state)
    if act == "abandon":
        if not change_id:
            return "❌ abandon 需要 change_id"
        return gerrit_abandon(change_id, message=message)
    if act in ("set_topic", "topic"):
        if not change_id:
            return "❌ set_topic 需要 change_id"
        return gerrit_set_topic(change_id, topic=topic)
    if act in ("list_projects", "projects", "repos"):
        return gerrit_list_projects(query=query, limit=limit or 50)
    if act in ("list_branches", "branches"):
        if not repo:
            return "❌ list_branches 需要 repo"
        return gerrit_list_branches(repo=repo, limit=limit or 80)
    if act in ("ls", "list", "listdir"):
        if not repo or not branch:
            return "❌ ls 需要 repo 与 branch"
        return gerrit_ls(repo=repo, branch=branch, path=path)
    if act in ("cat", "read", "file"):
        if not repo or not branch or not path:
            return "❌ cat 需要 repo、branch、path"
        return gerrit_cat(repo=repo, branch=branch, path=path, max_chars=max_chars, lines=lines)
    return (
        f"❌ 未知 action: {action}。"
        "支持: search, get_change, list_files, list_comments, commit_message, "
        "diff, download_patch, my_changes, review, review_line, add_reviewer, "
        "abandon, set_topic, list_projects, list_branches, ls, cat"
    )
