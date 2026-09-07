"""Atomic JSON IO for watch_state and events.jsonl (prefer formal run dir)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from citfix.run_log import emit_event


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def resolve_watch_run_dir(view: Any) -> Path:
    """Directory that owns ``watch/`` — prefer formal ``runs/<PRODUCT>/<run_id>/``."""
    formal_dir = getattr(view, "formal_dir", None)
    if formal_dir is not None:
        formal = Path(formal_dir)
        formal.mkdir(parents=True, exist_ok=True)
        return formal
    return Path(view.intermediate_dir)


resolve_watch_run_dir_for = resolve_watch_run_dir


def watch_dir(run_dir: Path) -> Path:
    d = Path(run_dir) / "watch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def watch_state_path(run_dir: Path) -> Path:
    return watch_dir(run_dir) / "watch_state.json"


def events_path(run_dir: Path) -> Path:
    return watch_dir(run_dir) / "events.jsonl"


def snapshots_dir(run_dir: Path) -> Path:
    d = watch_dir(run_dir) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".watch_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_watch_state(run_dir: Path) -> dict[str, Any]:
    p = watch_state_path(run_dir)
    # Transition: if formal empty but intermediate sibling has state, prefer existing later via caller
    if not p.is_file():
        return {
            "schema_version": "1.0",
            "watch_status": "ok",
            "nudge_count": 0,
            "infra_retry_count": 0,
            "product_retry_count": 0,
            "last_error_class": "",
            "last_action": "",
            "thread_id": None,
            "history_ref": "watch/events.jsonl",
        }
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {
            "schema_version": "1.0",
            "watch_status": "ok",
            "nudge_count": 0,
            "infra_retry_count": 0,
            "product_retry_count": 0,
            "parse_error": True,
        }


def load_watch_state_for_view(view: Any) -> dict[str, Any]:
    """Load from formal watch/; fall back to intermediate watch/ once during transition."""
    primary = resolve_watch_run_dir(view)
    if watch_state_path(primary).is_file():
        return load_watch_state(primary)
    inter = Path(view.intermediate_dir)
    if (
        getattr(view, "formal_dir", None) is not None
        and inter.is_dir()
        and watch_state_path(inter).is_file()
    ):
        return load_watch_state(inter)
    return load_watch_state(primary)


def save_watch_state(run_dir: Path, state: dict[str, Any]) -> Path:
    state = dict(state)
    state["updated_at"] = now_iso()
    path = watch_state_path(run_dir)
    atomic_write_json(path, state)
    return path


def append_watch_event(
    run_dir: Path,
    event: dict[str, Any],
    *,
    mirror_run_events: bool = True,
    formal_for_events: Path | None = None,
) -> None:
    """Append to watch/events.jsonl; optionally mirror actor=watch into run_events.jsonl."""
    path = events_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("ts", now_iso())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if not mirror_run_events:
        return
    target = formal_for_events or run_dir
    # Only mirror when target looks like a formal/run root (has or can host logs/)
    try:
        decision = str(row.get("decision") or row.get("event") or "watch")
        emit_event(
            target,
            actor="watch",
            event=decision if decision in ("nudge", "escalate", "observe") else "watch_event",
            summary=str(row.get("reason") or row.get("note") or decision),
            run_id=str(row.get("run_id") or ""),
            bug_id=str(row.get("bug_id") or ""),
            stage_id=str(row.get("stage") or row.get("stage_id") or ""),
            level="warn" if decision in ("nudge", "escalate") else "info",
            refs={
                k: row.get(k)
                for k in ("error_class", "nudge_count", "action", "dry_run", "snapshot")
                if k in row
            },
        )
    except Exception:
        pass
