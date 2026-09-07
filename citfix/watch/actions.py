"""Watch actions: snapshot, CLI nudge, escalate notify."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from citfix.watch.config import WatchConfig
from citfix.watch.scan import RunWatchView
from citfix.watch.state_io import (
    append_watch_event,
    now_iso,
    resolve_watch_run_dir_for,
    save_watch_state,
    snapshots_dir,
)


def snapshot_workflow(view: RunWatchView) -> Path:
    """Copy workflow_state.json into watch/snapshots/ (prefer formal)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    home = resolve_watch_run_dir_for(view)
    dest = snapshots_dir(home) / f"workflow_state_{ts}.json"
    shutil.copy2(view.state_path, dest)
    return dest


def build_nudge_prompt(bug_id: str) -> str:
    return (
        f"Load citfix skill from this workspace. Execute /citfix {bug_id} --resume. "
        "Read workflow_state.json and CHECKPOINT.md first. "
        "Continue one stage per turn; stop at next checkpoint with full report."
    )


def nudge_citfix_cli(
    view: RunWatchView,
    cfg: WatchConfig,
    watch_state: dict[str, Any],
) -> dict[str, Any]:
    """Invoke Cursor CLI resume prompt. Honors nudge_dry_run."""
    prompt = build_nudge_prompt(view.bug_id)
    cmd = [cfg.cursor_bin, *cfg.cursor_args, str(cfg.workspace)]
    thread_id = watch_state.get("thread_id")
    if cfg.use_thread_resume and thread_id:
        cmd.extend(["--resume", str(thread_id)])
    cmd.append(prompt)

    result: dict[str, Any] = {
        "cmd": cmd,
        "dry_run": cfg.nudge_dry_run,
        "ok": True,
        "detail": "",
    }
    if cfg.nudge_dry_run:
        result["detail"] = "dry_run — command not executed"
        return result

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cfg.workspace),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        result["returncode"] = proc.returncode
        result["detail"] = (proc.stderr or proc.stdout or "")[:2000]
        result["ok"] = proc.returncode == 0
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["detail"] = str(e)
    return result


def schedule_next_retry(watch_state: dict[str, Any], wait_seconds: int) -> None:
    now = datetime.now(timezone.utc).astimezone()
    nxt = now + timedelta(seconds=max(0, int(wait_seconds)))
    watch_state["next_retry_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%S%z")


def apply_nudge(
    view: RunWatchView,
    cfg: WatchConfig,
    watch_state: dict[str, Any],
    *,
    error_class: str,
    reason: str,
    wait_seconds: int = 0,
) -> dict[str, Any]:
    home = resolve_watch_run_dir_for(view)
    snap = snapshot_workflow(view)
    cli = nudge_citfix_cli(view, cfg, watch_state)
    watch_state["nudge_count"] = int(watch_state.get("nudge_count") or 0) + 1
    watch_state["last_error_class"] = error_class
    watch_state["last_action"] = "cli_resume_dry" if cfg.nudge_dry_run else "cli_resume"
    watch_state["watch_status"] = "nudge_pending"
    watch_state["last_seen_workflow_status"] = view.workflow_status
    watch_state["last_seen_stage"] = view.current_stage
    watch_state["last_seen_updated_at"] = view.updated_at
    watch_state["stall_since"] = watch_state.get("stall_since") or now_iso()
    watch_state["run_id"] = view.run_id
    watch_state["bug_id"] = view.bug_id
    if wait_seconds > 0:
        schedule_next_retry(watch_state, wait_seconds)
    else:
        schedule_next_retry(watch_state, cfg.nudge_backoff(int(watch_state["nudge_count"])))
    save_watch_state(home, watch_state)
    append_watch_event(
        home,
        {
            "run_id": view.run_id,
            "bug_id": view.bug_id,
            "stage": view.current_stage,
            "decision": "nudge",
            "error_class": error_class,
            "reason": reason,
            "nudge_count": watch_state["nudge_count"],
            "action": watch_state["last_action"],
            "snapshot": str(snap),
            "cli_ok": cli.get("ok"),
            "dry_run": cfg.nudge_dry_run,
        },
        formal_for_events=view.formal_dir or home,
    )
    return {"snapshot": str(snap), "cli": cli, "watch_state": watch_state}


def apply_escalate(
    view: RunWatchView,
    cfg: WatchConfig,
    watch_state: dict[str, Any],
    *,
    error_class: str,
    reason: str,
) -> dict[str, Any]:
    home = resolve_watch_run_dir_for(view)
    snap = snapshot_workflow(view)
    watch_state["watch_status"] = "escalated"
    watch_state["last_error_class"] = error_class
    watch_state["last_action"] = "escalate"
    watch_state["run_id"] = view.run_id
    watch_state["bug_id"] = view.bug_id
    save_watch_state(home, watch_state)
    append_watch_event(
        home,
        {
            "run_id": view.run_id,
            "bug_id": view.bug_id,
            "stage": view.current_stage,
            "decision": "escalate",
            "error_class": error_class,
            "reason": reason,
            "nudge_count": watch_state.get("nudge_count"),
            "action": "escalate",
            "snapshot": str(snap),
        },
        formal_for_events=view.formal_dir or home,
    )
    notify_path = None
    if cfg.escalate_file_notify:
        base = view.formal_dir or home
        if view.formal_dir is None:
            formal_hint = (view.raw or {}).get("formal_run_dir")
            if formal_hint:
                cand = Path(formal_hint)
                if not cand.is_absolute():
                    cand = cfg.workspace / cand
                if cand.is_dir():
                    base = cand
        notify_dir = Path(base) / "14_closure" / "output" / "notifications"
        notify_dir.mkdir(parents=True, exist_ok=True)
        notify_path = notify_dir / f"watch_escalate_{view.run_id}.json"
        doc = {
            "channel": "file",
            "event_type": "RETRY_EXHAUSTED" if "exhaust" in reason else "SAFETY_STOP",
            "dedupe_key": f"{view.run_id}:watch_escalate:{error_class}",
            "delivered_at": now_iso(),
            "event": {
                "run_id": view.run_id,
                "bug_id": view.bug_id,
                "error_class": error_class,
                "reason": reason,
                "stage": view.current_stage,
            },
        }
        notify_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"snapshot": str(snap), "notify": str(notify_path) if notify_path else None}
