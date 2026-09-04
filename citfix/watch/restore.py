"""Restore workflow_state.json from a watch snapshot (file-only rollback)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from citfix.watch.state_io import append_watch_event, load_watch_state, save_watch_state


def restore_workflow_state(
    intermediate_run_dir: Path,
    snapshot_path: Path,
    *,
    formal_run_dir: Path | None = None,
) -> Path:
    """Copy snapshot back to intermediate (and formal if given) workflow_state.json."""
    if not snapshot_path.is_file():
        raise FileNotFoundError(snapshot_path)
    raw = snapshot_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or "schema_version" not in data:
        raise ValueError("snapshot missing schema_version — refuse restore")

    dest = intermediate_run_dir / "workflow_state.json"
    shutil.copy2(snapshot_path, dest)
    if formal_run_dir is not None:
        formal_run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_path, formal_run_dir / "workflow_state.json")

    ws = load_watch_state(intermediate_run_dir)
    ws["last_action"] = "restore_snapshot"
    ws["watch_status"] = "ok"
    ws["restore_from"] = str(snapshot_path)
    save_watch_state(intermediate_run_dir, ws)
    append_watch_event(
        intermediate_run_dir,
        {
            "decision": "restore",
            "action": "restore_snapshot",
            "snapshot": str(snapshot_path),
            "run_id": data.get("run_id"),
            "bug_id": data.get("bug_id"),
        },
    )
    return dest


def quarantine_output(stage_output_path: Path, reason: str) -> Path:
    """Move a bad output file/dir aside under sibling `_rejected/`."""
    parent = stage_output_path.parent
    rejected = parent / "_rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    stamp = stage_output_path.name
    dest = rejected / stamp
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    shutil.move(str(stage_output_path), str(dest))
    reason_path = rejected / f"{stamp}.reason.txt"
    reason_path.write_text(reason + "\n", encoding="utf-8")
    return dest
