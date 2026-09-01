"""citfix batch API stubs — contract reserved, business not implemented.

Aligned with workflow/citfix_batch_api.json and docs/citfix-batch-api.md.
Single-bug /citfix remains the active path; these entry points only reserve shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"
STATUS_RESERVED = "reserved"
ERROR_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

_RESOLVE_RESULTS = frozenset({"resolved", "active", "closed", "skip", "manual"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _stub_envelope(
    *,
    api: str,
    batch_id: str = "",
    operator: str = "",
    bug_ids: list[int] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": STATUS_RESERVED,
        "api": api,
        "batch_id": batch_id or "",
        "operator": operator or "",
        "bug_ids": list(bug_ids or []),
        "stats": {
            "total": len(bug_ids or []),
            "succeeded": 0,
            "failed": 0,
            "skipped": len(bug_ids or []),
            "deduped_removed": 0,
            "retried": 0,
        },
        "items": [],
        "errors": [
            {
                "bug_id": None,
                "error_code": ERROR_NOT_IMPLEMENTED,
                "retryable": False,
                "message": (
                    f"{api} is a reserved stub; batch business logic is not implemented. "
                    "Use single-bug /citfix {bug_id} for now."
                ),
            }
        ],
        "created_at": _now_iso(),
        "finished_at": _now_iso(),
    }
    if extra:
        body.update(extra)
    return body


def _parse_bug_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        if raw.startswith("["):
            raw = json.loads(raw)
        else:
            raw = [x.strip() for x in raw.split(",") if x.strip()]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("bug_ids must be a list or comma-separated string")
    out: list[int] = []
    for x in raw:
        out.append(int(x))
    return out


def validate_resolve_request(req: dict[str, Any]) -> list[str]:
    """Return list of validation errors (empty = ok for stub accept)."""
    errs: list[str] = []
    if not isinstance(req, dict):
        return ["request must be an object"]
    if not req.get("operator"):
        errs.append("operator is required")
    if not req.get("resolve_result"):
        errs.append("resolve_result is required")
    elif str(req["resolve_result"]) not in _RESOLVE_RESULTS:
        errs.append(
            f"resolve_result must be one of {sorted(_RESOLVE_RESULTS)}"
        )
    try:
        bug_ids = _parse_bug_ids(req.get("bug_ids"))
    except (TypeError, ValueError) as e:
        errs.append(f"bug_ids invalid: {e}")
        bug_ids = []
    overrides = req.get("per_bug_overrides") or []
    if not bug_ids and not overrides:
        errs.append("bug_ids or per_bug_overrides must provide at least one id")
    return errs


def stub_batch_resolve(request: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Reserved: citfix_batch_resolve — no zentao writeback, no local batch state."""
    req = dict(request or {})
    req.update({k: v for k, v in kwargs.items() if v is not None and v != ""})
    v_errs = validate_resolve_request(req)
    if v_errs:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": STATUS_RESERVED,
            "api": "citfix_batch_resolve",
            "error_code": "INVALID_REQUEST",
            "errors": [
                {
                    "bug_id": None,
                    "error_code": "INVALID_REQUEST",
                    "retryable": False,
                    "message": msg,
                }
                for msg in v_errs
            ],
            "items": [],
            "stats": {
                "total": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "deduped_removed": 0,
                "retried": 0,
            },
            "created_at": _now_iso(),
            "finished_at": _now_iso(),
        }
    bug_ids = _parse_bug_ids(req.get("bug_ids"))
    for item in req.get("per_bug_overrides") or []:
        if isinstance(item, dict) and item.get("bug_id") is not None:
            bid = int(item["bug_id"])
            if bid not in bug_ids:
                bug_ids.append(bid)
    return _stub_envelope(
        api="citfix_batch_resolve",
        batch_id=str(req.get("batch_id") or ""),
        operator=str(req.get("operator") or ""),
        bug_ids=bug_ids,
        extra={"resolve_result": req.get("resolve_result"), "comment": req.get("comment") or ""},
    )


def stub_batch_query(batch_id: str = "", date: str = "") -> dict[str, Any]:
    return _stub_envelope(
        api="citfix_batch_query",
        batch_id=batch_id,
        extra={"date": date or ""},
    )


def stub_batch_retry(batch_id: str = "", bug_ids: Any = None) -> dict[str, Any]:
    try:
        ids = _parse_bug_ids(bug_ids)
    except (TypeError, ValueError):
        ids = []
    return _stub_envelope(api="citfix_batch_retry", batch_id=batch_id, bug_ids=ids)


def stub_batch_stats(batch_id: str = "") -> dict[str, Any]:
    return _stub_envelope(api="citfix_batch_stats", batch_id=batch_id)


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
