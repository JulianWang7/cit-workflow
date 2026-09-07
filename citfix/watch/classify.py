"""Classify run/batch watch views into decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from citfix.watch.config import WatchConfig
from citfix.watch.scan import BatchWatchView, RunWatchView, parse_iso


@dataclass
class Decision:
    action: str  # ok | nudge | retry_wait | escalate | skip
    error_class: str = ""
    reason: str = ""
    wait_seconds: int = 0


def _age_seconds(updated: datetime | None, now: datetime) -> float | None:
    if updated is None:
        return None
    # normalize aware/naive
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=now.tzinfo or timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=updated.tzinfo)
    return (now - updated).total_seconds()


def classify_run(
    view: RunWatchView,
    watch_state: dict[str, Any],
    cfg: WatchConfig,
    *,
    now: datetime | None = None,
    last_event_age_seconds: float | None = None,
) -> Decision:
    now = now or datetime.now(timezone.utc).astimezone()
    if view.claim_only:
        return Decision(action="skip", reason="claim_only")
    if view.workflow_status == "completed":
        return Decision(action="ok", reason="completed")

    status = str(watch_state.get("watch_status") or "ok")
    if status == "escalated":
        return Decision(action="escalate", error_class=str(watch_state.get("last_error_class") or "ESCALATED"), reason="already_escalated")

    nudge_count = int(watch_state.get("nudge_count") or 0)
    age = _age_seconds(view.updated_at_dt, now)

    # wall clock from first observation
    first = parse_iso(str(watch_state.get("first_seen_at") or ""))
    if cfg.run_wall_clock_max_seconds > 0 and first:
        wall = _age_seconds(first, now)
        if wall is not None and wall > cfg.run_wall_clock_max_seconds:
            return Decision(
                action="escalate",
                error_class="RUN_WALL_CLOCK",
                reason=f"wall_clock>{cfg.run_wall_clock_max_seconds}s",
            )

    # retry_wait gate
    next_retry = parse_iso(str(watch_state.get("next_retry_at") or ""))
    if next_retry and _age_seconds(next_retry, now) is not None:
        if next_retry.tzinfo is None:
            next_retry = next_retry.replace(tzinfo=now.tzinfo)
        if next_retry > now:
            return Decision(
                action="retry_wait",
                error_class=str(watch_state.get("last_error_class") or "RETRY_WAIT"),
                reason="waiting_next_retry_at",
                wait_seconds=int((next_retry - now).total_seconds()),
            )

    stall_limit = cfg.stall_timeout_for(view.current_stage or "default")

    if view.workflow_status == "running":
        # Optional: no fresh run_events while workflow_status stays running
        if (
            cfg.event_heartbeat_enabled
            and last_event_age_seconds is not None
            and last_event_age_seconds >= cfg.event_heartbeat_timeout_seconds
        ):
            if nudge_count >= cfg.nudge_max:
                return Decision(
                    action="escalate",
                    error_class="EVENT_HEARTBEAT",
                    reason=f"no_run_event>{cfg.event_heartbeat_timeout_seconds}s nudge_exhausted",
                )
            return Decision(
                action="nudge",
                error_class="EVENT_HEARTBEAT",
                reason=(
                    f"event_heartbeat age={int(last_event_age_seconds)}s "
                    f"limit={cfg.event_heartbeat_timeout_seconds}s"
                ),
                wait_seconds=cfg.nudge_backoff(nudge_count),
            )
        if age is None:
            return Decision(action="nudge", error_class="WORKER_STALL", reason="missing_updated_at")
        if age >= stall_limit:
            if nudge_count >= cfg.nudge_max:
                return Decision(
                    action="escalate",
                    error_class="WORKER_STALL",
                    reason=f"stall>{stall_limit}s nudge_exhausted",
                )
            return Decision(
                action="nudge",
                error_class="WORKER_STALL",
                reason=f"running_stall age={int(age)}s limit={stall_limit}s",
                wait_seconds=cfg.nudge_backoff(nudge_count),
            )
        return Decision(action="ok", reason="running_fresh")

    if view.workflow_status == "blocked":
        reason_l = (view.checkpoint_reason or "").lower()
        if "safety" in reason_l or "neverallow" in reason_l:
            return Decision(action="escalate", error_class="SAFETY_GATE", reason=view.checkpoint_reason)
        # human gate: do not auto-nudge forever
        if "human" in reason_l or "pending_human" in reason_l:
            if age is not None and age >= cfg.stall_timeout_for("13_human_gate"):
                return Decision(
                    action="escalate",
                    error_class="WAITING_HUMAN",
                    reason="human_gate_timeout",
                )
            return Decision(action="ok", error_class="WAITING_HUMAN", reason="await_human")

        if age is not None and age >= cfg.blocked_nudge_after_seconds:
            if nudge_count >= cfg.nudge_max:
                return Decision(
                    action="escalate",
                    error_class="BLOCKED_STALE",
                    reason="blocked_nudge_exhausted",
                )
            return Decision(
                action="nudge",
                error_class="BLOCKED_STALE",
                reason=f"blocked_stale age={int(age)}s",
                wait_seconds=cfg.nudge_backoff(nudge_count),
            )
        return Decision(action="ok", reason="blocked_within_grace")

    return Decision(action="ok", reason=f"status={view.workflow_status}")


def classify_batch(
    view: BatchWatchView,
    cfg: WatchConfig,
    *,
    now: datetime | None = None,
) -> Decision:
    now = now or datetime.now(timezone.utc).astimezone()
    age = _age_seconds(view.updated_at_dt, now)
    if view.batch_status == "blocked" and view.current_bug_id:
        if age is not None and age >= cfg.blocked_nudge_after_seconds:
            return Decision(
                action="nudge",
                error_class="BATCH_STALL",
                reason=f"batch_blocked bug={view.current_bug_id}",
            )
        return Decision(action="ok", reason="batch_blocked_grace")
    if view.batch_status == "running" and view.current_item_status == "running":
        if age is not None and age >= cfg.batch_item_running_timeout_seconds:
            return Decision(
                action="nudge",
                error_class="BATCH_STALL",
                reason=f"batch_item_running_timeout bug={view.current_bug_id}",
            )
    return Decision(action="ok", reason="batch_ok")
