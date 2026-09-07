"""Formal run event log (runs/<PRODUCT>/<run_id>/logs/run_events.jsonl).

Bypass observability layer: write failures must not block the pipeline.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ACTORS = frozenset({"engine", "agent", "watch", "mcp", "cli"})
LEVELS = frozenset({"info", "warn", "error"})

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|auth|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_INLINE_RE = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|authorization|secret)\b\s*[=:]\s*([^\s,;]+)"
)

RUN_EVENTS_NAME = "run_events.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def logs_dir(formal_run_dir: Path, *, create: bool = False) -> Path:
    d = Path(formal_run_dir) / "logs"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def run_events_path(formal_run_dir: Path) -> Path:
    return logs_dir(formal_run_dir) / RUN_EVENTS_NAME


def stage_logs_dir(formal_run_dir: Path) -> Path:
    d = logs_dir(formal_run_dir, create=True) / "stages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_log_file(formal_run_dir: Path, run_id: str, stage_id: str) -> Path:
    return stage_logs_dir(formal_run_dir) / f"{run_id}_{stage_id}.log"


def redact_text(text: str) -> str:
    if not text:
        return text
    return _SENSITIVE_INLINE_RE.sub(r"\1=***", str(text))


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY_RE.search(str(key or "")):
        return "***"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value]
    return value


def redact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in event.items():
        out[str(k)] = _redact_value(str(k), v)
    if "summary" in out and isinstance(out["summary"], str):
        out["summary"] = redact_text(out["summary"])
    return out


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Fill defaults and coerce to schema-shaped dict (does not raise on soft issues)."""
    row = redact_event(dict(event))
    row.setdefault("ts", now_iso())
    row["run_id"] = str(row.get("run_id") or "")
    row["bug_id"] = str(row.get("bug_id") or "")
    row["stage_id"] = str(row.get("stage_id") or "")
    actor = str(row.get("actor") or "engine")
    if actor not in ACTORS:
        actor = "engine"
    row["actor"] = actor
    row["event"] = str(row.get("event") or "unknown")
    level = str(row.get("level") or "info")
    if level not in LEVELS:
        level = "info"
    row["level"] = level
    row["summary"] = redact_text(str(row.get("summary") or ""))
    refs = row.get("refs")
    if not isinstance(refs, dict):
        refs = {}
    row["refs"] = _redact_value("refs", refs)
    return row


def append_run_event(
    formal_run_dir: Path | str,
    event: Mapping[str, Any],
    *,
    raise_on_error: bool = False,
) -> Path | None:
    """Append one JSONL row under formal run logs/. Never blocks pipeline by default."""
    try:
        path = run_events_path(Path(formal_run_dir))
        row = normalize_event(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        return path
    except Exception as e:  # noqa: BLE001 — observability must not break workflow
        print(f"[citfix.run_log] append failed: {e}", file=sys.stderr)
        if raise_on_error:
            raise
        return None


def emit_event(
    formal_run_dir: Path | str,
    *,
    actor: str,
    event: str,
    summary: str,
    run_id: str = "",
    bug_id: str = "",
    stage_id: str = "",
    level: str = "info",
    refs: Mapping[str, Any] | None = None,
    raise_on_error: bool = False,
) -> Path | None:
    return append_run_event(
        formal_run_dir,
        {
            "run_id": run_id,
            "bug_id": bug_id,
            "stage_id": stage_id,
            "actor": actor,
            "event": event,
            "level": level,
            "summary": summary,
            "refs": dict(refs or {}),
        },
        raise_on_error=raise_on_error,
    )


def read_run_events(
    formal_run_dir: Path | str,
    *,
    limit: int | None = None,
    skip_bad_lines: bool = True,
) -> list[dict[str, Any]]:
    """Read events; skip unparseable lines by default."""
    path = run_events_path(Path(formal_run_dir))
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            if not skip_bad_lines:
                raise
    if limit is not None and limit >= 0:
        return rows[-limit:]
    return rows


def append_stage_log_line(log_path: Path, msg: str) -> None:
    """Append a timestamped line to a stage detail log (formal logs/stages/)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {redact_text(msg)}\n")
