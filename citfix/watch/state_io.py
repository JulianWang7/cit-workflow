"""Atomic JSON IO for watch_state and events.jsonl."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def watch_dir(intermediate_run_dir: Path) -> Path:
    d = intermediate_run_dir / "watch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def watch_state_path(intermediate_run_dir: Path) -> Path:
    return watch_dir(intermediate_run_dir) / "watch_state.json"


def events_path(intermediate_run_dir: Path) -> Path:
    return watch_dir(intermediate_run_dir) / "events.jsonl"


def snapshots_dir(intermediate_run_dir: Path) -> Path:
    d = watch_dir(intermediate_run_dir) / "snapshots"
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


def load_watch_state(intermediate_run_dir: Path) -> dict[str, Any]:
    p = watch_state_path(intermediate_run_dir)
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


def save_watch_state(intermediate_run_dir: Path, state: dict[str, Any]) -> Path:
    state = dict(state)
    state["updated_at"] = now_iso()
    path = watch_state_path(intermediate_run_dir)
    atomic_write_json(path, state)
    return path


def append_watch_event(intermediate_run_dir: Path, event: dict[str, Any]) -> None:
    path = events_path(intermediate_run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("ts", now_iso())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
