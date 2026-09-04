"""Human-case registry: runtime source of truth for verification_mode.

AI may only propose; Engine reads approved registry entries.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from citfix.paths import to_repo_relative

VERIFY_MODES = frozenset({"auto", "human", "hybrid"})

# Force human (or proposal) when registry misses — never silent auto.
HIGH_RISK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"主副?\s*MIC|MIC\s*测试|loopback|回环",
        r"喇叭|听筒|扬声器|耳机|音频|声压|分贝|\d+\s*dB\b",
        r"按键|物理键|侧键|电源键",
        r"触摸|触控|手写",
        r"相机|摄像头|闪光|对焦|拍照",
        r"振动|马达",
        r"夹具|拆机|测试点",
        r"主观|目视|人工|手测",
    )
)


def registry_path(repo_root: Path, product: str) -> Path:
    # Sanitize product for filename; keep readable Chinese where possible.
    safe = re.sub(r'[<>:"/\\|?*]', "_", product.strip()) or "DEFAULT"
    return repo_root / "config" / "citfix" / "human_case_registry" / f"{safe}.json"


def load_registry(repo_root: Path, product: str) -> dict[str, Any]:
    path = registry_path(repo_root, product)
    if not path.is_file():
        return {
            "schema_version": "1.0",
            "product": product,
            "cases": [],
            "default_execution": "auto",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {
            "schema_version": "1.0",
            "product": product,
            "cases": [],
            "default_execution": "auto",
        }
    data.setdefault("cases", [])
    data.setdefault("default_execution", "auto")
    return data


def _text_blob(title: str, steps: str) -> str:
    return f"{title}\n{steps}"


def match_registry_cases(
    registry: dict[str, Any], title: str, steps: str = ""
) -> list[dict[str, Any]]:
    blob = _text_blob(title, steps)
    hit: list[dict[str, Any]] = []
    for case in registry.get("cases") or []:
        if not isinstance(case, dict):
            continue
        match = case.get("match") if isinstance(case.get("match"), dict) else {}
        ok = False
        title_re = str(match.get("title_regex") or "").strip()
        if title_re:
            try:
                if re.search(title_re, blob, re.I):
                    ok = True
            except re.error:
                pass
        cls = str(match.get("class") or "").strip()
        if cls and cls in blob:
            ok = True
        case_id = str(case.get("case_id") or "")
        if case_id and case_id in blob:
            ok = True
        if ok:
            hit.append(case)
    return hit


def high_risk_hit(title: str, steps: str = "") -> bool:
    blob = _text_blob(title, steps)
    return any(p.search(blob) for p in HIGH_RISK_PATTERNS)


def resolve_verification(
    repo_root: Path,
    product: str,
    title: str,
    steps: str = "",
) -> dict[str, Any]:
    """Return tasks[].verification object for normalize / context routing."""
    registry = load_registry(repo_root, product)
    matched = match_registry_cases(registry, title, steps)
    human_cases: list[dict[str, Any]] = []
    auto_cases: list[dict[str, Any]] = []
    for c in matched:
        entry = {
            "case_id": c.get("case_id"),
            "execution": c.get("execution"),
            "reason": c.get("reason"),
        }
        ex = str(c.get("execution") or "").lower()
        if ex == "human":
            human_cases.append(entry)
        elif ex == "auto":
            auto_cases.append(entry)
        elif ex == "hybrid":
            human_cases.append(entry)
            auto_cases.append(entry)

    if human_cases and auto_cases:
        mode = "hybrid"
        source = "registry"
        confidence = "high"
        needs_proposal = False
    elif human_cases:
        mode = "human"
        source = "registry"
        confidence = "high"
        needs_proposal = False
    elif auto_cases:
        mode = "auto"
        source = "registry"
        confidence = "high"
        needs_proposal = False
    elif high_risk_hit(title, steps):
        mode = "human"
        source = "risk_heuristic"
        confidence = "medium"
        needs_proposal = True
        human_cases.append(
            {
                "case_id": "unregistered_high_risk",
                "execution": "human",
                "reason": "high_risk_keyword_without_registry_hit",
            }
        )
    else:
        default = str(registry.get("default_execution") or "auto").lower()
        mode = default if default in VERIFY_MODES else "auto"
        source = "registry_default"
        confidence = "low"
        needs_proposal = False

    result = {
        "mode": mode,
        "human_cases": human_cases,
        "auto_cases": auto_cases,
        "classification_source": source,
        "classification_confidence": confidence,
        "needs_proposal": needs_proposal,
        "registry_ref": to_repo_relative(repo_root, registry_path(repo_root, product)),
        # Preserved classification for later human-gate re-enable
        "classified_mode": mode,
        "classified_human_cases": list(human_cases),
        "classified_auto_cases": list(auto_cases),
    }
    return result


def apply_runtime_verify_policy(
    verification: dict[str, Any],
    *,
    force_auto: bool,
) -> dict[str, Any]:
    """Keep classification marks; optionally force runtime verify_mode=auto.

    Extension point: set ``force_auto=False`` (or pipeline gate
    ``verify_runtime_force_auto=false``) to restore human/hybrid gate behavior
    using ``classified_*`` fields.
    """
    ver = dict(verification or {})
    if "classified_mode" not in ver:
        ver["classified_mode"] = ver.get("mode")
        ver["classified_human_cases"] = list(ver.get("human_cases") or [])
        ver["classified_auto_cases"] = list(ver.get("auto_cases") or [])
    if not force_auto:
        return ver
    ver["mode"] = "auto"
    ver["human_cases"] = []
    ver["needs_proposal"] = False
    ver["runtime_policy"] = "force_auto_bootstrap"
    ver["runtime_note"] = (
        "classified_* retained; human/manual interfaces kept but not blocking "
        "while bootstrap runs all cases as auto"
    )
    return ver
