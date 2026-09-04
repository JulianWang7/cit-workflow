"""Shared prepare helpers (title gate, attachments, context gates)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from citfix.models import RunContext
from citfix.paths import to_repo_relative
from citfix.stages.util_io import (
    _ensure_repo_on_path,
    _log,
    _parse_download_path,
    _sha256_file,
)

def _cit_title_ok(title: str, steps: str = "") -> bool:
    text = f"{title}\n{steps}"
    return bool(re.search(r"CIT|【CIT】|\[CIT问题\]", text, re.I))


def _localize_attachments(
    ctx: RunContext,
    attachments: list[Any],
    log_path: Path,
) -> list[dict[str, Any]]:
    """Download zentao attachments into the run attachments dir; fill local_path/sha256."""
    att_dir = ctx.run_dir / "06_context_snapshot" / "input" / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    if not attachments:
        return []

    _ensure_repo_on_path(ctx.paths.repo_root)
    from bugflow.core import zentao

    localized: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        entry = dict(att)
        entry.setdefault("local_path", "")
        entry.setdefault("sha256", "")
        file_id = int(entry.get("id") or 0)
        if file_id <= 0:
            entry["missing"] = True
            entry["download_error"] = "invalid attachment id"
            localized.append(entry)
            continue
        try:
            msg = zentao.download_attachment(int(ctx.bug_id), file_id, str(att_dir))
            path = _parse_download_path(msg)
            if path is None:
                candidates = sorted(
                    [p for p in att_dir.glob("*") if p.is_file()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                path = candidates[0] if candidates else None
            if path is None or not path.is_file():
                entry["missing"] = True
                entry["download_error"] = msg if isinstance(msg, str) else "download produced no file"
                _log(log_path, f"attachment {file_id} missing: {entry['download_error']}")
            else:
                entry["local_path"] = to_repo_relative(ctx.paths.repo_root, path.resolve())
                entry["sha256"] = _sha256_file(path)
                entry["missing"] = False
                entry.pop("download_error", None)
                _log(log_path, f"attachment {file_id} -> {entry['local_path']}")
        except Exception as e:
            entry["missing"] = True
            entry["local_path"] = ""
            entry["sha256"] = ""
            entry["download_error"] = str(e)
            _log(log_path, f"attachment {file_id} download failed: {e}")
        localized.append(entry)
    return localized


def _compute_gates(
    *,
    server: str,
    code_root: str,
    device_serial: str,
    cit_ok: bool,
    attachments: list[dict[str, Any]],
    allow_missing_attachments: bool,
    device_label_verified: bool = False,
    require_device_label_verified: bool = True,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Return (gates, validation) — analyze vs reproduce/flash-verify split."""
    errors: list[str] = []
    if not server:
        errors.append("workspace.server empty")
    if not code_root:
        errors.append("workspace.code_root empty")
    if not cit_ok:
        errors.append("CIT title gate not satisfied")

    missing_atts = [a for a in attachments if a.get("missing") is True]
    if attachments and missing_atts:
        ids = ",".join(str(a.get("id")) for a in missing_atts)
        if allow_missing_attachments:
            errors.append(f"attachments missing (ids={ids}) but allow_missing_attachments=true")
        else:
            errors.append(f"attachments not localized (ids={ids})")

    if not device_serial:
        errors.append("device_serial empty — bind device before reproduce")

    if require_device_label_verified and not device_label_verified:
        errors.append(
            "device_label_verified=false — verify device CIT label / serial binding "
            "then set context.source.device_label_verified=true"
        )

    ready_analyze = bool(server) and bool(code_root) and cit_ok
    if attachments and missing_atts and not allow_missing_attachments:
        ready_analyze = False

    ready_reproduce = ready_analyze and bool(device_serial)
    ready_flash_verify = ready_reproduce and (
        device_label_verified or not require_device_label_verified
    )

    validation = {
        "ok": ready_analyze,
        "errors": errors,
    }
    gates = {
        "ready_for_analyze": ready_analyze,
        "ready_for_reproduce": ready_reproduce,
        "ready_for_flash_verify": ready_flash_verify,
        "device_label_verified": bool(device_label_verified),
    }
    return gates, validation


def _load_project_info(ctx: RunContext, product: str) -> dict[str, Any] | None:
    info = ctx.paths.repo_root / "plan_bank" / product / "project_info.json"
    if info.is_file():
        return json.loads(info.read_text(encoding="utf-8"))
    return None

