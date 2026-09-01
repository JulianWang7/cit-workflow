"""模型能力自动检测：探测内网 API 的实际上下文窗口、最大输出、思考行为。

用法：
    from bugflow.core.model_detect import detect_all, auto_configure

    # 只检测，报告结果
    results = detect_all()

    # 检测 + 自动配置（写回 provider JSON）
    results = auto_configure()

检测策略：
    1. max_output_tokens: 发 max_tokens=1000000，解析报错里的实际上限
    2. context_window: 递增发送大输入（130K→260K→500K），解析拒绝错误
    3. reasoning: 发中等难度题，对比默认 vs xhigh 的 reasoning_tokens

所有探测都用 urllib（标准库），不依赖额外包。
API key 解密用手动 Fernet（读 ~/.bugfix-flow/.master_key 文件）。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error

from .config import (
    _provider_json_path,
    _read_provider_json,
    save_model_config,
    VERIFIED_CONTEXT_WINDOWS,
    VERIFIED_REASONING,
)

logger = logging.getLogger("core.model_detect")


# ── API 凭据 ──────────────────────────────────────────────────────

def get_api_credentials() -> tuple[str, str]:
    """从 provider JSON 读取 base_url + 解密后的 api_key。

    解密用手动 Fernet（读 ~/.bugfix-flow/.master_key 文件）。
    """
    data = _read_provider_json()
    base_url = data.get("base_url", "").rstrip("/")
    enc_key = data.get("api_key", "")

    if not enc_key.startswith("ENC:"):
        return base_url, enc_key  # 未加密，直接用

    # 手动 Fernet（读 .master_key 文件）
    import base64
    from cryptography.fernet import Fernet

    from .config import _config_dir
    key_file = _config_dir() / ".master_key"
    key_hex = key_file.read_text(encoding="utf-8").strip()
    raw = bytes.fromhex(key_hex)
    fernet_key = base64.urlsafe_b64encode(raw[:32])
    f = Fernet(fernet_key)
    token = enc_key[4:].encode("ascii")
    return base_url, f.decrypt(token).decode("utf-8")


# ── HTTP 请求 ─────────────────────────────────────────────────────

def _post_chat(api_key: str, base_url: str, payload: dict, timeout: int = 120) -> dict:
    """发 POST /v1/chat/completions，返回 {"ok": True, "data": ...} 或 {"ok": False, "status": ..., "body": ...}。"""
    url = f"{base_url}/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return {"ok": True, "data": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": e.code, "body": resp_body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


def _get_models(api_key: str, base_url: str) -> list[str]:
    """GET /v1/models，返回模型 ID 列表。"""
    url = f"{base_url}/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


# ── 单项探测 ──────────────────────────────────────────────────────

# 解析 max_output_tokens 的正则模式（匹配各模型网关的错误格式）
_RE_MAX_TOKENS_PATTERNS = [
    re.compile(r"at most (\d+) completion tokens"),
    re.compile(r"range of max_tokens is \[1, (\d+)\]", re.IGNORECASE),
    re.compile(r"Range of max_tokens should be \[1, (\d+)\]"),
]

# 解析 context_window 的正则
_RE_CONTEXT_LENGTH = re.compile(r"context length \((\d+) tokens\)", re.IGNORECASE)
_RE_INPUT_TOKENS = re.compile(r"input \((\d+) tokens\)", re.IGNORECASE)


def probe_max_output_tokens(api_key: str, base_url: str, model_id: str) -> int | None:
    """发 max_tokens=1000000 触发报错，解析实际输出上限。"""
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1000000,
    }
    r = _post_chat(api_key, base_url, payload, timeout=30)
    if r["ok"]:
        # 居然接受了？说明模型不限制或网关不校验
        return 1000000
    body = r.get("body", "")
    for pat in _RE_MAX_TOKENS_PATTERNS:
        m = pat.search(body)
        if m:
            return int(m.group(1))
    return None


def probe_context_window(
    api_key: str, base_url: str, model_id: str, max_output: int = 5
) -> dict:
    """递增发送大输入，探测实际输入上下文窗口。

    返回 {"context_window": int, "method": "rejected_at"|"accepted_up_to"|"nginx_limited", "detail": str}
    """
    # 探测点：130K → 260K → 500K
    # 每个 'a ' ≈ 1 token，所以 N 个 'a ' ≈ N tokens
    probes = [130000, 260000, 500000]
    last_accepted = 0

    for target in probes:
        text = "a " * target
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": text + "OK."}],
            "max_tokens": max_output,
        }
        r = _post_chat(api_key, base_url, payload, timeout=180)
        if r["ok"]:
            usage = r["data"].get("usage", {})
            last_accepted = usage.get("prompt_tokens", target)
            continue

        status = r.get("status", 0)
        body = r.get("body", "")

        # 413 = nginx body 限制
        if status == 413:
            if last_accepted > 0:
                return {
                    "context_window": last_accepted,
                    "method": "nginx_limited",
                    "detail": f"nginx body 限制，最大接受 {last_accepted} tokens",
                }
            return {
                "context_window": target,
                "method": "nginx_limited",
                "detail": f"nginx 413，130K 已超 body 限制",
            }

        # 400 = 上下文超限，解析实际限制
        m_ctx = _RE_CONTEXT_LENGTH.search(body)
        m_in = _RE_INPUT_TOKENS.search(body)
        if m_ctx:
            limit = int(m_ctx.group(1))
            return {
                "context_window": limit,
                "method": "rejected_at",
                "detail": body[:150],
            }
        if m_in:
            # 有些错误只说 input tokens 不说 limit
            input_tokens = int(m_in.group(1))
            return {
                "context_window": input_tokens - (input_tokens % 1024),
                "method": "rejected_at",
                "detail": body[:150],
            }

        # 其他 400 错误
        if last_accepted > 0:
            return {
                "context_window": last_accepted,
                "method": "accepted_up_to",
                "detail": f"最后接受 {last_accepted} tokens，之后非 413 拒绝: {body[:100]}",
            }

    # 所有探测都接受了 → 上下文非常大，受 nginx body 限制
    return {
        "context_window": last_accepted or 500000,
        "method": "accepted_up_to",
        "detail": f"500K 全部接受，受 nginx body 限制无法测更高",
    }


def probe_reasoning(api_key: str, base_url: str, model_id: str) -> dict:
    """探测思考行为：默认是否思考 + reasoning_effort 各档位的效果。

    用 1 道中等难度题，每档（default/high/xhigh）重复 3 次取中位数。
    用同一道题可隔离题目难度干扰，只看 effort 对思考量的影响。

    返回 {
        "thinks_by_default": bool,
        "effort_effective": bool,       # xhigh vs default 差异是否显著
        "default_reasoning_tokens": int, # default 档中位数
        "high_reasoning_tokens": int,    # high 档中位数
        "xhigh_reasoning_tokens": int,   # xhigh 档中位数
        "recommended_effort": str|None,
    }
    """
    # 单道中等难度题，每档重复 5 次取平均值
    # 用同一道题可隔离题目难度干扰，只看 effort 对思考量的影响
    # 5 次 + 均值比 3 次 + 中位数更稳定（LLM reasoning_tokens 随机性大）
    question = (
        "一个水池有进水管和出水管，进水管单独注满要6小时，出水管单独排空要8小时。"
        "现在水池是空的，同时打开两个管，问几小时能注满？给出详细推理过程。"
    )
    trials = 5
    efforts = [None, "high", "xhigh"]
    # rt_matrix[effort] = [rt_trial1, ..., rt_trial5]
    rt_matrix: dict[str | None, list[int]] = {e: [] for e in efforts}
    has_rc_any = False

    for effort in efforts:
        for _ in range(trials):
            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": question}],
                "max_tokens": 32768,
            }
            if effort is not None:
                payload["reasoning_effort"] = effort
            r = _post_chat(api_key, base_url, payload, timeout=90)
            rt = _extract_reasoning_tokens(r)
            rt_matrix[effort].append(rt)
            if effort is None and _has_reasoning_content(r):
                has_rc_any = True

    # 取平均值（5 次均值比中位数更稳定）
    def _mean(lst: list[int]) -> int:
        return round(sum(lst) / len(lst)) if lst else 0

    rt_default = _mean(rt_matrix[None])
    rt_high = _mean(rt_matrix["high"])
    rt_xhigh = _mean(rt_matrix["xhigh"])

    thinks_by_default = rt_default > 0 or has_rc_any

    # 判断 effort 是否有效：xhigh vs default 差异超过 30%
    # （30% 阈值：5 次均值的随机波动约 ±20%，只有 >30% 才算真有增益）
    # 保守策略：不确定时不设 effort（模型默认就在思考，安全降级）
    if rt_default > 0:
        ratio = rt_xhigh / rt_default
    elif rt_xhigh > 0:
        ratio = 3.0  # 从 0 变成有思考
    else:
        ratio = 1.0
    effort_effective = abs(ratio - 1.0) > 0.30

    recommended = _recommend_effort(thinks_by_default, effort_effective, rt_default, rt_high, rt_xhigh)

    return {
        "thinks_by_default": thinks_by_default,
        "effort_effective": effort_effective,
        "default_reasoning_tokens": rt_default,
        "high_reasoning_tokens": rt_high,
        "xhigh_reasoning_tokens": rt_xhigh,
        "recommended_effort": recommended,
    }


def _extract_reasoning_tokens(response: dict) -> int:
    """从 API 响应中提取 reasoning_tokens。"""
    if not response.get("ok"):
        return 0
    usage = response["data"].get("usage", {})
    # 顶层 reasoning_tokens
    rt = usage.get("reasoning_tokens", 0)
    if rt:
        return rt
    # 嵌套在 completion_tokens_details 里
    details = usage.get("completion_tokens_details", {})
    if details:
        return details.get("reasoning_tokens", 0)
    return 0


def _has_reasoning_content(response: dict) -> bool:
    """检查响应是否有 reasoning_content 字段。"""
    if not response.get("ok"):
        return False
    choices = response["data"].get("choices", [])
    if not choices:
        return False
    msg = choices[0].get("message", {})
    rc = msg.get("reasoning_content")
    return bool(rc)


def _recommend_effort(
    thinks_by_default: bool,
    effort_effective: bool,
    rt_default: int,
    rt_high: int,
    rt_xhigh: int,
) -> str | None:
    """根据探测结果推荐 reasoning_effort 值。

    保守策略（reasoning_tokens 随机性大，避免误判）：
    - 不思考 + xhigh 能让它思考 → 推荐 xhigh
    - 不思考 + xhigh 也不思考 → None
    - 默认就思考 → None（默认已够，强行设 effort 可能反而降低）
    """
    if not thinks_by_default:
        return "xhigh" if rt_xhigh > 0 else None
    return None


# ── 推荐配置 ──────────────────────────────────────────────────────

def _build_recommendation(detection: dict) -> dict:
    """根据检测结果生成推荐配置。"""
    ctx = detection.get("context_window", 131072)
    max_out = detection.get("max_output_tokens", 32768)
    rec_effort = detection.get("recommended_effort")

    rec = {
        "max_tokens": min(max_out, 32768),  # 不超过 32768（够用且省资源）
        "max_input_length": min(ctx, 500000),  # 不超过 nginx body 限制
        "max_input_length_configured": True,
        "relay_reasoning": True,
    }

    if rec_effort is not None:
        rec["reasoning_effort"] = rec_effort
        rec["thinking_enabled"] = True
        rec["thinking_param_style"] = "effort"

    return rec


# ── 批量检测 + 自动配置 ───────────────────────────────────────────

def detect_all(model_ids: list[str] | None = None) -> dict:
    """检测所有（或指定）模型的能力。

    Args:
        model_ids: 要检测的模型 ID 列表，None=检测 provider JSON 里的全部模型

    Returns:
        {
            "base_url": str,
            "models": {
                "model_id": {
                    "context_window": int,
                    "max_output_tokens": int,
                    "thinks_by_default": bool,
                    "effort_effective": bool,
                    "default_reasoning_tokens": int,
                    "xhigh_reasoning_tokens": int,
                    "recommended_effort": str|None,
                    "recommended_config": dict,
                }
            }
        }
    """
    base_url, api_key = get_api_credentials()
    if not api_key:
        raise RuntimeError("无法解密 API key，检查 ~/.bugfix-flow/.master_key")

    provider_data = _read_provider_json()
    all_models = [m["id"] for m in provider_data.get("models", []) + provider_data.get("extra_models", [])]

    if model_ids is None:
        model_ids = all_models

    results: dict[str, dict] = {}
    for mid in model_ids:
        if mid not in all_models:
            results[mid] = {"error": f"模型 {mid} 不在 provider 里"}
            continue

        logger.info("正在检测模型 %s ...", mid)

        # 1. max output tokens
        max_out = probe_max_output_tokens(api_key, base_url, mid)

        # 2. context window
        ctx_result = probe_context_window(api_key, base_url, mid)
        ctx = ctx_result["context_window"]

        # 3. reasoning behavior
        reasoning = probe_reasoning(api_key, base_url, mid)

        detection = {
            "context_window": ctx,
            "context_method": ctx_result["method"],
            "context_detail": ctx_result["detail"],
            "max_output_tokens": max_out,
            "thinks_by_default": reasoning["thinks_by_default"],
            "effort_effective": reasoning["effort_effective"],
            "default_reasoning_tokens": reasoning["default_reasoning_tokens"],
            "high_reasoning_tokens": reasoning["high_reasoning_tokens"],
            "xhigh_reasoning_tokens": reasoning["xhigh_reasoning_tokens"],
            "recommended_effort": reasoning["recommended_effort"],
            "recommended_config": {},
        }
        detection["recommended_config"] = _build_recommendation(detection)
        results[mid] = detection

    return {"base_url": base_url, "models": results}


def auto_configure(model_ids: list[str] | None = None, dry_run: bool = False) -> dict:
    """检测 + 自动配置（写回 provider JSON）。

    Args:
        model_ids: 要配置的模型 ID 列表，None=全部
        dry_run: True=只报告将要做的更改，不实际写入

    Returns:
        {
            "base_url": str,
            "dry_run": bool,
            "models": {
                "model_id": {
                    "detection": {...},
                    "applied": bool,
                    "changes": {...} or None,
                }
            }
        }
    """
    detection_results = detect_all(model_ids)

    output: dict[str, dict] = {}
    for mid, det in detection_results["models"].items():
        if "error" in det:
            output[mid] = {"detection": det, "applied": False, "changes": None}
            continue

        rec = det.get("recommended_config", {})
        if dry_run:
            output[mid] = {
                "detection": det,
                "applied": False,
                "dry_run": True,
                "would_apply": rec,
            }
        else:
            try:
                result = save_model_config(mid, rec)
                output[mid] = {
                    "detection": det,
                    "applied": True,
                    "changes": result.get("changed", {}),
                }
            except Exception as e:
                output[mid] = {
                    "detection": det,
                    "applied": False,
                    "error": str(e),
                }

    return {
        "base_url": detection_results["base_url"],
        "dry_run": dry_run,
        "models": output,
    }
