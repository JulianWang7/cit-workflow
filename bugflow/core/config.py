"""配置读取：servers.yaml + bug 目标 env。

servers.yaml 放 ~/.bugfix-flow/servers.yaml（或 userConfig.config_dir 指定的目录），
含 SSH 主机/端口/用户/key_file。敏感信息走文件，不进 userConfig。

orchestrator 通过 env 把当前 bug 目标传给 ZCode 会话（→ MCP server）：
  BUGFIX_SERVER=252
  BUGFIX_DEVICE_SERIAL=xxx
  BUGFIX_CODE_ROOT=/home7/yangwei/SRM965
MCP server 读这些 env 确定操作目标，与 orchestrator 同源。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml


def _config_dir() -> Path:
    """配置目录：BUGFIX_CONFIG_DIR env（插件注入 userConfig）> ~/.bugfix-flow。"""
    env = os.environ.get("BUGFIX_CONFIG_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".bugfix-flow"


def _servers_yaml_path() -> Path:
    return _config_dir() / "servers.yaml"


@lru_cache(maxsize=1)
def _load_servers() -> dict:
    """读取 servers.yaml（缓存），合并预置默认服务器。

    DEFAULT_SERVERS 提供已知的内网服务器（host/port 固定），
    servers.yaml 中的用户配置覆盖默认值。缺失文件时返回默认服务器。

    YAML 中未加引号的数字键（如 252:）会被 PyYAML 解析为 int，
    这里统一转成 str，确保 load_server_config("252") 能匹配。
    """
    p = _servers_yaml_path()
    user_servers: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            raw = data.get("servers", {}) if isinstance(data, dict) else {}
            user_servers = {str(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, yaml.YAMLError):
            user_servers = {}
    # merge: 预置服务器底，用户配置覆盖
    merged = {k: dict(v) for k, v in DEFAULT_SERVERS.items()}
    for name, cfg in user_servers.items():
        if name in merged:
            merged[name].update(cfg)
        else:
            merged[name] = cfg
    return merged


def load_server_config(server_name: str) -> dict:
    """读 ~/.bugfix-flow/servers.yaml → {host, port, user, key_file, password, code_roots}。

    支持 "server:port" 覆盖语法（如 "252:1804"）。
    缺服务器报 ValueError，调用方转成清晰提示。
    """
    name = server_name or ""
    port_override: int | None = None
    if ":" in name:
        base, port_str = name.rsplit(":", 1)
        try:
            port_override = int(port_str)
            name = base
        except ValueError:
            pass  # 保留原名，让它以"未知服务器"报错

    servers = _load_servers()
    if name not in servers:
        avail = list(servers.keys())
        raise ValueError(
            f"未知服务器: {server_name}。可用: {avail or '(空 — 请检查 ~/.bugfix-flow/servers.yaml)'}"
        )
    cfg = dict(servers[name])
    if port_override is not None:
        cfg["port"] = port_override
    return cfg


def list_servers() -> list[str]:
    """列出已配置的服务器名。"""
    return list(_load_servers().keys())


# ── 外部独立 APK 源码仓库配置 ────────────────────────────────────

def _external_sources_yaml_path() -> Path:
    return _config_dir() / "external_sources.yaml"


@lru_cache(maxsize=1)
def load_external_sources() -> dict:
    """读取 external_sources.yaml（缓存），合并预置默认仓库信息。

    返回 {repo_key: {repo_url, branch, description, servers: {server_name: {path: ...}}}}
    repo_url/branch/description 从 DEFAULT_EXTERNAL_SOURCES 预置，用户只需在
    servers 下填自己服务器上的仓库根路径。
    """
    p = _external_sources_yaml_path()
    user_cfg: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            user_cfg = data.get("external_sources", {}) if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            user_cfg = {}
    # merge: 预置默认 → 用户配置（后者覆盖前者）
    merged = {k: dict(v) for k, v in DEFAULT_EXTERNAL_SOURCES.items()}
    for key, cfg in user_cfg.items():
        if key in merged:
            merged[key].update(cfg)
        else:
            merged[key] = cfg
        # servers 里的键统一转 str（YAML 数字键问题）
        servers = merged[key].get("servers", {})
        if isinstance(servers, dict):
            merged[key]["servers"] = {str(k): v for k, v in servers.items()}
    return merged


def load_external_source(repo_key: str, server: str = "") -> dict:
    """读取指定仓库在指定 server 上的源码路径。

    Args:
        repo_key: 仓库标识（如 "meiglink"）
        server: 服务器名（如 "252"），空则用当前工作区 server

    Returns:
        {"path": "/home/.../MeiGLink", "repo_url": "...", "branch": "..."}
        path 为空串表示该 server 未配置路径。
    """
    if not server:
        env = get_bugfix_env()
        server = env.get("server", "")
    sources = load_external_sources()
    repo = sources.get(repo_key, {})
    servers = repo.get("servers", {})
    server_cfg = servers.get(server, {})
    return {
        "path": server_cfg.get("path", ""),
        "repo_url": repo.get("repo_url", ""),
        "branch": repo.get("branch", ""),
    }


def save_external_source(repo_key: str, server: str, path: str) -> dict:
    """写入/更新指定仓库在指定 server 上的源码路径。幂等，原子写。

    repo_url/branch/description 从 DEFAULT_EXTERNAL_SOURCES 预置补入，
    用户不需提供。同一 repo 可配多个 server 的路径。

    Args:
        repo_key: 仓库标识（如 "meiglink"）
        server: 服务器名（如 "252"）
        path: 仓库在该服务器上的根路径

    Returns:
        {"ok": True, "repo_key": ..., "server": ..., "path": str(yaml_path)}
    """
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    yaml_path = _external_sources_yaml_path()
    # 读已有配置（保留其他 repo 和其他 server 的路径）
    existing: dict = {}
    if yaml_path.is_file():
        try:
            with yaml_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            existing = loaded.get("external_sources", {}) if isinstance(loaded, dict) else {}
        except (OSError, yaml.YAMLError):
            existing = {}

    # 取/建该 repo 的配置，补预置默认
    repo_cfg = existing.get(repo_key, {})
    for k, v in DEFAULT_EXTERNAL_SOURCES.get(repo_key, {}).items():
        repo_cfg.setdefault(k, v)
    # 更新该 server 的路径
    servers = repo_cfg.get("servers", {})
    if not isinstance(servers, dict):
        servers = {}
    servers[str(server)] = {"path": path}
    repo_cfg["servers"] = servers
    existing[repo_key] = repo_cfg

    # 原子写 YAML
    tmp = config_dir / "external_sources.yaml.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"external_sources": existing}, f, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, yaml_path)

    # 清缓存
    load_external_sources.cache_clear()

    return {
        "ok": True,
        "repo_key": repo_key,
        "server": server,
        "path": str(yaml_path),
    }


# ── 预置默认值（内网地址固定，用户只需配账号密码） ────────────────
DEFAULTS: dict[str, dict] = {
    "zentao": {
        "base_url": "http://192.168.0.166:9100",
    },
    "gerrit": {
        "base_url": "https://192.168.0.240",
        "auth_mode": "basic_session",
    },
    "wiki": {
        "base_url": "http://192.168.0.138/wiki",
    },
    "daily_report": {
        "base_url": "http://192.168.0.18/daily",
    },
}

# 预置 SSH 服务器（地址/端口固定，用户只需提供用户名和密钥/密码）
DEFAULT_SERVERS: dict[str, dict] = {
    "252": {
        "host": "172.16.10.252",
        "port": 2204,
        "user": "",  # 用户自行配置
        "key_file": "~/.ssh/id_rsa",
    },
}

# 预置外部独立 APK 源码仓库（地址/分支固定，用户只需提供服务器上的路径）
# CIT/FQCTest/RuninTest 等独立 APK 源码不在系统源码树中，在此仓库单独管理。
# 用户通过 config(action="save", config_type="external_source") 配置各服务器的路径。
DEFAULT_EXTERNAL_SOURCES: dict[str, dict] = {
    "meiglink": {
        "repo_url": "ssh://yangwei@192.168.0.240:29418/MeiGLink",
        "branch": "master",
        "description": "独立 APK 源码仓库（CIT/FQCTest/RuninTest 等）",
    },
}

# 预置内网 LLM provider（openai-custom-new，地址固定）
# 模型配置存在 ~/.bugfix-flow/providers/custom/openai-custom-new.json
DEFAULT_LLM_PROVIDER_FILE = "openai-custom-new.json"

# API 实测的上下文窗口（2026-08-09 对 http://192.168.0.18/v1 探测）
VERIFIED_CONTEXT_WINDOWS: dict[str, int] = {
    "glm-5.2": 131072,           # API 在 132024 tokens 处拒绝，精确确认
    "deepseek-v4-flash": 500000,  # API 接受 500K（nginx ~1MB body 限制，600K 413）
    "deepseek-v4-pro": 500000,    # 同上
    "qwen3.7-plus": 500000,       # 同上
}

# API 实测的思考行为（用于 model_detect 判断 effort 配置是否合理）
VERIFIED_REASONING: dict[str, dict] = {
    "glm-5.2": {
        "thinks_by_default": True,
        "effort_effective": None,
        "recommended_effort": "xhigh",
    },
    "deepseek-v4-flash": {
        "thinks_by_default": True,
        "effort_effective": None,
        "recommended_effort": None,
    },
    "deepseek-v4-pro": {
        "thinks_by_default": True,
        "effort_effective": None,
        "recommended_effort": "xhigh",
    },
    "qwen3.7-plus": {
        "thinks_by_default": True,
        "effort_effective": None,
        "recommended_effort": None,
    },
}


def _zentao_yaml_path() -> Path:
    return _config_dir() / "zentao.yaml"


@lru_cache(maxsize=1)
def load_zentao_config() -> dict:
    """读 ~/.bugfix-flow/zentao.yaml → {base_url, user, password, mcp_token, mcp_secret}。

    缺失返回空 dict，由调用方报清晰错误。
    和 servers.yaml 同目录，敏感信息走文件不进 userConfig。
    """
    p = _zentao_yaml_path()
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("zentao", {}) if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def reload_config() -> None:
    """清缓存，重读所有 YAML（测试/改配置后用）。"""
    _load_servers.cache_clear()
    load_zentao_config.cache_clear()
    load_gerrit_config.cache_clear()
    load_wiki_config.cache_clear()
    load_daily_report_config.cache_clear()
    load_llm_config.cache_clear()
    load_external_sources.cache_clear()
    # HttpClient singleton 需要重置才能用新凭据。
    try:
        from . import gerrit as _g, wiki as _w, daily_report as _dr
        _g._client_cache = None
        _g._client_cache_key = None
        _w._wiki_client = None
        _w._wiki_logged_in = False
        _dr._client = None
    except (ImportError, AttributeError):
        pass
    # zentao Web 登录态 + bug/task 缓存也需重置，否则换账号后仍用旧 session。
    try:
        from . import zentao as _zt
        _zt._zt_jar = None
        _zt._zt_logged_in = False
        _zt._bug_cache.clear()
        _zt._task_cache.clear()
    except (ImportError, AttributeError):
        pass


def _llm_yaml_path() -> Path:
    return _config_dir() / "llm.yaml"


@lru_cache(maxsize=1)
def load_llm_config() -> dict:
    """读 ~/.bugfix-flow/llm.yaml → {model, base_url, api_key}。

    bugfix-batch 专用的 LLM 默认配置，优先级高于 ZCode config.json。
    缺失返回空 dict，回退到 ZCode config.json。
    """
    p = _llm_yaml_path()
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("llm", {}) if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


# ── Gerrit 配置 ────────────────────────────────────────────────

def _gerrit_yaml_path() -> Path:
    return _config_dir() / "gerrit.yaml"


@lru_cache(maxsize=1)
def load_gerrit_config() -> dict:
    """读 ~/.bugfix-flow/gerrit.yaml → {base_url, user, password, auth_mode}。

    缺失返回预置默认值（base_url + auth_mode）。
    """
    p = _gerrit_yaml_path()
    user_cfg: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            user_cfg = data.get("gerrit", {}) if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            user_cfg = {}
    merged = dict(DEFAULTS.get("gerrit", {}))
    merged.update(user_cfg)
    # normalize: 旧文件可能写了 username，统一成 user
    if "username" in merged and "user" not in merged:
        merged["user"] = merged.pop("username")
    elif "username" in merged:
        merged.pop("username")
    return merged


# ── Wiki 配置 ──────────────────────────────────────────────────

def _wiki_yaml_path() -> Path:
    return _config_dir() / "wiki.yaml"


@lru_cache(maxsize=1)
def load_wiki_config() -> dict:
    """读 ~/.bugfix-flow/wiki.yaml → {base_url, user, password}。

    缺失返回预置默认值（base_url）。
    """
    p = _wiki_yaml_path()
    user_cfg: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            user_cfg = data.get("wiki", {}) if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            user_cfg = {}
    merged = dict(DEFAULTS.get("wiki", {}))
    merged.update(user_cfg)
    if "username" in merged and "user" not in merged:
        merged["user"] = merged.pop("username")
    elif "username" in merged:
        merged.pop("username")
    return merged


# ── 日报配置 ───────────────────────────────────────────────────

def _daily_report_yaml_path() -> Path:
    return _config_dir() / "daily_report.yaml"


@lru_cache(maxsize=1)
def load_daily_report_config() -> dict:
    """读 ~/.bugfix-flow/daily_report.yaml → {base_url, user, password}。

    缺失返回预置默认值（base_url）。
    """
    p = _daily_report_yaml_path()
    user_cfg: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            user_cfg = data.get("daily_report", {}) if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            user_cfg = {}
    merged = dict(DEFAULTS.get("daily_report", {}))
    merged.update(user_cfg)
    # normalize: 旧文件可能写了 username，统一成 user
    if "username" in merged and "user" not in merged:
        merged["user"] = merged.pop("username")
    elif "username" in merged:
        merged.pop("username")
    return merged


# ── LLM Provider JSON（model_detect / vision 用） ─────────────────

def _provider_json_path() -> Path:
    """内网 OpenAI 兼容 provider 的 JSON 配置文件路径。"""
    return _config_dir() / "providers" / "custom" / DEFAULT_LLM_PROVIDER_FILE


def _read_provider_json() -> dict:
    """读 provider JSON，返回 dict。文件不存在则报错。"""
    import json
    p = _provider_json_path()
    if not p.is_file():
        raise FileNotFoundError(
            f"provider 配置文件不存在: {p}\n"
            f"请先在 ~/.bugfix-flow/providers/custom/ 下创建 provider JSON。"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def _write_provider_json(data: dict) -> None:
    """原子写 provider JSON。"""
    import json
    p = _provider_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _find_model(provider_data: dict, model_id: str) -> dict | None:
    """在 models + extra_models 里找模型，返回模型 dict 引用（可原地修改）。"""
    for m in provider_data.get("models", []):
        if m.get("id") == model_id:
            return m
    for m in provider_data.get("extra_models", []):
        if m.get("id") == model_id:
            return m
    return None


def save_model_config(model_id: str, data: dict) -> dict:
    """修改内网 provider JSON 中单个模型的配置字段。

    改后需重启 MCP server 生效。支持字段：max_tokens, reasoning_effort,
    thinking_enabled, thinking_param_style, relay_reasoning,
    max_input_length, max_input_length_configured。
    """
    provider_data = _read_provider_json()
    model = _find_model(provider_data, model_id)
    if model is None:
        available = [m["id"] for m in provider_data.get("models", []) + provider_data.get("extra_models", [])]
        raise ValueError(f"模型 {model_id!r} 不在 provider 里。可用: {available}")

    changed: dict[str, tuple] = {}
    for key, new_val in data.items():
        old_val = model.get(key)
        if key == "reasoning_effort":
            gk = model.get("generate_kwargs") or {}
            old_gk = gk.get("reasoning_effort")
            gk["reasoning_effort"] = new_val
            model["generate_kwargs"] = gk
            if old_gk != new_val:
                changed["generate_kwargs.reasoning_effort"] = (old_gk, new_val)
        if old_val != new_val:
            model[key] = new_val
            changed[key] = (old_val, new_val)

    if not changed:
        return {"ok": True, "model": model_id, "changed": {}, "path": str(_provider_json_path()), "note": "无变更"}

    _write_provider_json(provider_data)
    return {
        "ok": True,
        "model": model_id,
        "changed": changed,
        "path": str(_provider_json_path()),
        "note": "需重启 MCP server 生效",
    }


def get_model_config_status() -> dict:
    """返回内网 provider 中所有模型的配置状态概览。"""
    try:
        provider_data = _read_provider_json()
    except FileNotFoundError:
        return {"available": False, "models": {}, "error": "provider 配置文件不存在"}

    models: dict[str, dict] = {}
    for m in provider_data.get("models", []) + provider_data.get("extra_models", []):
        mid = m.get("id", "?")
        configured_ctx = m.get("max_input_length", 131072)
        verified_ctx = VERIFIED_CONTEXT_WINDOWS.get(mid)
        # 压缩能否触发：configured=True 用 max_input_length，否则被目录覆盖
        compaction_ok = bool(m.get("max_input_length_configured"))
        # 配置值与实测值是否匹配（configured 值 ≤ verified 值才算合理）
        ctx_match = (
            verified_ctx is not None
            and compaction_ok
            and configured_ctx <= verified_ctx
        )
        models[mid] = {
            "max_tokens": m.get("max_tokens", 8192),
            "max_input_length": configured_ctx,
            "max_input_length_configured": m.get("max_input_length_configured", False),
            "verified_context_window": verified_ctx,
            "context_match": ctx_match,
            "thinking_enabled": m.get("thinking_enabled"),
            "reasoning_effort": m.get("reasoning_effort"),
            "relay_reasoning": m.get("relay_reasoning", True),
            "generate_kwargs": m.get("generate_kwargs", {}),
            "compaction_ok": compaction_ok,
        }
        # 思考行为判定：用实测数据判断 effort 配置是否合理
        vr = VERIFIED_REASONING.get(mid)
        if vr is not None:
            current_effort = m.get("reasoning_effort") or m.get("generate_kwargs", {}).get("reasoning_effort")
            recommended = vr.get("recommended_effort")
            models[mid]["recommended_effort"] = recommended
            models[mid]["effort_effective"] = vr.get("effort_effective")
            models[mid]["thinks_by_default"] = vr.get("thinks_by_default")
            models[mid]["reasoning_ok"] = (current_effort == recommended)
        else:
            models[mid]["reasoning_ok"] = None  # 未知模型，不判定
    return {"available": True, "models": models, "path": str(_provider_json_path())}


# ── save_config：AI 直接写配置（用户只说账号密码） ────────────────

# config_type → YAML 顶层 key 映射
_YAML_TOP_KEY: dict[str, str] = {
    "zentao": "zentao",
    "gerrit": "gerrit",
    "wiki": "wiki",
    "daily_report": "daily_report",
}


def save_config(config_type: str, data: dict) -> dict:
    """写入配置 YAML（自动补预置地址，用户只需提供账号密码）。

    内网地址（base_url 等）从 DEFAULTS 自动补入，用户 data 覆盖之。
    写后自动清缓存，后续 load_*_config() 立即读到新值。

    Args:
        config_type: "zentao" | "gerrit" | "wiki" | "daily_report" | "servers" | "external_source"
        data: 用户提供的字段。
              - zentao/gerrit/wiki/daily_report: {user, password, ...}（base_url 自动补）
              - servers: {"name": "252", "user": "...", "key_file": "...", ...}（host/port 自动补）
              - external_source: {"repo_key": "meiglink", "server": "252", "path": "/home/.../MeiGLink"}
                （repo_url/branch 自动补，配置独立 APK 源码仓库在各服务器上的路径）

    Returns:
        {"ok": True, "config_type": ..., "path": ..., "fields": [...]}
    """
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    if config_type == "servers":
        return _save_server_config(config_dir, data)

    if config_type == "external_source":
        repo_key = str(data.get("repo_key", "")).strip()
        server = str(data.get("server", "")).strip()
        path = str(data.get("path", "")).strip()
        if not repo_key or not server or not path:
            raise ValueError(
                "external_source 配置必须提供 repo_key, server, path"
                "（如 {'repo_key': 'meiglink', 'server': '252', "
                "'path': '/home/.../MeiGLink'}）"
            )
        return save_external_source(repo_key, server, path)

    top_key = _YAML_TOP_KEY.get(config_type)
    if not top_key:
        raise ValueError(
            f"未知 config_type: {config_type}。"
            f"可选: {list(_YAML_TOP_KEY.keys()) + ['servers', 'external_source']}"
        )

    # 读已有 YAML（保留用户之前写的字段）
    yaml_path = config_dir / f"{config_type}.yaml"
    existing: dict = {}
    if yaml_path.is_file():
        try:
            with yaml_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            existing = loaded.get(top_key, {}) if isinstance(loaded, dict) else {}
        except (OSError, yaml.YAMLError):
            existing = {}

    # merge: 预置默认 → 已有配置 → 新传入 data（后者覆盖前者）
    merged = dict(DEFAULTS.get(config_type, {}))
    merged.update(existing)
    merged.update(data)

    # normalize: 防止 AI 传 username 而非 user（历史问题，所有含 user 的配置统一）
    if config_type in ("gerrit", "daily_report", "zentao", "wiki") and "username" in merged:
        if "user" not in merged:
            merged["user"] = merged["username"]
        merged.pop("username", None)

    # 原子写 YAML（tmp + os.replace，防写一半崩溃）
    tmp = config_dir / f"{config_type}.yaml.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump({top_key: merged}, f, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, yaml_path)

    # 清缓存
    reload_config()

    return {
        "ok": True,
        "config_type": config_type,
        "path": str(yaml_path),
        "fields": list(merged.keys()),
    }


def _save_server_config(config_dir: Path, data: dict) -> dict:
    """写单个 SSH 服务器配置到 servers.yaml（合并预置地址）。"""
    server_name = str(data.get("name", "")).strip()
    if not server_name:
        raise ValueError("servers 配置必须提供 name 字段（服务器名，如 '252'）")

    yaml_path = config_dir / "servers.yaml"
    existing_servers: dict = {}
    if yaml_path.is_file():
        try:
            with yaml_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            raw = loaded.get("servers", {}) if isinstance(loaded, dict) else {}
            existing_servers = {str(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, yaml.YAMLError):
            existing_servers = {}

    # merge: 预置默认 → 已有配置 → 新传入 data（去掉 name 字段）
    merged = dict(DEFAULT_SERVERS.get(server_name, {}))
    merged.update(existing_servers.get(server_name, {}))
    user_data = {k: v for k, v in data.items() if k != "name" and v}
    merged.update(user_data)

    existing_servers[server_name] = merged
    # 原子写 YAML（tmp + os.replace，防写一半崩溃）
    tmp = config_dir / "servers.yaml.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"servers": existing_servers}, f, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, yaml_path)

    reload_config()

    return {
        "ok": True,
        "config_type": "servers",
        "path": str(yaml_path),
        "fields": list(merged.keys()),
    }


# ── get_config_status：配置状态概览 ────────────────────────────────

def get_config_status() -> dict:
    """返回所有配置的状态概览（哪些已配、哪些缺账号密码）。

    AI 用此函数判断还缺什么，引导用户补充。
    """
    zentao = load_zentao_config()
    gerrit = load_gerrit_config()
    wiki = load_wiki_config()
    daily = load_daily_report_config()
    servers = _load_servers()
    llm = load_llm_config()

    # workspace.json
    ws_path = _workspace_file()
    workspace = {}
    if ws_path.is_file():
        try:
            import json
            with open(ws_path, "r", encoding="utf-8") as f:
                workspace = json.load(f)
        except Exception:
            logger.debug("workspace.json 读取失败，按空处理", exc_info=True)

    # 外部源码仓库（独立 APK 源码，如 MeiGLink）
    env_server = workspace.get("server", "") or os.environ.get("BUGFIX_SERVER", "")
    ext_sources = load_external_sources()
    ext_status = {}
    for key, repo in ext_sources.items():
        repo_servers = repo.get("servers", {})
        server_cfg = repo_servers.get(env_server, {})
        ext_status[key] = {
            "repo_url": repo.get("repo_url", ""),
            "path": server_cfg.get("path", ""),
            "configured": bool(server_cfg.get("path")),
        }

    return {
        "zentao": {
            "base_url": zentao.get("base_url", ""),
            "has_user": bool(zentao.get("user")),
            "has_password": bool(zentao.get("password")),
            "has_mcp": bool(zentao.get("mcp_token") and zentao.get("mcp_secret")),
            "ready": bool(zentao.get("user") and zentao.get("mcp_token")),
        },
        "gerrit": {
            "base_url": gerrit.get("base_url", ""),
            "has_user": bool(gerrit.get("user")),
            "has_password": bool(gerrit.get("password")),
            "ready": bool(gerrit.get("user") and gerrit.get("password")),
        },
        "wiki": {
            "base_url": wiki.get("base_url", ""),
            "has_user": bool(wiki.get("user")),
            "has_password": bool(wiki.get("password")),
            "ready": bool(wiki.get("user") and wiki.get("password")),
        },
        "daily_report": {
            "base_url": daily.get("base_url", ""),
            "has_user": bool(daily.get("user")),
            "has_password": bool(daily.get("password")),
            "ready": bool(daily.get("user") and daily.get("password")),
        },
        "servers": {
            name: {
                "host": cfg.get("host", ""),
                "has_user": bool(cfg.get("user")),
                "has_key_or_password": bool(cfg.get("key_file") or cfg.get("password")),
            }
            for name, cfg in servers.items()
        },
        "workspace": {
            "server": workspace.get("server", ""),
            "code_root": workspace.get("code_root", ""),
            "set": bool(workspace.get("server") and workspace.get("code_root")),
        },
        "external_sources": ext_status,
        "embedding": {
            "base_url": llm.get("embedding_base_url", ""),
            "has_api_key": bool(llm.get("embedding_api_key")),
            "has_model": bool(llm.get("embedding_model")),
            "ready": bool(
                llm.get("embedding_base_url") and llm.get("embedding_api_key")
            ),
        },
        "models": get_model_config_status(),
    }


def _workspace_file() -> Path:
    """workspace 持久化文件（跨进程/重启恢复）。"""
    return _config_dir() / "workspace.json"


def save_workspace(server: str = "", code_root: str = "", device_serial: str = "") -> None:
    """写 workspace.json（原子写），供 MCP server 重启后恢复。

    同时写 os.environ，使同进程后续调用即时生效。
    """
    import json

    if server:
        os.environ["BUGFIX_SERVER"] = server
    if code_root:
        os.environ["BUGFIX_CODE_ROOT"] = code_root
    if device_serial:
        os.environ["BUGFIX_DEVICE_SERIAL"] = device_serial

    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "server": server or os.environ.get("BUGFIX_SERVER", ""),
        "code_root": code_root or os.environ.get("BUGFIX_CODE_ROOT", ""),
        "device_serial": device_serial or os.environ.get("BUGFIX_DEVICE_SERIAL", ""),
    }
    tmp = d / "workspace.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _workspace_file())


def get_bugfix_env() -> dict:
    """读当前 bug 目标。

    优先级：env（orchestrator 注入）> workspace.json（MCP set_workspace 持久化）> 空串。

    返回 {server, device_serial, code_root}，任一缺失为空串。
    """
    env_server = os.environ.get("BUGFIX_SERVER", "")
    env_serial = os.environ.get("BUGFIX_DEVICE_SERIAL", "")
    env_root = os.environ.get("BUGFIX_CODE_ROOT", "")

    # env 全有 → 直接返回（orchestrator 模式）
    if env_server and env_root:
        return {"server": env_server, "device_serial": env_serial, "code_root": env_root}

    # env 缺 → 读 workspace.json 回退（MCP server 重启恢复）
    wf = _workspace_file()
    if wf.is_file():
        try:
            import json
            data = json.loads(wf.read_text(encoding="utf-8"))
            return {
                "server": env_server or data.get("server", ""),
                "device_serial": env_serial or data.get("device_serial", ""),
                "code_root": env_root or data.get("code_root", ""),
            }
        except (OSError, ValueError):
            pass

    return {"server": env_server, "device_serial": env_serial, "code_root": env_root}
