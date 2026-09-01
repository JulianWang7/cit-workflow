# -*- coding: utf-8 -*-
"""Wiki 实时抓取 — MediaWiki 登录 + 页面搜索 + 代码信息提取。

从 YWAgent tools/wiki.py 移植，去掉 YWAgent 框架依赖。
配置从 ~/.bugfix-flow/wiki.yaml 读取。

Wiki 版本: MediaWiki 1.15.x (旧版)
- 登录: POST Special:UserLogin (wpLoginToken)
- 页面: GET /index.php/{title}?action=raw
- 搜索: GET /index.php?title=Special:Search&search=...
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.error
import urllib.parse

from .config import load_wiki_config
from .http_client import HttpClient

# re-export for build_kb_wiki.py
__all__ = [
    "load_wiki_config", "wiki", "wiki_search", "wiki_fetch_product", "wiki_fetch_page",
    "_get_client", "_ensure_login", "_get_page_wikitext", "_search_pages",
    "_extract_code_info", "_candidate_titles", "WikiError",
]

logger = logging.getLogger("core.wiki")


# ── Singleton client + thread-safe login ──────────────────────

_wiki_client: HttpClient | None = None
_wiki_logged_in: bool = False
_wiki_lock = threading.Lock()


def _get_client() -> HttpClient:
    global _wiki_client
    cfg = load_wiki_config()
    if _wiki_client is not None and _wiki_client.base_url == cfg.get("base_url", "").rstrip("/"):
        return _wiki_client
    _wiki_client = HttpClient(
        base_url=cfg.get("base_url", "http://192.168.0.138/wiki").rstrip("/"),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        auth_mode="form",
        verify_ssl=False,
        cookie_file="wiki",
    )
    return _wiki_client


class WikiError(Exception):
    pass


def _ensure_login() -> bool:
    """登录 MediaWiki 1.15（wpLoginToken flow，线程安全）。"""
    global _wiki_logged_in
    if _wiki_logged_in:
        return True

    with _wiki_lock:
        if _wiki_logged_in:
            return True
        client = _get_client()
        try:
            # Step 1: GET login page, extract wpLoginToken
            page = client.get("/index.php?title=Special:UserLogin", timeout=15)
            if "pt-logout" in page or 'id="pt-userpage"' in page:
                _wiki_logged_in = True
                return True

            m = re.search(r'name="wpLoginToken"\s+value="([^"]+)"', page)
            if not m:
                m = re.search(r'name="wpLoginToken"[^>]*value="([^"]+)"', page)
            if not m:
                raise WikiError("Wiki 登录失败: 无法提取 wpLoginToken")
            token = m.group(1)

            # Step 2: POST login
            resp = client.post_form(
                "/index.php?title=Special:UserLogin&action=submitlogin&type=login",
                {
                    "wpName": client.user,
                    "wpPassword": client.password,
                    "wpLoginToken": token,
                    "wpRemember": "1",
                    "wpLoginattempt": "Log in",
                },
                timeout=15,
            )

            # Step 3: 验证
            check = resp or ""
            if "pt-logout" in check or 'id="pt-userpage"' in check:
                _wiki_logged_in = True
                return True

            home = client.get("/index.php/Main_Page", timeout=10)
            if "pt-logout" in home or 'id="pt-userpage"' in home:
                _wiki_logged_in = True
                return True

            # Last resort: raw page fetch
            test = client.get("/index.php/SRM965", params={"action": "raw"}, timeout=10)
            if len(test) > 100 and not test.lstrip().lower().startswith("<!doctype"):
                _wiki_logged_in = True
                return True

            raise WikiError("Wiki 登录失败: 验证未通过，请检查用户名密码")
        except WikiError:
            raise
        except Exception as e:
            raise WikiError(f"Wiki 登录失败: {e}") from e


# ── 页面抓取 ──────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _get_page_wikitext(title: str) -> str:
    """获取页面原始 wikitext。"""
    client = _get_client()
    path_title = title.replace(" ", "_")
    try:
        result = client.get(
            f"/index.php/{urllib.parse.quote(path_title, safe='()')}",
            params={"action": "raw"}, timeout=15,
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise WikiError(f"Wiki 页面不存在: {title}") from e
        raise WikiError(f"Wiki 页面获取失败 ({title}): HTTP {e.code}") from e
    if not result or len(result) < 10 or "does not exist" in result.lower():
        raise WikiError(f"Wiki 页面不存在: {title}")
    if result.lstrip().lower().startswith("<!doctype") or "<html" in result[:200].lower():
        raise WikiError(f"Wiki 页面不可读（可能未登录或无权限）: {title}")
    return result


def _is_noise_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if t.startswith(("特殊:", "Special:", "帮助:", "Help:", "模板:", "Template:")):
        return True
    if t.startswith("您的") or t.startswith("访问"):
        return True
    if "尚未撰写" in t or t.endswith("（尚未撰写）"):
        return True
    return False


def _search_pages(keyword: str, limit: int = 10) -> list[str]:
    """搜索 Wiki，返回页面标题列表。"""
    client = _get_client()
    try:
        html = client.get(
            f"/index.php?title=Special:Search&search={urllib.parse.quote(keyword)}&fulltext=1",
            timeout=15,
        )
        titles: list[str] = []
        for block in re.finditer(
            r"<ul[^>]*class=['\"]mw-search-results['\"][^>]*>(.*?)</ul>",
            html, re.I | re.S,
        ):
            for m in re.finditer(
                r'<a[^>]+href="/wiki/index\.php/[^"]+"[^>]*\stitle="([^"]+)"',
                block.group(1),
            ):
                title = _strip_html(m.group(1))
                if _is_noise_title(title):
                    continue
                if title not in titles:
                    titles.append(title)
            if len(titles) >= limit:
                break
        return titles[:limit]
    except Exception as e:
        logger.warning("Wiki search '%s' failed: %s", keyword, e)
        return []


def _candidate_titles(product: str) -> list[str]:
    """构建产品代号的可能页面标题。"""
    p = (product or "").strip()
    if not p:
        return []
    out = [p]
    for suffix in (
        "_Android16(德赛)", " Android16(德赛)", "_Android16", " Android16",
        "_Android15", " Android15", "_Android14", " Android14",
    ):
        cand = f"{p}{suffix}" if not p.endswith(suffix.strip()) else p
        if cand not in out:
            out.append(cand)
    return out


# ── 代码信息提取 ──────────────────────────────────────────────

def _extract_code_info(wikitext: str) -> dict:
    """从 wikitext 提取仓库 URL、分支、manifest、编译命令。"""
    plain = _strip_html(wikitext)
    lines = plain.split("\n")

    repo_url_re = re.compile(
        r"(?:https?://192\.168\.0\.(?:240|248)/(?:admin/)?(?:repos|projects)/[A-Za-z0-9_.\-/]+|"
        r"ssh://[^\s<>\"']+@?192\.168\.0\.240:\d+/[A-Za-z0-9_.\-/]+)",
        re.IGNORECASE,
    )
    branch_label_re = re.compile(
        r"(?:提交分支|平台提交分支|平台代码分支|平台自动化代码分支|美格代码分支|"
        r"德赛客户自动化代码分支|代码分支|分支|Branch|基线分支)"
        r"(?:\([^)]*\))?\s*[:：]\s*([A-Za-z0-9_][A-Za-z0-9_.\-/]*)"
    )
    manifest_branch_re = re.compile(r"repo\s+init\b[^\n]*?-b\s+(\S+)")
    manifest_xml_re = re.compile(r"repo\s+init\b[^\n]*?-m\s+(\S+\.xml)")
    download_cmd_re = re.compile(
        r"(repo\s+init\b[^\n]*?(?:-u\s+\S+|ssh://\S+)[^\n]*)", re.IGNORECASE,
    )
    repo_sync_re = re.compile(r"(repo\s+sync\b[^\n]*)", re.IGNORECASE)
    xml_name_re = re.compile(
        r"([A-Za-z][\w.-]*?(?:Vehicle|Desay|qssi|vendor|lxc)[\w.-]*\.xml)", re.IGNORECASE,
    )
    git_clone_re = re.compile(r"(git\s+clone\s+[^\n]+)", re.IGNORECASE)
    mkdir_re = re.compile(r"(mkdir\s+[^\n]+)", re.IGNORECASE)

    info = {
        "branches": [], "manifest_branch": "", "manifest_xml": "",
        "download_commands": [], "build_commands": [], "repos": [],
    }

    def _add_cmd(cmd: str):
        cmd = cmd.strip().rstrip("\\").strip()
        if cmd and cmd not in info["download_commands"]:
            info["download_commands"].append(cmd)

    for line in lines:
        stripped = re.sub(r"^:{1,}", "", line).strip()
        stripped = re.sub(r"'''?", "", stripped)
        if not stripped or stripped.startswith("=") or stripped.startswith("{{"):
            continue

        for m in repo_url_re.finditer(stripped):
            url = m.group(0).rstrip(".,;:)")
            if url not in info["repos"]:
                info["repos"].append(url)

        m = branch_label_re.search(stripped)
        if m:
            branch = m.group(1).strip(".,;: []")
            branch = re.split(r"--+", branch, maxsplit=1)[0].strip("-")
            if branch not in info["branches"] and len(branch) > 3:
                info["branches"].append(branch)

        m = manifest_branch_re.search(stripped)
        if m:
            info["manifest_branch"] = m.group(1)
        m = manifest_xml_re.search(stripped)
        if m:
            info["manifest_xml"] = m.group(1)
        if not info["manifest_xml"]:
            m = xml_name_re.search(stripped)
            if m:
                info["manifest_xml"] = m.group(1)

        m = download_cmd_re.search(stripped)
        if m:
            _add_cmd(m.group(1))
        m = repo_sync_re.search(stripped)
        if m:
            _add_cmd(m.group(1))
        m = mkdir_re.search(stripped)
        if m and "LA." in m.group(1):
            _add_cmd(m.group(1))
        for m in git_clone_re.finditer(stripped):
            _add_cmd(m.group(1))

    # Full-text scan
    for m in download_cmd_re.finditer(plain):
        _add_cmd(m.group(1))
    for m in repo_sync_re.finditer(plain):
        _add_cmd(m.group(1))

    return info


def _format_code_info(info: dict, product: str, page_titles: list[str]) -> str:
    lines = [f"## {product} — Wiki 代码信息"]
    if page_titles:
        lines.append(f"来源页面: {', '.join(page_titles)}\n")
    if info["manifest_branch"]:
        lines.append(f"**Manifest 分支**: `{info['manifest_branch']}`")
    if info["manifest_xml"]:
        lines.append(f"**Manifest XML**: `{info['manifest_xml']}`")
    if info["branches"]:
        lines.append("\n**代码分支**:")
        for b in info["branches"]:
            lines.append(f"  - `{b}`")
    if info["download_commands"]:
        lines.append("\n**拉取命令**:")
        lines.append("```bash")
        for cmd in info["download_commands"]:
            lines.append(cmd)
        lines.append("```")
    if info["repos"]:
        lines.append(f"\n**相关仓库** ({len(info['repos'])} 个):")
        for r in info["repos"][:15]:
            lines.append(f"  - {r}")
    if not any([info["branches"], info["manifest_branch"], info["repos"], info["download_commands"]]):
        lines.append("\n⚠️ 未能从 Wiki 页面自动提取代码信息。")
    return "\n".join(lines)


# ── 公开函数 ──────────────────────────────────────────────────

def wiki_search(keyword: str, limit: int = 10) -> str:
    """在 Wiki 中搜索关键词。"""
    _ensure_login()
    try:
        titles = _search_pages(keyword, limit)
        if not titles:
            return f"Wiki 中未找到关于 '{keyword}' 的页面。"
        lines = [f"Wiki 搜索 '{keyword}' ({len(titles)} 条):\n"]
        for title in titles[:limit]:
            lines.append(f"  📄 {title}")
        return "\n".join(lines)
    except WikiError as e:
        return f"❌ {e}"


def wiki_fetch_product(product: str) -> str:
    """从 Wiki 获取指定产品的代码信息。"""
    _ensure_login()
    try:
        titles: list[str] = []
        fetched: dict[str, str] = {}

        for cand in _candidate_titles(product):
            try:
                fetched[cand] = _get_page_wikitext(cand)
                if cand not in titles:
                    titles.append(cand)
                break
            except WikiError:
                continue

        def _norm(t: str) -> str:
            return t.replace(" ", "_")

        seen_norm = {_norm(t) for t in titles}
        for t in _search_pages(product, limit=8):
            if _norm(t) not in seen_norm:
                titles.append(t)
                seen_norm.add(_norm(t))

        if not titles:
            return f"❌ Wiki 中未找到关于 '{product}' 的页面。"

        all_info = {
            "branches": [], "manifest_branch": "", "manifest_xml": "",
            "download_commands": [], "build_commands": [], "repos": [],
        }
        used_titles: list[str] = []

        for title in titles[:5]:
            wikitext = fetched.get(title)
            if wikitext is None:
                try:
                    wikitext = _get_page_wikitext(title)
                except WikiError:
                    continue
            if wikitext and len(wikitext) > 50:
                used_titles.append(title)
                info = _extract_code_info(wikitext)
                for key in ("branches", "download_commands", "build_commands", "repos"):
                    for item in info.get(key, []):
                        if item not in all_info[key]:
                            all_info[key].append(item)
                for key in ("manifest_branch", "manifest_xml"):
                    if info.get(key) and not all_info[key]:
                        all_info[key] = info[key]

        if not used_titles:
            return f"❌ Wiki 中找到候选页面但均无法读取: {', '.join(titles[:5])}"

        # 自动缓存到本地知识库
        try:
            from .kb_wiki import upsert_wiki_page
            for title in used_titles:
                wikitext = fetched.get(title) or ""
                upsert_wiki_page({
                    "product": product,
                    "title": title,
                    "wikitext": wikitext,
                    "branches": "\n".join(all_info["branches"]),
                    "manifest_branch": all_info["manifest_branch"],
                    "manifest_xml": all_info["manifest_xml"],
                    "download_commands": "\n".join(all_info["download_commands"]),
                    "build_commands": "\n".join(all_info.get("build_commands", [])),
                    "repos": "\n".join(all_info["repos"]),
                    "url": "",
                })
            logger.info("Cached %d wiki page(s) for %s to local DB", len(used_titles), product)
        except Exception as e:
            logger.warning("Failed to cache wiki page to DB: %s", e)

        return _format_code_info(all_info, product, used_titles)
    except WikiError as e:
        return f"❌ {e}"


def wiki_fetch_page(title: str) -> str:
    """获取指定 Wiki 页面的原始内容。"""
    _ensure_login()
    try:
        wikitext = _get_page_wikitext(title)
        plain = _strip_html(wikitext)
        # 截断超长内容
        if len(plain) > 10000:
            plain = plain[:10000] + "\n\n… 已截断至 10000 字符"
        return f"📄 Wiki 页面: {title}\n\n{plain}"
    except WikiError as e:
        return f"❌ {e}"


def wiki(action: str, keyword: str = "", product: str = "", limit: int = 10) -> str:
    """统一 Wiki 入口（供 tools/wiki_tools.py 调用）。

    Args:
        action: search / fetch_product / search_db / stats
        keyword: 搜索关键词（search / search_db）
        product: 产品代号（fetch_product / search_db）
        limit: 返回条数
    """
    if action == "search":
        if not keyword:
            return "❌ search 需要 keyword 参数。"
        return wiki_search(keyword, limit)

    elif action == "fetch_product":
        if not product:
            return "❌ fetch_product 需要 product 参数。"
        return wiki_fetch_product(product)

    elif action == "search_db":
        from .kb_wiki import search_kb_wiki_formatted
        kw = keyword or product
        if not kw:
            return "❌ search_db 需要 keyword 或 product 参数。"
        return search_kb_wiki_formatted(keyword=kw, limit=limit)

    elif action == "stats":
        from .kb_wiki import kb_wiki_stats
        return kb_wiki_stats()

    else:
        return f"❌ 未知 action: {action}。支持: search/fetch_product/search_db/stats"
