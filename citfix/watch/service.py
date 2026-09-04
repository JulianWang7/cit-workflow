"""One watch cycle: scan → classify → act."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from citfix.paths import load_pipeline_config
from citfix.watch.actions import apply_escalate, apply_nudge, snapshot_workflow
from citfix.watch.classify import Decision, classify_batch, classify_run
from citfix.watch.config import WatchConfig, load_watch_config
from citfix.watch.scan import BatchWatchView, RunWatchView, iter_active_runs, iter_batches
from citfix.watch.state_io import append_watch_event, load_watch_state, now_iso, save_watch_state


def _ensure_first_seen(watch_state: dict[str, Any], view: RunWatchView) -> None:
    if not watch_state.get("first_seen_at"):
        watch_state["first_seen_at"] = now_iso()
    watch_state.setdefault("run_id", view.run_id)
    watch_state.setdefault("bug_id", view.bug_id)


def _mark_ok(view: RunWatchView, watch_state: dict[str, Any], decision: Decision) -> None:
    # Progress detected: reset stall marker when updated_at moved
    last = str(watch_state.get("last_seen_updated_at") or "")
    if view.updated_at and view.updated_at != last:
        watch_state["stall_since"] = None
        if watch_state.get("watch_status") == "nudge_pending":
            watch_state["watch_status"] = "ok"
            watch_state["nudge_count"] = 0
            watch_state["next_retry_at"] = None
    watch_state["last_seen_workflow_status"] = view.workflow_status
    watch_state["last_seen_stage"] = view.current_stage
    watch_state["last_seen_updated_at"] = view.updated_at
    watch_state["last_action"] = "observe"
    save_watch_state(view.intermediate_dir, watch_state)


def process_run(view: RunWatchView, cfg: WatchConfig, *, now: datetime | None = None) -> dict[str, Any]:
    watch_state = load_watch_state(view.intermediate_dir)
    _ensure_first_seen(watch_state, view)
    decision = classify_run(view, watch_state, cfg, now=now)
    result: dict[str, Any] = {
        "run_id": view.run_id,
        "bug_id": view.bug_id,
        "decision": decision.action,
        "error_class": decision.error_class,
        "reason": decision.reason,
    }
    if decision.action == "skip":
        return result
    if decision.action == "ok":
        _mark_ok(view, watch_state, decision)
        return result
    if decision.action == "retry_wait":
        watch_state["watch_status"] = "retry_wait"
        watch_state["last_action"] = "retry_wait"
        save_watch_state(view.intermediate_dir, watch_state)
        return result
    if decision.action == "nudge":
        detail = apply_nudge(
            view,
            cfg,
            watch_state,
            error_class=decision.error_class,
            reason=decision.reason,
            wait_seconds=decision.wait_seconds,
        )
        result["detail"] = {"snapshot": detail.get("snapshot"), "dry_run": cfg.nudge_dry_run}
        return result
    if decision.action == "escalate":
        detail = apply_escalate(
            view,
            cfg,
            watch_state,
            error_class=decision.error_class,
            reason=decision.reason,
        )
        result["detail"] = detail
        return result
    return result


def process_batch(view: BatchWatchView, cfg: WatchConfig, *, now: datetime | None = None) -> dict[str, Any]:
    decision = classify_batch(view, cfg, now=now)
    result: dict[str, Any] = {
        "batch_id": view.batch_id,
        "decision": decision.action,
        "error_class": decision.error_class,
        "reason": decision.reason,
        "bug_id": view.current_bug_id,
    }
    if decision.action != "nudge" or not view.current_bug_id:
        return result
    # Nudge the blocked/stalled child bug via a synthetic minimal view if its run exists
    from citfix.watch.scan import view_from_state_file

    projects = view.batch_dir.parents[1]  # .../projects/<PRODUCT>/batches/<id>
    # Prefer finding intermediate run for bug
    nudged = False
    for run_root in (projects / "runs").glob("CIT-*-" + view.current_bug_id):
        wf = run_root / "workflow_state.json"
        if wf.is_file():
            rv = view_from_state_file(wf)
            if rv:
                process_run(rv, cfg, now=now)
                nudged = True
                break
    if not nudged:
        # Still record batch-level event beside batch_state
        append_watch_event(
            view.batch_dir,
            {
                "batch_id": view.batch_id,
                "decision": "nudge",
                "error_class": decision.error_class,
                "reason": decision.reason,
                "bug_id": view.current_bug_id,
                "note": "child run not found; manual /citfix {bug} --resume",
            },
        )
        result["note"] = "child_run_not_found"
    return result


def run_watch_cycle(
    cfg: WatchConfig | None = None,
    *,
    now: datetime | None = None,
    include_batches: bool = True,
) -> dict[str, Any]:
    """Single scan cycle (used by cit_watch_once and APScheduler jobs)."""
    cfg = cfg or load_watch_config()
    paths, _ = load_pipeline_config()
    # Prefer cfg.workspace test_bed if repo differs
    projects = paths.projects_root()
    if cfg.workspace and (cfg.workspace / "runs_work" / "projects").is_dir():
        projects = cfg.workspace / "runs_work" / "projects"

    now = now or datetime.now(timezone.utc).astimezone()
    run_results = [process_run(v, cfg, now=now) for v in iter_active_runs(projects)]
    batch_results: list[dict[str, Any]] = []
    if include_batches:
        batch_results = [process_batch(b, cfg, now=now) for b in iter_batches(projects)]

    summary = {
        "ts": now_iso(),
        "runs_scanned": len(run_results),
        "batches_scanned": len(batch_results),
        "nudges": sum(1 for r in run_results if r.get("decision") == "nudge"),
        "escalations": sum(1 for r in run_results if r.get("decision") == "escalate"),
        "runs": run_results,
        "batches": batch_results,
        "nudge_dry_run": cfg.nudge_dry_run,
        "workspace": str(cfg.workspace),
    }
    return summary
