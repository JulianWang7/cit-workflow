"""credentials — LLM 凭证读取。

优先级:
1. 环境变量 (ZCODE_MODEL + ZCODE_BASE_URL + ZCODE_API_KEY)
2. ~/.bugfix-flow/llm.yaml  ← bugfix-batch 专用默认（如内网 GLM）
3. ~/.zcode/v2/config.json  ← ZCode app 配置（回退）

纯 stdlib，不依赖任何 ZCode 内部模块。

可被 orchestrator 和 zcode_adapter 使用，也可独立调用：
    python -m bugflow.credentials  → 打印 env 三元组
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path


def _config_path() -> Path:
    """ZCode v2 config.json 路径。"""
    # 允许环境变量覆盖（测试用）
    if "ZCODE_CONFIG_PATH" in os.environ:
        return Path(os.environ["ZCODE_CONFIG_PATH"])
    return Path.home() / ".zcode" / "v2" / "config.json"


def _load_llm_yaml() -> dict[str, str]:
    """读 ~/.bugfix-flow/llm.yaml → {model, base_url, api_key}。

    缺失或字段不全返回空 dict。
    """
    try:
        from bugflow.core.config import load_llm_config
        cfg = load_llm_config()
    except Exception:
        return {}
    model = cfg.get("model", "").strip()
    base_url = cfg.get("base_url", "").strip()
    api_key = cfg.get("api_key", "").strip()
    if model and base_url and api_key:
        return {"model": model, "base_url": base_url, "api_key": api_key}
    return {}


@lru_cache(maxsize=4)
def load_zcode_credentials(provider_hint: str = "") -> dict[str, str]:
    """读取 LLM 凭证，返回 env 三元组。

    返回 {"ZCODE_MODEL": ..., "ZCODE_BASE_URL": ..., "ZCODE_API_KEY": ...}

    选择逻辑：
    1. 环境变量已全设 → 直接用（最高优先级）
    2. ~/.bugfix-flow/llm.yaml → bugfix-batch 专用默认（provider_hint 为空时生效）
    3. provider_hint 非空 → 按 provider ID 子串或 model name 子串匹配 config.json
    4. config.json → 找第一个有 apiKey+baseURL 的 provider

    Raises FileNotFoundError if config.json 不存在且 llm.yaml 也没配。
    Raises ValueError if 没有找到有效 provider。
    """
    # 1. 环境变量已全设 → 直接用（provider_hint 忽略）
    env_model = os.environ.get("ZCODE_MODEL", "")
    env_url = os.environ.get("ZCODE_BASE_URL", "")
    env_key = os.environ.get("ZCODE_API_KEY", "")
    if env_model and env_url and env_key:
        return {
            "ZCODE_MODEL": env_model,
            "ZCODE_BASE_URL": env_url,
            "ZCODE_API_KEY": env_key,
        }

    # 2. llm.yaml（provider_hint 为空时，用 bugfix-batch 专用默认）
    if not provider_hint:
        llm = _load_llm_yaml()
        if llm:
            return {
                "ZCODE_MODEL": llm["model"],
                "ZCODE_BASE_URL": llm["base_url"],
                "ZCODE_API_KEY": llm["api_key"],
            }

    # 3. 读 config.json
    cfg_path = _config_path()
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"ZCode config.json 不存在: {cfg_path}\n"
            f"请先在 ZCode 中配置 provider，或设置 ZCODE_MODEL/ZCODE_BASE_URL/ZCODE_API_KEY 环境变量。"
        )

    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)

    providers = config.get("provider", {})
    if not isinstance(providers, dict) or not providers:
        raise ValueError("config.json 中无 provider 配置")

    # 3. 收集所有有 apiKey+baseURL 的 provider
    candidates = []
    for pid, p in providers.items():
        if not isinstance(p, dict):
            continue
        opts = p.get("options", {})
        if not isinstance(opts, dict):
            continue
        api_key = opts.get("apiKey", "")
        base_url = opts.get("baseURL", "")
        if not api_key or not base_url:
            continue
        # model 列表
        models = p.get("models", {})
        model_name = ""
        model_names: list[str] = []
        if isinstance(models, dict) and models:
            model_name = next(iter(models.keys()))
            model_names = list(models.keys())
        elif isinstance(models, list) and models:
            model_name = models[0] if isinstance(models[0], str) else models[0].get("name", "")
            model_names = [
                m if isinstance(m, str) else m.get("name", "") for m in models
            ]

        # provider_hint 匹配：ID 子串 或 model name 子串
        hint = (provider_hint or "").strip().lower()
        if hint:
            matches = (
                hint in pid.lower()
                or any(hint in mn.lower() for mn in model_names)
            )
            if not matches:
                continue

        enabled = p.get("enabled")
        # enabled=True → 高优先级；enabled 缺失/None → 中优先级；enabled=False → 跳过
        if enabled is False:
            if p.get("systemDisabledReason"):
                continue
            candidates.append((0, model_name, base_url, api_key, pid))
        elif enabled is True:
            candidates.append((2, model_name, base_url, api_key, pid))
        else:
            # enabled 缺失 → 可用
            candidates.append((1, model_name, base_url, api_key, pid))

    if not candidates:
        if provider_hint:
            raise ValueError(
                f"config.json 中无匹配 '{provider_hint}' 的 provider（需要有 apiKey + baseURL）。"
            )
        raise ValueError(
            "config.json 中无可用 provider（需要有 apiKey + baseURL）。"
            "请在 ZCode 设置中添加 provider。"
        )

    # 按优先级排序：enabled=True > enabled缺失 > enabled=False
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, model_name, base_url, api_key, pid = candidates[0]

    if not model_name:
        raise ValueError(f"provider {pid} 无 model 配置")

    return {
        "ZCODE_MODEL": model_name,
        "ZCODE_BASE_URL": base_url,
        "ZCODE_API_KEY": api_key,
    }


def get_env_trio() -> dict[str, str]:
    """load_zcode_credentials 的公开别名。"""
    return load_zcode_credentials()


if __name__ == "__main__":
    try:
        creds = load_zcode_credentials()
        print(f"ZCODE_MODEL={creds['ZCODE_MODEL']}")
        print(f"ZCODE_BASE_URL={creds['ZCODE_BASE_URL']}")
        # 只打印 key 前 8 位 + ...（安全）
        key = creds["ZCODE_API_KEY"]
        masked = key[:8] + "..." if len(key) > 8 else "***"
        print(f"ZCODE_API_KEY={masked}")
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
