# -*- coding: utf-8 -*-
"""共享 HTTP 客户端 — 基于 Python 标准库 urllib，无额外依赖。

从 YWAgent tools/http_client.py 移植，去掉 YWAgent 框架依赖。
支持: Cookie 会话保持（持久化到磁盘）、Basic Auth、Basic+Session（Gerrit）、
Form 登录（MediaWiki/日报站）、SSL 跳过（内网自签名证书）。

Cookie 存储目录：~/.bugfix-flow/cookies/
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import LWPCookieJar
from pathlib import Path
from typing import Optional

from .config import _config_dir

logger = logging.getLogger("core.http_client")


def _cookie_dir() -> Path:
    """Cookie 持久化目录。"""
    d = _config_dir() / "cookies"
    d.mkdir(parents=True, exist_ok=True)
    # .gitignore 防泄露
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    return d


class HttpClient:
    """轻量 HTTP 客户端，支持 Cookie 持久化 + Basic Auth / Session / Form 登录。

    Args:
        base_url: 基础 URL。
        user / password: 凭据。
        auth_mode:
            - "basic": 仅 HTTP Basic Auth
            - "basic_session": Basic Auth + GET /login/ 获取会话 Cookie
              （适合 auth.type=HTTP 的内网 Gerrit）
            - "form": POST username/password 到 /login/（LDAP/表单登录）
        verify_ssl: 是否验证 SSL 证书。内网自签名证书设 False。
        cookie_file: Cookie 持久化文件名（如 "gerrit" → cookies/gerrit.txt）。
    """

    def __init__(
        self,
        base_url: str = "",
        user: str = "",
        password: str = "",
        auth_mode: str = "basic",
        verify_ssl: bool = True,
        cookie_file: str = "",
        auto_a_prefix: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.auth_mode = auth_mode
        self.verify_ssl = verify_ssl
        self.auto_a_prefix = auto_a_prefix
        self._lock = threading.Lock()
        self._logged_in = False
        self._login_attempted = False  # 避免每请求重试注定失败的 login（HTTP Credentials 站点 /login/ 也 401）

        # Cookie 持久化
        self._cookie_path: Optional[Path] = None
        if cookie_file:
            self._cookie_path = _cookie_dir() / f"{cookie_file}.txt"
            self.jar = LWPCookieJar(str(self._cookie_path))
            if self._cookie_path.exists():
                try:
                    self.jar.load(ignore_discard=True, ignore_expires=True)
                    if self._has_session_cookie():
                        self._logged_in = True
                        logger.info("从 %s 加载 %d 个 Cookie", self._cookie_path, len(self.jar))
                except Exception:
                    pass
        else:
            self.jar = LWPCookieJar()

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(
                context=self._create_ssl_context(verify_ssl)
            ),
        )

    def _save_cookies(self):
        if self._cookie_path:
            try:
                self.jar.save(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass

    # ── Public session API（供调用方管理登录态，避免触私有属性）──────

    @property
    def logged_in(self) -> bool:
        """当前是否已登录（可写，True 表示调用方已确认登录成功）。"""
        return self._logged_in

    @logged_in.setter
    def logged_in(self, value: bool) -> None:
        self._logged_in = bool(value)

    def save_cookies(self) -> None:
        """持久化当前 Cookie 到磁盘（公共别名）。"""
        self._save_cookies()

    @staticmethod
    def _create_ssl_context(verify: bool = True) -> ssl.SSLContext:
        if verify:
            return ssl.create_default_context()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ── Login ──────────────────────────────────────────────────

    def _has_session_cookie(self) -> bool:
        try:
            cookies = [c for c in self.jar if not c.is_expired()]
        except Exception:
            return False
        if not cookies:
            return False
        if any(c.name == "GerritAccount" for c in cookies):
            return True
        return self.auth_mode != "basic_session"

    def login(self) -> bool:
        if self.auth_mode not in ("form", "basic_session"):
            return False
        if not self.user or not self.password:
            return False
        if self._logged_in and self._has_session_cookie():
            return True
        try:
            login_url = f"{self.base_url}/login/"
            if self.auth_mode == "basic_session":
                req = urllib.request.Request(login_url, method="GET")
                self._add_auth(req)
            else:
                data = urllib.parse.urlencode(
                    {"username": self.user, "password": self.password}
                ).encode()
                req = urllib.request.Request(login_url, data=data, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with self._lock:
                with self.opener.open(req, timeout=15) as resp:
                    resp.read()
            self._logged_in = bool(self._has_session_cookie())
            self._login_attempted = True
            self._save_cookies()
            return self._logged_in
        except Exception as e:
            logger.warning("%s 登录失败: %s", self.auth_mode, e)
            self._logged_in = False
            self._login_attempted = True  # 标记已尝试，避免每请求重试
            return False

    # ── HTTP Methods ───────────────────────────────────────────

    def get(self, path: str = "", params: Optional[dict] = None,
            headers: Optional[dict] = None, timeout: int = 30) -> str:
        self._ensure_login()
        url = self._url(path, params)
        req = urllib.request.Request(url, headers=headers or {})
        self._add_auth(req)
        try:
            with self._lock:
                with self.opener.open(req, timeout=timeout) as resp:
                    result = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # Gerrit HTTP Credentials 站点：直连 401 → 自动用 /a/ 前缀重试
            if e.code == 401 and self.auto_a_prefix:
                a_path = self._a_prefix_for(path)
                if a_path is not None:
                    return self.get(a_path, params=params, headers=headers, timeout=timeout)
            raise
        self._save_cookies()
        return result

    def post(self, path: str = "", data: Optional[dict] = None,
             headers: Optional[dict] = None, timeout: int = 30) -> str:
        self._ensure_login()
        url = self._url(path)
        hdrs = dict(headers or {})
        body = None
        if data:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        self._add_auth(req)
        self._add_xsrf(req)
        try:
            with self._lock:
                with self.opener.open(req, timeout=timeout) as resp:
                    result = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # Gerrit HTTP Credentials 站点：直连 401 → 自动用 /a/ 前缀重试
            if e.code == 401 and self.auto_a_prefix:
                a_path = self._a_prefix_for(path)
                if a_path is not None:
                    return self.post(a_path, data=data, headers=headers, timeout=timeout)
            raise
        self._save_cookies()
        return result

    def put(self, path: str = "", data: Optional[dict] = None,
            headers: Optional[dict] = None, timeout: int = 30) -> str:
        """HTTP PUT — 用于 Gerrit set_topic 等 PUT 端点。

        与 post() 同构：auto_a_prefix 下 401 自动重试 /a/ 前缀。
        """
        self._ensure_login()
        url = self._url(path)
        hdrs = dict(headers or {})
        body = None
        if data:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="PUT")
        self._add_auth(req)
        self._add_xsrf(req)
        try:
            with self._lock:
                with self.opener.open(req, timeout=timeout) as resp:
                    result = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 401 and self.auto_a_prefix:
                a_path = self._a_prefix_for(path)
                if a_path is not None:
                    return self.put(a_path, data=data, headers=headers, timeout=timeout)
            raise
        self._save_cookies()
        return result

    def put_json(self, path: str = "", data: Optional[dict] = None,
                 timeout: int = 30) -> dict:
        text = self.put(path, data, timeout=timeout)
        if text.startswith(")]}'"):
            text = text[4:]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            snippet = text[:200].replace("\n", " ")
            raise json.JSONDecodeError(
                f"Response is not valid JSON (first 200 chars): {snippet!r}", text, 0
            ) from e

    def post_form(self, path: str = "", fields: Optional[dict] = None,
                  timeout: int = 30) -> str:
        self._ensure_login()
        url = self._url(path)
        body = urllib.parse.urlencode(fields or {}).encode()
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        self._add_auth(req)
        self._add_xsrf(req)
        try:
            with self._lock:
                with self.opener.open(req, timeout=timeout) as resp:
                    result = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # Gerrit HTTP Credentials 站点：直连 401 → 自动用 /a/ 前缀重试
            if e.code == 401 and self.auto_a_prefix:
                a_path = self._a_prefix_for(path)
                if a_path is not None:
                    return self.post_form(a_path, fields=fields, timeout=timeout)
            raise
        self._save_cookies()
        return result

    def get_json(self, path: str = "", params: Optional[dict] = None,
                 timeout: int = 30) -> dict:
        text = self.get(path, params, timeout=timeout)
        # Gerrit REST API returns ")]}'" prefix (XSSI protection)
        if text.startswith(")]}'"):
            text = text[4:]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            snippet = text[:200].replace("\n", " ")
            raise json.JSONDecodeError(
                f"Response is not valid JSON (first 200 chars): {snippet!r}", text, 0
            ) from e

    def post_json(self, path: str = "", data: Optional[dict] = None,
                  timeout: int = 30) -> dict:
        text = self.post(path, data, timeout=timeout)
        if text.startswith(")]}'"):
            text = text[4:]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            snippet = text[:200].replace("\n", " ")
            raise json.JSONDecodeError(
                f"Response is not valid JSON (first 200 chars): {snippet!r}", text, 0
            ) from e

    # ── Internal ───────────────────────────────────────────────

    def _url(self, path: str = "", params: Optional[dict] = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        if params:
            # doseq=True 让 list 值重复成多 key（Gerrit o= 选项需要 o=A&o=B）
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return url

    def _a_prefix_for(self, path: str) -> Optional[str]:
        """返回 Gerrit REST 路径的 /a/ 前缀版本。

        Gerrit HTTP Credentials 站点的 REST 必须走 /a/ 前缀（直连 401）。
        但 Gitiles 等 plugin 路径（/plugins/...）不走 /a/（会 404），返回 None。
        已带 /a/ 的路径返回 None（避免重复重试）。
        """
        p = (path or "").lstrip("/")
        if not p or p.startswith("a/"):
            return None
        if p.startswith("plugins/"):
            return None
        return f"/a/{p}"

    def _add_auth(self, req: urllib.request.Request):
        if self.auth_mode in ("basic", "basic_session") and self.user and self.password:
            credentials = base64.b64encode(
                f"{self.user}:{self.password}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {credentials}")

    def _xsrf_token(self) -> str:
        try:
            for c in self.jar:
                if c.name == "XSRF_TOKEN" and not c.is_expired():
                    return c.value or ""
        except Exception:
            pass
        return ""

    def _add_xsrf(self, req: urllib.request.Request):
        token = self._xsrf_token()
        if token and not req.has_header("X-Gerrit-Auth"):
            req.add_header("X-Gerrit-Auth", token)

    def _ensure_login(self):
        # login 已尝试且失败 → 不再每请求重试（HTTP Credentials 站点 /login/ 必 401）
        if self._login_attempted and not self._logged_in:
            return
        if self.auth_mode in ("form", "basic_session") and not self._logged_in:
            self.login()
        elif self.auth_mode == "basic_session" and not self._has_session_cookie():
            self._logged_in = False
            self.login()
