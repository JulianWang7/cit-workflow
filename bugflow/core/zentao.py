"""禅道客户端 — MCP 读路径 + Web 写路径 + 附件下载。

从 YWAgent tools/zentao.py 移植，去掉 YWAgent 依赖（ToolRegistry/agent.config/ssh_tools），
改为自-contained 模块，配置读 ~/.bugfix-flow/zentao.yaml。

两条路径：
  - MCP 读路径: POST {base_url}/mcp.php + X-MCP-Token/X-MCP-Secret，JSON-RPC 2.0
    远程工具: zentao_bug_detail / zentao_bugs_search / zentao_product_detail
  - Web 写路径: GET login → extract verifyRand → md5(md5(pw)+vrand) → POST login
    写操作: update_bug (官方 MCP 无写工具)

BugItem 映射:
  bug_id = str(id)
  title = title
  description = html_to_text(steps)  ← 禅道 steps 字段就是 bug 正文
  reproduce_steps = ""  ← 让 AI 从标题+描述推导
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

from bugflow.core.config import load_zentao_config

logger = logging.getLogger("bugflow.core.zentao")


# ── 异常 ──────────────────────────────────────────────────────
class ZentaoError(Exception):
    """禅道操作失败（瞬态，可重试）。"""


class ZentaoAuthError(ZentaoError):
    """鉴权失败（不可重试 — token/账密错误）。"""


# ── 全局状态（Web 登录态）──────────────────────────────────────
_zt_jar: CookieJar | None = None
_zt_base: str = ""
_zt_user: str = ""
_zt_password: str = ""
_zt_logged_in: bool = False
_zt_lock = threading.Lock()

# Bug 缓存: {bug_id: (result_str, cached_at_epoch)}
# 成功 600s，错误 60s（超时瞬态，过一会儿可重试）
_bug_cache: dict[int, tuple[str, float]] = {}
_BUG_CACHE_TTL_OK = 600.0
_BUG_CACHE_TTL_ERR = 60.0

# 构建名称缓存: {build_id_str: (build_name, cached_at_epoch)}
_build_name_cache: dict[str, tuple[str, float]] = {}
_BUILD_CACHE_TTL = 3600.0  # 1h

# MCP 消息 ID
_mcp_msg_id = 0
_mcp_call_lock = threading.Lock()

# SSL context（内网系统跳过验证）
_ssl_no_verify: ssl.SSLContext | None = None

_VERIFY_RAND_PATTERNS = (
    r"verifyRand'[^>]*value='([a-z0-9]+)'",
    r'verifyRand"[^>]*value="([a-z0-9]+)"',
    r'name=["\']verifyRand["\'][^>]*value=["\']([a-z0-9]+)["\']',
    r'value=["\']([a-z0-9]+)["\'][^>]*name=["\']verifyRand["\']',
    r"verifyRand\s*[:=]\s*['\"]([a-z0-9]+)['\"]",
)


# ── 配置 ──────────────────────────────────────────────────────
def _load_cfg() -> dict:
    """读 zentao.yaml 配置（每次调用读，缓存由 config.lru_cache 管）。"""
    cfg = load_zentao_config()
    global _zt_base, _zt_user, _zt_password
    _zt_base = cfg.get("base_url", "").rstrip("/")
    _zt_user = cfg.get("user", "")
    _zt_password = cfg.get("password", "")
    return cfg


def _mcp_credentials() -> tuple[str, str]:
    """读 MCP Token/Secret，缺失报 ZentaoAuthError。"""
    cfg = _load_cfg()
    token = cfg.get("mcp_token", "").strip()
    secret = cfg.get("mcp_secret", "").strip()
    if not token or not secret:
        raise ZentaoAuthError(
            "未配置禅道 MCP Token/Secret，请在 ~/.bugfix-flow/zentao.yaml 设置 "
            "mcp_token 和 mcp_secret（从禅道 → MCP 页面生成）"
        )
    return token, secret


# ═══════════════════════════════════════════════════════════════
# MCP 读路径 (JSON-RPC 2.0 over HTTP)
# ═══════════════════════════════════════════════════════════════

def _mcp_url() -> str:
    cfg = _load_cfg()
    if not _zt_base:
        raise ZentaoAuthError(
            "未配置禅道 base_url，请在 ~/.bugfix-flow/zentao.yaml 设置 zentao.base_url"
        )
    return f"{_zt_base}/mcp.php"


def _mcp_call(method: str, params: dict | None = None, timeout: float = 30) -> dict:
    """发送 JSON-RPC 2.0 请求，返回 result dict。

    HTTP 401/403 → ZentaoAuthError（不可重试）
    网络/超时/5xx → ZentaoError（transient）
    """
    global _mcp_msg_id
    with _mcp_call_lock:
        _mcp_msg_id += 1
        msg_id = _mcp_msg_id
    msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        msg["params"] = params

    url = _mcp_url()
    token, secret = _mcp_credentials()
    body = json.dumps(msg).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("X-MCP-Token", token)
    req.add_header("X-MCP-Secret", secret)

    logger.debug("ZenTao MCP call: %s (id=%d)", method, msg_id)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ZentaoAuthError(
                f"禅道 MCP 鉴权失败 (HTTP {e.code})，请检查 zentao.yaml 中的 "
                "mcp_token/mcp_secret"
            )
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise ZentaoError(f"禅道 MCP HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ZentaoError(f"禅道 MCP 连接失败: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ZentaoError(f"禅道 MCP 响应解析失败: {e}; body={raw[:200]}") from e

    if "error" in data:
        err = data["error"]
        code = err.get("code", "?")
        message = err.get("message", str(err))
        raise ZentaoError(f"禅道 MCP 错误 [{code}]: {message}")

    return data.get("result", {})


def _mcp_tool_call(tool_name: str, arguments: dict | None = None, timeout: float = 30) -> Any:
    """调用 MCP 工具，返回 content[0].text 解析后的 dict/list。

    MCP 工具返回格式: {"content": [{"type":"text","text":"<json-string>"}]}
    """
    result = _mcp_call("tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout=timeout)
    content = result.get("content", [])
    if result.get("isError"):
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        raise ZentaoError(f"禅道 MCP 工具 '{tool_name}' 返回错误: {' '.join(texts)}")
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    raw_text = "\n".join(texts) if texts else ""
    if not raw_text:
        raise ZentaoError(f"禅道 MCP 工具 '{tool_name}' 返回空响应")
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ZentaoError(
            f"禅道 MCP 工具 '{tool_name}' 返回非 JSON: {e}; text={raw_text[:200]}"
        ) from e


# ═══════════════════════════════════════════════════════════════
# Web 写路径 (urllib + CookieJar + md5 login)
# ═══════════════════════════════════════════════════════════════

def _ssl_context() -> ssl.SSLContext:
    global _ssl_no_verify
    if _ssl_no_verify is None:
        _ssl_no_verify = ssl.create_default_context()
        _ssl_no_verify.check_hostname = False
        _ssl_no_verify.verify_mode = ssl.CERT_NONE
    return _ssl_no_verify


def _zt_get(path: str, timeout: int = 15) -> str:
    _load_cfg()
    global _zt_jar
    if _zt_jar is None:
        _zt_jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_zt_jar),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    url = f"{_zt_base}/{path.lstrip('/')}" if path else _zt_base
    req = urllib.request.Request(url)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _zt_post_form(path: str, fields: dict, timeout: int = 10) -> str:
    _load_cfg()
    global _zt_jar
    if _zt_jar is None:
        _zt_jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_zt_jar),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    url = f"{_zt_base}/{path.lstrip('/')}" if path else _zt_base
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _zt_download(path: str, timeout: int = 300) -> bytes:
    """下载文件（返回 bytes），复用 _ensure_login 的 CookieJar。"""
    _load_cfg()
    _ensure_login()
    global _zt_jar
    if _zt_jar is None:
        _zt_jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_zt_jar),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    url = f"{_zt_base}/{path.lstrip('/')}" if not path.startswith("http") else path
    req = urllib.request.Request(url)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _reset_session() -> None:
    global _zt_jar, _zt_logged_in
    with _zt_lock:
        _zt_jar = CookieJar()
        _zt_logged_in = False


def resolve_build_name(build_id: str) -> str:
    """将 openedBuild 数字 ID 解析为构建名称（如 11185 → SNM928K_251213_100_V01_T04）。

    通过禅道 Web 接口 m=build&f=view&buildID=<id>&t=json 获取，解析 JSON title 字段。
    带 1h 缓存。失败返回空字符串（不抛异常，graceful degradation）。
    """
    bid = str(build_id).strip()
    if not bid or bid == "0":
        return ""

    # 缓存检查
    now = time.time()
    cached = _build_name_cache.get(bid)
    if cached:
        name, cached_at = cached
        if now - cached_at < _BUILD_CACHE_TTL:
            return name

    def _do_fetch() -> str:
        _ensure_login()
        resp = _zt_get(f"/index.php?m=build&f=view&buildID={bid}&t=json")
        if not resp or "verifyRand" in resp or "m=user&f=login" in resp:
            # 会话过期，重试一次
            _reset_session()
            _ensure_login()
            resp = _zt_get(f"/index.php?m=build&f=view&buildID={bid}&t=json")
        try:
            data = json.loads(resp)
            title = data.get("title", "")
            if title:
                _build_name_cache[bid] = (title, time.time())
                return title
        except (json.JSONDecodeError, TypeError):
            pass
        return ""

    try:
        result = _do_fetch()
        if result:
            _build_name_cache[bid] = (result, time.time())
        return result
    except Exception:
        logger.debug("resolve_build_name(%s) failed", bid, exc_info=True)
        return ""


def _version_prefix(build_name: str) -> str:
    """从构建名称提取版本前缀（第一个下划线前的部分）。

    SNM928K_251213_100_V01_T04 → SNM928K
    """
    if not build_name:
        return ""
    return build_name.split("_")[0]


def _extract_verify_rand(page: str) -> str | None:
    for pat in _VERIFY_RAND_PATTERNS:
        m = re.search(pat, page, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _ensure_login(max_attempts: int = 3) -> bool:
    """确保已登录（线程安全）。失败抛 ZentaoAuthError。"""
    global _zt_logged_in
    _load_cfg()

    if _zt_logged_in:
        return True

    with _zt_lock:
        if _zt_logged_in:
            return True

        if not _zt_user or not _zt_password:
            raise ZentaoAuthError(
                "未配置禅道账号密码，请在 ~/.bugfix-flow/zentao.yaml 中设置 user 和 password"
            )

        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                global _zt_jar
                _zt_jar = CookieJar()

                page = _zt_get("/index.php?m=user&f=login", timeout=30)
                if not page or len(page) < 100:
                    raise ZentaoAuthError(
                        f"禅道登录页异常（{len(page) if page else 0} 字节），请检查网络/VPN"
                    )
                vrand = _extract_verify_rand(page)
                if not vrand:
                    snippet = re.sub(r"\s+", " ", page)[:180]
                    raise ZentaoAuthError(
                        f"无法从登录页提取 verifyRand（页面长度: {len(page)}；摘要: {snippet}）"
                    )

                inner = hashlib.md5(_zt_password.encode()).hexdigest()
                pw_hash = hashlib.md5((inner + vrand).encode()).hexdigest()

                _zt_post_form(
                    "/index.php?m=user&f=login",
                    {
                        "account": _zt_user,
                        "password": pw_hash,
                        "passwordStrength": "1",
                        "referer": "",
                        "verifyRand": vrand,
                        "keepLogin[]": "on",
                    },
                    timeout=30,
                )

                resp = _zt_get("/index.php?m=my&f=index&t=json", timeout=30)
                if "Dashboard" in resp or '"title"' in resp:
                    _zt_logged_in = True
                    return True
                raise ZentaoAuthError(f"登录验证失败（用户名或密码错误）；响应: {resp[:200]}")
            except (ConnectionError, TimeoutError, OSError) as e:
                last_err = e
                logger.warning("ZenTao login network error attempt %d/%d: %s", attempt, max_attempts, e)
            except ZentaoAuthError as e:
                last_err = e
                if "用户名或密码错误" in str(e):
                    raise
                logger.warning("ZenTao login auth error attempt %d/%d: %s", attempt, max_attempts, e)

            _zt_logged_in = False
            if attempt < max_attempts:
                time.sleep(min(30, 3 * attempt))

        if isinstance(last_err, ZentaoAuthError):
            raise last_err
        raise ZentaoAuthError(f"无法连接禅道服务器 ({_zt_base}): {last_err}") from last_err


# ═══════════════════════════════════════════════════════════════
# 格式化辅助
# ═══════════════════════════════════════════════════════════════

def _html_to_text(html: str) -> str:
    """简单 HTML → 纯文本。"""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.strip()


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 0:
        return "?"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _format_bug(bug: dict, actions: dict | list | None = None) -> str:
    lines = [
        f"Bug #{bug.get('id')}: {bug.get('title', '无标题')}",
        f"  状态: {bug.get('status', '?')} | 严重程度: {bug.get('severity', '?')}",
        f"  优先级: {bug.get('pri', '?')} | 指派给: {bug.get('assignedTo', '?')}",
    ]
    module = bug.get("module", "")
    bug_type = bug.get("type", "")
    if module or bug_type:
        extras = []
        if module:
            extras.append(f"模块: {module}")
        if bug_type:
            extras.append(f"类型: {bug_type}")
        lines.append(f"  {' | '.join(extras)}")
    product = bug.get("product_name", bug.get("product", ""))
    build = bug.get("openedBuild", "")
    if product:
        line = f"  产品: {product}"
        if build:
            line += f" | 发现版本: {build}"
        lines.append(line)
    os_info = bug.get("os", "")
    if os_info:
        lines.append(f"  平台: {os_info}")
    lines.append(f"  创建人: {bug.get('openedBy', '?')} | 创建时间: {bug.get('openedDate', '?')}")
    resolution = bug.get("resolution", "")
    if resolution:
        lines.append(f"  解决: {resolution}")
    steps = bug.get("steps", "")
    if steps:
        lines.append(f"\n  描述:\n{_html_to_text(steps)[:800]}")

    if actions:
        action_list = list(actions.values()) if isinstance(actions, dict) else actions
        action_list.sort(key=lambda a: a.get("date", ""), reverse=True)
        comment_actions = [a for a in action_list if a.get("comment", "").strip()]
        if comment_actions:
            lines.append(f"\n  评论 ({len(comment_actions)} 条):")
            for act in comment_actions[:20]:
                comment = _html_to_text(act.get("comment", "")).strip()
                if comment:
                    actor = act.get("actor", "?")
                    date = act.get("date", "")[:16]
                    lines.append(f"    [{date}] {actor}: {comment[:500]}")

    atts = bug.get("attachments", [])
    if atts:
        lines.append(f"\n  附件 ({len(atts)}):")
        for a in atts:
            if not isinstance(a, dict):
                continue
            size = _fmt_size(int(a.get("size", 0) or 0))
            ext = a.get("extension", "")
            title = a.get("title", "?")
            name = f"{title}.{ext}" if ext and not title.endswith(f".{ext}") else title
            lines.append(f"    [{a.get('id')}] {name} ({size})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 结构化访问（给 Python 调用方，返回 dict）
# ═══════════════════════════════════════════════════════════════

def _mcp_bug_to_flat(bug_detail: dict) -> dict:
    """将 MCP zentao_bug_detail 的 {basic, actions, attachments} 扁平化。"""
    basic = bug_detail.get("basic", bug_detail)
    flat = dict(basic)
    if "actions" in bug_detail:
        flat["actions"] = bug_detail["actions"]
    if "attachments" in bug_detail:
        flat["attachments"] = bug_detail["attachments"]
    return flat


def _ensure_product_name(bug: dict) -> None:
    """若 bug 缺 product_name，调 MCP zentao_product_detail 补全。"""
    product_id = str(bug.get("product", ""))
    if product_id and not bug.get("product_name"):
        try:
            prod_data = _mcp_tool_call("zentao_product_detail", {"productID": int(product_id)})
            prod_basic = prod_data.get("basic", prod_data) if isinstance(prod_data, dict) else {}
            if isinstance(prod_basic, dict):
                bug["product_name"] = prod_basic.get("name", product_id)
        except (ZentaoError, ZentaoAuthError):
            raise
        except Exception:
            bug["product_name"] = product_id


def get_bug(bug_id: int) -> dict:
    """获取 Bug 详情，返回扁平化 dict（含完整未截断 steps + actions）。

    Raises ZentaoError on failure.
    """
    data = _mcp_tool_call("zentao_bug_detail", {"bugID": int(bug_id)})
    if not data or not data.get("basic", {}).get("id"):
        raise ZentaoError(f"Bug #{bug_id} 不存在")
    bug = _mcp_bug_to_flat(data)
    _ensure_product_name(bug)
    return bug


def get_bug_formatted(bug_id: int) -> str:
    """获取 Bug 详情，返回格式化文本（给 MCP 工具用，带缓存）。"""
    now = time.time()
    with _zt_lock:
        cached = _bug_cache.get(bug_id)
        if cached:
            result_str, cached_at = cached
            is_error = result_str.startswith("❌")
            ttl = _BUG_CACHE_TTL_ERR if is_error else _BUG_CACHE_TTL_OK
            if now - cached_at < ttl:
                return result_str

    try:
        bug = get_bug(bug_id)
        actions = bug.get("actions")
        result = _format_bug(bug, actions)
        with _zt_lock:
            _bug_cache[bug_id] = (result, time.time())
        return result
    except (ZentaoError, ZentaoAuthError):
        raise
    except Exception as e:
        logger.exception("get_bug_formatted #%s unexpected error", bug_id)
        result = f"❌ 查询 Bug #{bug_id} 失败: {e}"
        with _zt_lock:
            _bug_cache[bug_id] = (result, time.time())
        return result


def search_bugs(
    keyword: str = "",
    limit: int = 20,
    product_id: int = 0,
    status: str = "",
    page: int = 1,
    person: str = "",
) -> tuple[list[dict], int]:
    """服务端搜索 Bug，返回 (bug dict 列表, 匹配总数)。

    至少提供 keyword / product_id / status / person 之一作为过滤条件。
    服务端每页最多返回 100 条，超出需用 page 翻页（total 为全量匹配数）。
    """
    args: dict = {"limit": limit, "page": max(1, int(page))}
    if keyword:
        args["title"] = keyword
    if product_id:
        args["product"] = product_id
    if status:
        args["status"] = status
    if person:
        args["person"] = person
    if not any(k in args for k in ("title", "product", "status", "person")):
        return [], 0
    data = _mcp_tool_call("zentao_bugs_search", args)
    if not isinstance(data, dict):
        return [], 0
    return data.get("bugs", []) or [], int(data.get("total", 0) or 0)


def configured_user() -> str:
    """当前 zentao.yaml 中的登录账号（assignedTo 比对用）。"""
    _load_cfg()
    return str(_zt_user or "").strip()


def my_bugs(limit: int = 20) -> list[dict]:
    """查询指派给我的未解决 Bug。"""
    _load_cfg()
    data = _mcp_tool_call(
        "zentao_bugs_search",
        {"person": _zt_user, "status": "active", "limit": limit, "page": 1},
    )
    return data.get("bugs", []) if isinstance(data, dict) else []


def my_bugs_all(page_size: int = 100, max_pages: int = 20) -> list[dict]:
    """分页拉取指派给当前用户的全部 active Bug。"""
    _load_cfg()
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        bugs, total = search_bugs(
            "",
            limit=page_size,
            status="active",
            page=page,
            person=_zt_user,
        )
        if not bugs:
            break
        out.extend(bugs)
        if len(out) >= total or len(bugs) < page_size:
            break
    return out


def list_products(limit: int = 100) -> list[dict]:
    """获取禅道产品列表（结构化）。"""
    data = _mcp_tool_call("zentao_products_list", {"limit": limit, "page": 1})
    products = data.get("products", []) if isinstance(data, dict) else []
    return list(products) if isinstance(products, list) else []


def search_bugs_formatted(
    keyword: str = "",
    limit: int = 20,
    product_id: int = 0,
    status: str = "",
    page: int = 1,
    person: str = "",
) -> str:
    """搜索 Bug，返回格式化文本。

    keyword 为空时按 product_id / status / person 浏览 Bug 列表。
    服务端每页最多 100 条，匹配总数超过本页时给出翻页提示。
    """
    kw = keyword.strip()
    if not kw and not product_id and not status and not person:
        return "❌ 请提供搜索关键词或产品 ID / 状态 / 指派人过滤条件。"
    bugs, total = search_bugs(
        kw, limit=limit, product_id=product_id, status=status, page=page, person=person
    )
    if not bugs:
        desc_parts: list[str] = []
        if kw:
            desc_parts.append(f"关键词 '{kw}'")
        if product_id:
            desc_parts.append(f"产品 {product_id}")
        if status:
            desc_parts.append(f"状态 {status}")
        if person:
            desc_parts.append(f"指派人 {person}")
        return f"未找到匹配的 Bug（{' + '.join(desc_parts)}）。"
    pg = max(1, int(page))
    lines = [f"=== Bug (共 {total} 个匹配，本页显示 {len(bugs)} 条，第 {pg} 页) ==="]
    for bug in bugs[:limit]:
        lines.append(_format_bug(bug))
        lines.append("---")
    fetched = (pg - 1) * 100 + len(bugs)
    if total > fetched:
        lines.append(
            f"⚠️ 还有 {total - fetched} 条未显示（服务端每页最多 100 条），"
            f"用 page={pg + 1} 继续翻页。"
        )
    return "\n".join(lines)


def my_bugs_formatted(limit: int = 20) -> str:
    """我的未解决 Bug，返回格式化文本。"""
    bugs = my_bugs(limit=limit)
    if not bugs:
        return "✅ 没有指派给你的未解决 Bug。"
    lines = [f"共 {len(bugs)} 个未解决 Bug:\n"]
    for bug in bugs[:limit]:
        lines.append(_format_bug(bug))
        lines.append("---")
    return "\n".join(lines)


def list_products_formatted(limit: int = 50) -> str:
    """获取禅道产品列表，返回格式化文本（id + 名称）。"""
    data = _mcp_tool_call("zentao_products_list", {"limit": limit, "page": 1})
    products = data.get("products", []) if isinstance(data, dict) else []
    if not products:
        return "未找到任何产品。"
    lines = [f"=== 产品列表 ({len(products)} 个) ==="]
    for p in products[:limit]:
        pid = p.get("id", "?")
        name = p.get("name", "?")
        code = p.get("code", "")
        lines.append(f"  #{pid}  {name}" + (f"  ({code})" if code else ""))
    return "\n".join(lines)


def _validate_zt_response(resp: str, action_desc: str) -> None:
    """校验禅道 Web 写操作的响应，失败抛 ZentaoError/ZentaoAuthError。

    禅道成功响应通常为 JSON {"result":"success"} 或包含 "成功" 的 HTML。
    登录页重定向（session 过期）→ ZentaoAuthError 并重置登录态。
    """
    if not resp or len(resp) < 10:
        raise ZentaoError(f"禅道{action_desc}返回空响应")

    # 检测登录页重定向（session 过期）
    if "verifyRand" in resp or "m=user&f=login" in resp:
        _reset_session()
        raise ZentaoAuthError(f"禅道{action_desc}失败：会话已过期（返回登录页），请重试")

    # 尝试 JSON 解析
    try:
        data = json.loads(resp)
        result = data.get("result", "")
        if result == "success":
            return
        message = data.get("message", str(data))
        raise ZentaoError(f"禅道{action_desc}失败: {message}")
    except json.JSONDecodeError:
        pass  # 非 JSON，继续检查 HTML

    # HTML 响应：检查是否包含成功/失败标志
    if "成功" in resp or "success" in resp.lower():
        return
    # 某些操作成功后返回空 body 或重定向 HTML（302 已被 opener 跟踪）
    if "location" in resp.lower() or len(resp) < 200:
        return

    snippet = re.sub(r"\s+", " ", resp)[:300]
    raise ZentaoError(f"禅道{action_desc}可能失败（未检测到成功标志）: {snippet}")


def update_bug(bug_id: int, status: str = "", comment: str = "") -> str:
    """更新禅道 Bug 状态/备注（走 Web 写路径）。

    - 状态: POST m=bug&f=edit&bugID=<id>（仅传 status 字段）— 核心操作，优先执行
    - 评论: POST m=action&f=comment&objectType=bug&objectID=<id> — 辅助操作，失败降级为 warning
    - 两者同时: 先状态再评论，status 成功即核心完成，comment 失败不阻塞

    Raises ZentaoError/ZentaoAuthError on status failure (not comment failure).
    """
    _ensure_login()
    if not status and not comment:
        return "❌ 请指定要更新的字段 (status 或 comment)"

    parts = []
    warnings = []

    # 1. 状态变更（核心操作，优先执行，失败抛异常）
    if status:
        resp = _zt_post_form(
            f"/index.php?m=bug&f=edit&bugID={bug_id}",
            {"status": status},
        )
        _validate_zt_response(resp, f"Bug #{bug_id} 状态变更")
        parts.append(f"状态 → {status}")

    # 2. 评论（辅助操作，失败降级为 warning，不阻塞核心流程）
    if comment:
        try:
            resp = _zt_post_form(
                f"/index.php?m=action&f=comment&objectType=bug&objectID={bug_id}",
                {"comment": comment},
            )
            _validate_zt_response(resp, f"Bug #{bug_id} 添加备注")
            parts.append("备注已添加")
        except (ZentaoError, ZentaoAuthError) as e:
            warnings.append(f"备注添加失败（不阻塞状态更新）: {e}")

    msg = f"✅ Bug #{bug_id} 已更新: {', '.join(parts)}"
    if warnings:
        msg += f"  ⚠️ {'; '.join(warnings)}"
    return msg


def download_attachment(bug_id: int, file_id: int, save_dir: str = ".") -> str:
    """下载禅道 Bug 附件到 save_dir 目录。

    返回保存路径与大小。失败抛 ZentaoError。
    """
    bug_id = int(bug_id)
    file_id = int(file_id)

    # 1. 取附件元信息
    filename = f"file_{file_id}"
    try:
        bug = get_bug(bug_id)
        atts = bug.get("attachments", []) or []
        match = next(
            (a for a in atts if isinstance(a, dict) and int(a.get("id", 0) or 0) == file_id),
            None,
        )
        if match:
            title = str(match.get("title", f"file_{file_id}"))
            ext = str(match.get("extension", ""))
            filename = f"{title}.{ext}" if ext and not title.endswith(f".{ext}") else title
    except Exception:
        pass  # best-effort，用 fallback 文件名

    # 2. Web HTTP 下载
    data = _zt_download(f"/index.php?m=file&f=download&fileID={file_id}")

    # 3. 保存
    save_path = os.path.join(save_dir, filename)
    os.makedirs(save_dir, exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(data)
    return f"已下载: {save_path} ({_fmt_size(len(data))})"


# ═══════════════════════════════════════════════════════════════
# BugItem 映射（给 CLI 用）
# ═══════════════════════════════════════════════════════════════

def fetch_bug_as_bugitem(bug_id: int):
    """从禅道拉取 bug 详情，返回 BugItem（供 orchestrator 用）。

    Raises ZentaoError on failure.
    """
    # 延迟 import 避免循环
    from bugflow.cli.main import BugItem

    bug = get_bug(bug_id)
    steps = _html_to_text(bug.get("steps", ""))
    return BugItem(
        bug_id=str(bug.get("id", bug_id)),
        title=bug.get("title", f"Bug #{bug_id}"),
        description=steps,
        reproduce_steps="",  # 让 AI 从标题+描述推导
    )


# ── 提交记录查询 ──────────────────────────────────────────────

# commit hash: 必须有 "commit"/"提交" 前缀 + 8-40 位十六进制
# （裸 hex 串如 f000000/25008604 是寄存器地址，非 commit hash）
_COMMIT_RE = re.compile(r"(?:commit|提交)\s+([0-9a-f]{8,40})\b")
# Gerrit Change-Id: I + 40 位十六进制
_CHANGEID_RE = re.compile(r"(I[0-9a-f]{40})")
# Gerrit URL — 捕获 change number，匹配多种格式:
#   /c/project/+/123        /#/c/project/+/123        /+/123
_GERRIT_URL_RE = re.compile(
    r"https?://[^\s]+/(?:#/c/[^/\s]+/\+/|c/[^/\s]+/\+/|\+/)(\d+)", re.IGNORECASE
)
# 禅道里常见的 "Bug #xxx" 引用
_BUG_REF_RE = re.compile(r"[Bb]ug\s*#?\s*(\d+)")


def query_bug_commits(bug_id: int) -> str:
    """查 Bug 关联的代码提交记录。

    解析 Bug 的 actions/comments 中的 commit hash、Gerrit Change-Id、
    Gerrit URL，同时搜索相似已解决 Bug 的解决方案中可能包含的提交信息。

    Returns:
        格式化文本，包含找到的提交记录和相似 Bug 的修复参考。
    """
    lines: list[str] = [f"=== Bug #{bug_id} 提交记录查询 ===\n"]

    # 1. 取当前 Bug 详情（含 actions/comments）
    try:
        bug = get_bug(bug_id)
    except (ZentaoError, ZentaoAuthError):
        raise
    except Exception as e:
        return f"❌ 查询 Bug #{bug_id} 失败: {e}"

    found_commits: list[str] = []
    found_changeids: list[str] = []
    found_gerrit_change_nums: list[str] = []  # Gerrit change number（可直接传给 gerrit_search）

    # 2. 解析 resolution 字段
    resolution = bug.get("resolution", "")
    if resolution:
        lines.append(f"解决方案: {resolution}")
        for m in _COMMIT_RE.findall(resolution):
            if m not in found_commits:
                found_commits.append(m)
        for m in _CHANGEID_RE.findall(resolution):
            if m not in found_changeids:
                found_changeids.append(m)
        for m in _GERRIT_URL_RE.findall(resolution):
            if m not in found_gerrit_change_nums:
                found_gerrit_change_nums.append(m)

    # 3. 解析 actions/comments
    actions = bug.get("actions")
    if actions:
        action_list = list(actions.values()) if isinstance(actions, dict) else actions
        action_list.sort(key=lambda a: a.get("date", ""), reverse=True)

        for act in action_list:
            comment = _html_to_text(act.get("comment", "")).strip()
            if not comment:
                continue
            actor = act.get("actor", "?")
            date = act.get("date", "")[:16]
            action_type = act.get("action", "")

            # 提取 commit 引用
            for m in _COMMIT_RE.findall(comment):
                if m not in found_commits:
                    found_commits.append(m)
            for m in _CHANGEID_RE.findall(comment):
                if m not in found_changeids:
                    found_changeids.append(m)
            for m in _GERRIT_URL_RE.findall(comment):
                if m not in found_gerrit_change_nums:
                    found_gerrit_change_nums.append(m)

            # 如果 comment 包含提交相关信息，记录
            if any(kw in comment.lower() for kw in ("commit", "提交", "change-id", "gerrit", "merge", "合入")):
                lines.append(f"  [{date}] {actor} ({action_type}): {comment[:300]}")

    # 4. 输出找到的提交引用
    if found_commits:
        lines.append(f"\n关联 Commit ({len(found_commits)}):")
        for c in found_commits:
            lines.append(f"  commit {c}")
    if found_changeids:
        lines.append(f"\n关联 Change-Id ({len(found_changeids)}):")
        for c in found_changeids:
            lines.append(f"  {c}")
    if found_gerrit_change_nums:
        lines.append(f"\nGerrit Change Number ({len(found_gerrit_change_nums)}) — 可直接传给 gerrit_search(change_id=...):")
        for n in found_gerrit_change_nums:
            lines.append(f"  #{n}")

    # 5. 版本同源 Bug 提交搜索（优先按产品+版本前缀，降级按标题关键词）
    title = bug.get("title", "")
    product_id = bug.get("product", "")
    opened_build = bug.get("openedBuild", "")
    similar_results: list[str] = []

    # 5a. 解析当前 Bug 的版本前缀
    version_prefix = ""
    if opened_build:
        build_name = resolve_build_name(str(opened_build))
        if build_name:
            version_prefix = _version_prefix(build_name)
            lines.append(f"\n版本同源搜索: 当前版本={build_name}, 前缀={version_prefix}")

    # 5b. 按产品搜索已解决 Bug（比标题关键词更精准，避免跨产品误匹配）
    sibling_candidates: list[dict] = []
    if product_id:
        try:
            prod_siblings, _ = search_bugs(
                product_id=int(product_id), status="resolved", limit=50
            )
            # search_bugs product 过滤已知不可靠，二次确认同产品
            sibling_candidates = [
                s for s in prod_siblings
                if str(s.get("product", "")) == str(product_id)
            ]
        except Exception:
            pass

    # 5c. 降级：同产品无结果 → 标题关键词搜索
    if not sibling_candidates and title:
        try:
            kw_siblings, _ = search_bugs(title[:30], limit=10)
            sibling_candidates = kw_siblings
        except Exception:
            pass

    # 5d. 遍历候选，提取提交记录
    for sb in sibling_candidates:
        sb_id = sb.get("id")
        if not sb_id or int(sb_id) == bug_id:
            continue
        sb_status = sb.get("status", "")
        if sb_status not in ("resolved", "closed"):
            continue
        sb_resolution = sb.get("resolution", "")
        sb_title = sb.get("title", "")
        try:
            sb_detail = get_bug(int(sb_id))

            # 版本前缀过滤：能解析则过滤，不能解析则放行
            if version_prefix:
                sb_build_name = resolve_build_name(
                    str(sb_detail.get("openedBuild", ""))
                )
                if sb_build_name and not sb_build_name.startswith(version_prefix):
                    continue  # 版本不匹配，跳过

            sb_actions = sb_detail.get("actions")
            sb_commits: list[str] = []
            sb_changeids: list[str] = []
            sb_change_nums: list[str] = []
            # scan_sources 同时包含搜索结果的 resolution 和详情的 resolution（后者更完整）
            scan_sources = [sb_resolution, sb_detail.get("resolution", "")]
            if sb_actions:
                sb_act_list = list(sb_actions.values()) if isinstance(sb_actions, dict) else sb_actions
                for sa in sb_act_list:
                    sa_comment = _html_to_text(sa.get("comment", "")).strip()
                    scan_sources.append(sa_comment)
            for text in scan_sources:
                for m in _COMMIT_RE.findall(text):
                    if m not in sb_commits:
                        sb_commits.append(m)
                for m in _CHANGEID_RE.findall(text):
                    if m not in sb_changeids:
                        sb_changeids.append(m)
                for m in _GERRIT_URL_RE.findall(text):
                    if m not in sb_change_nums:
                        sb_change_nums.append(m)
            has_refs = sb_commits or sb_changeids or sb_change_nums or sb_resolution
            if has_refs:
                ref_parts = []
                if sb_change_nums:
                    ref_parts.append(f"Change #{', #'.join(sb_change_nums[:3])}")
                if sb_changeids:
                    ref_parts.append(f"Change-Id {', '.join(sb_changeids[:2])}")
                if sb_commits:
                    ref_parts.append(f"commit {', '.join(sb_commits[:3])}")
                ref_str = " | ".join(ref_parts) if ref_parts else "无明确 commit"
                similar_results.append(
                    f"  Bug #{sb_id}: {sb_title}\n"
                    f"    状态: {sb_status} | 解决: {sb_resolution}\n"
                    f"    提交: {ref_str}"
                )
        except Exception:
            pass

    if similar_results:
        lines.append(f"\n版本同源 Bug 的提交参考 ({len(similar_results)}):")
        lines.extend(similar_results)

    if not found_commits and not found_changeids and not found_gerrit_change_nums and not similar_results:
        lines.append("\n未找到关联的提交记录。可能该 Bug 尚未修复，或提交记录未记录在禅道中。")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Task（任务）— MCP 读路径，与 Bug 平行
# ═══════════════════════════════════════════════════════════════

# Task 缓存: {task_id: (result_str, cached_at_epoch)}
_task_cache: dict[int, tuple[str, float]] = {}


def _format_task(task: dict, actions: dict | list | None = None) -> str:
    """格式化 task dict 为可读文本。"""
    lines = [
        f"Task #{task.get('id')}: {task.get('name', '无标题')}",
        f"  状态: {task.get('status', '?')} | 优先级: {task.get('pri', '?')}",
        f"  指派给: {task.get('assignedTo', '?')} | 类型: {task.get('type', '?')}",
    ]
    project = task.get("project", "")
    module = task.get("module", "")
    if project or module:
        extras = []
        if project:
            extras.append(f"项目: {project}")
        if module:
            extras.append(f"模块: {module}")
        lines.append(f"  {' | '.join(extras)}")
    est = task.get("est", "")
    consumed = task.get("consumed", "")
    left = task.get("left", "")
    if est or consumed or left:
        lines.append(f"  工时: 预计 {est}h | 已耗 {consumed}h | 剩余 {left}h")
    deadline = task.get("deadline", "")
    if deadline and deadline != "0000-00-00":
        lines.append(f"  截止: {deadline}")
    lines.append(f"  创建人: {task.get('openedBy', '?')} | 创建时间: {task.get('openedDate', '?')}")
    desc = task.get("desc", "")
    if desc:
        lines.append(f"\n  描述:\n{_html_to_text(desc)[:800]}")

    if actions:
        action_list = list(actions.values()) if isinstance(actions, dict) else actions
        action_list.sort(key=lambda a: a.get("date", ""), reverse=True)
        comment_actions = [a for a in action_list if a.get("comment", "").strip()]
        if comment_actions:
            lines.append(f"\n  评论 ({len(comment_actions)} 条):")
            for act in comment_actions[:20]:
                comment = _html_to_text(act.get("comment", "")).strip()
                if comment:
                    actor = act.get("actor", "?")
                    date = act.get("date", "")[:16]
                    lines.append(f"    [{date}] {actor}: {comment[:500]}")

    return "\n".join(lines)


def get_task(task_id: int) -> dict:
    """获取 Task 详情，返回扁平化 dict。"""
    data = _mcp_tool_call("zentao_task_detail", {"taskID": int(task_id)})
    if not data or not data.get("basic", {}).get("id"):
        raise ZentaoError(f"Task #{task_id} 不存在")
    flat = dict(data.get("basic", data))
    if "actions" in data:
        flat["actions"] = data["actions"]
    if "attachments" in data:
        flat["attachments"] = data["attachments"]
    return flat


def get_task_formatted(task_id: int) -> str:
    """获取 Task 详情，返回格式化文本（带缓存）。"""
    now = time.time()
    with _zt_lock:
        cached = _task_cache.get(task_id)
        if cached:
            result_str, cached_at = cached
            is_error = result_str.startswith("❌")
            ttl = _BUG_CACHE_TTL_ERR if is_error else _BUG_CACHE_TTL_OK
            if now - cached_at < ttl:
                return result_str

    try:
        task = get_task(task_id)
        actions = task.get("actions")
        result = _format_task(task, actions)
        with _zt_lock:
            _task_cache[task_id] = (result, time.time())
        return result
    except (ZentaoError, ZentaoAuthError):
        raise
    except Exception as e:
        logger.exception("get_task_formatted #%s unexpected error", task_id)
        result = f"❌ 查询 Task #{task_id} 失败: {e}"
        with _zt_lock:
            _task_cache[task_id] = (result, time.time())
        return result


def search_tasks(
    keyword: str = "",
    limit: int = 20,
    person: str = "",
    status: str = "",
    page: int = 1,
) -> list[dict]:
    """服务端搜索 Task，返回 task dict 列表。

    至少提供 keyword / person / status 之一作为过滤条件。
    """
    args: dict = {"limit": limit, "page": max(1, int(page))}
    if keyword:
        args["title"] = keyword
    if person:
        args["person"] = person
    if status:
        args["status"] = status
    if not any(k in args for k in ("title", "person", "status")):
        return []
    data = _mcp_tool_call("zentao_tasks_search", args)
    return data.get("tasks", []) if isinstance(data, dict) else []


def my_tasks(limit: int = 20) -> list[dict]:
    """查询指派给我的未完成 Task。

    远程 zentao_tasks_search 的 status 参数不支持逗号分隔多值（传
    "wait,doing" 会返回 0 条），因此对每个未完成状态分别查询再合并去重。
    """
    _load_cfg()
    if not _zt_user:
        raise ZentaoAuthError(
            "未配置禅道账号，无法查询'我的 Task'。请在 ~/.bugfix-flow/zentao.yaml 中设置 user"
        )
    #禅道 task 未完成状态：wait（未开始）/ doing（进行中）/ pause（暂停）
    seen_ids: set[str] = set()
    tasks: list[dict] = []
    for st in ("wait", "doing", "pause"):
        data = _mcp_tool_call(
            "zentao_tasks_search",
            {"person": _zt_user, "status": st, "limit": limit, "page": 1},
        )
        batch = data.get("tasks", []) if isinstance(data, dict) else []
        for t in batch:
            tid = t.get("id")
            if tid and tid not in seen_ids:
                seen_ids.add(tid)
                tasks.append(t)
        if len(tasks) >= limit:
            break
    return tasks[:limit]


def search_tasks_formatted(
    keyword: str = "",
    limit: int = 20,
    person: str = "",
    status: str = "",
    page: int = 1,
) -> str:
    """搜索 Task，返回格式化文本。

    keyword 为空时按 person / status 浏览 Task 列表。
    """
    kw = keyword.strip()
    if not kw and not person and not status:
        return "❌ 请提供搜索关键词或指派人 / 状态过滤条件。"
    tasks = search_tasks(kw, limit=limit, person=person, status=status, page=page)
    if not tasks:
        desc_parts: list[str] = []
        if kw:
            desc_parts.append(f"关键词 '{kw}'")
        if person:
            desc_parts.append(f"指派人 {person}")
        if status:
            desc_parts.append(f"状态 {status}")
        return f"未找到匹配的 Task（{' + '.join(desc_parts)}）。"
    lines = [f"=== Task ({len(tasks)} 个匹配) ==="]
    for task in tasks[:limit]:
        lines.append(_format_task(task))
        lines.append("---")
    return "\n".join(lines)


def my_tasks_formatted(limit: int = 20) -> str:
    """我的未完成 Task，返回格式化文本。"""
    tasks = my_tasks(limit=limit)
    if not tasks:
        return "✅ 没有指派给你的未完成 Task。"
    lines = [f"共 {len(tasks)} 个未完成 Task:\n"]
    for task in tasks[:limit]:
        lines.append(_format_task(task))
        lines.append("---")
    return "\n".join(lines)


def query_task_commits(task_id: int) -> str:
    """查 Task（需求/开发任务）关联的代码提交记录。

    解析 Task 的 actions/comments 中的 commit hash、Gerrit Change-Id、
    Gerrit URL，同时搜索相似已完结 Task 的提交参考。

    Returns:
        格式化文本，包含找到的提交记录和相似 Task 的实现参考。
    """
    lines: list[str] = [f"=== Task #{task_id} 提交记录查询 ===\n"]

    # 1. 取当前 Task 详情（含 actions/comments）
    try:
        task = get_task(task_id)
    except (ZentaoError, ZentaoAuthError):
        raise
    except Exception as e:
        return f"❌ 查询 Task #{task_id} 失败: {e}"

    found_commits: list[str] = []
    found_changeids: list[str] = []
    found_gerrit_urls: list[str] = []
    found_bug_refs: list[str] = []

    # 2. 解析 actions/comments（Task 无 resolution 字段，直接走评论解析）
    actions = task.get("actions")
    if actions:
        action_list = list(actions.values()) if isinstance(actions, dict) else actions
        action_list.sort(key=lambda a: a.get("date", ""), reverse=True)

        for act in action_list:
            comment = _html_to_text(act.get("comment", "")).strip()
            if not comment:
                continue
            actor = act.get("actor", "?")
            date = act.get("date", "")[:16]
            action_type = act.get("action", "")

            # 提取 commit 引用
            for m in _COMMIT_RE.findall(comment):
                if m not in found_commits:
                    found_commits.append(m)
            for m in _CHANGEID_RE.findall(comment):
                if m not in found_changeids:
                    found_changeids.append(m)
            for m in _GERRIT_URL_RE.findall(comment):
                if m not in found_gerrit_urls:
                    found_gerrit_urls.append(m)
            # 提取 "Bug #xxx" 关联引用
            for m in _BUG_REF_RE.findall(comment):
                if m not in found_bug_refs:
                    found_bug_refs.append(m)

            # 如果 comment 包含提交/实现相关信息，记录
            if any(kw in comment.lower() for kw in ("commit", "提交", "change-id", "gerrit", "merge", "合入", "实现", "方案")):
                lines.append(f"  [{date}] {actor} ({action_type}): {comment[:300]}")

    # 3. 输出找到的提交引用
    if found_commits:
        lines.append(f"\n关联 Commit ({len(found_commits)}):")
        for c in found_commits:
            lines.append(f"  commit {c}")
    if found_changeids:
        lines.append(f"\n关联 Change-Id ({len(found_changeids)}):")
        for c in found_changeids:
            lines.append(f"  {c}")
    if found_gerrit_urls:
        lines.append(f"\nGerrit 链接 ({len(found_gerrit_urls)}):")
        for u in found_gerrit_urls:
            lines.append(f"  {u}")
    if found_bug_refs:
        lines.append(f"\n关联 Bug ({len(found_bug_refs)}):")
        for b in found_bug_refs:
            lines.append(f"  Bug #{b}")

    # 4. 搜索相似已完结 Task，查它们的提交参考
    title = task.get("name", "")
    similar_results: list[str] = []
    if title:
        keywords = title[:30]  # 用标题前 30 字符搜索
        try:
            similar_tasks = search_tasks(keywords, limit=10)
            for st in similar_tasks:
                st_id = st.get("id")
                if not st_id or int(st_id) == task_id:
                    continue
                st_status = st.get("status", "")
                if st_status not in ("done", "closed"):
                    continue
                st_title = st.get("name", "")
                # 尝试取相似 Task 的 actions 看有没有 commit
                try:
                    st_detail = get_task(int(st_id))
                    st_actions = st_detail.get("actions")
                    st_commits: list[str] = []
                    if st_actions:
                        st_act_list = list(st_actions.values()) if isinstance(st_actions, dict) else st_actions
                        for sa in st_act_list:
                            sa_comment = _html_to_text(sa.get("comment", "")).strip()
                            for m in _COMMIT_RE.findall(sa_comment):
                                if m not in st_commits:
                                    st_commits.append(m)
                    if st_commits:
                        commit_str = ", ".join(st_commits[:3])
                        similar_results.append(
                            f"  Task #{st_id}: {st_title}\n"
                            f"    状态: {st_status}\n"
                            f"    提交: {commit_str}"
                        )
                except Exception:
                    pass
        except Exception:
            pass

    if similar_results:
        lines.append(f"\n相似已完结 Task 的提交参考 ({len(similar_results)}):")
        lines.extend(similar_results)

    if not found_commits and not found_changeids and not found_gerrit_urls and not found_bug_refs and not similar_results:
        lines.append("\n未找到关联的提交记录。可能该 Task 尚未完成，或提交记录未记录在禅道中。")

    return "\n".join(lines)
