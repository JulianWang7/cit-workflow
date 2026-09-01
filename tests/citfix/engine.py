"""CIT /citfix workflow engine — resumable multi-stage pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import stages
from .models import Blocker, StageRecord, StageStatus
from .paths import PipelinePaths, load_pipeline_config

@dataclass
class WorkflowState:
    schema_version: str
    pipeline_id: str
    bug_id: str
    run_id: str
    entry_mode: str
    current_stage: str
    workflow_status: str
    updated_at: str
    stages: dict[str, StageRecord]
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pipeline_id": self.pipeline_id,
            "bug_id": self.bug_id,
            "run_id": self.run_id,
            "entry_mode": self.entry_mode,
            "current_stage": self.current_stage,
            "workflow_status": self.workflow_status,
            "updated_at": self.updated_at,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "checkpoint": self.checkpoint,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _kb_path(paths: PipelinePaths, stage_cfg: dict[str, Any]) -> Path:
    return paths.kb_pipeline_root / stage_cfg.get("kb_file", "")


def _init_stage_records(pipeline: dict[str, Any], paths: PipelinePaths) -> dict[str, StageRecord]:
    records: dict[str, StageRecord] = {}
    for st in pipeline["stages"]:
        sid = st["id"]
        kb = _kb_path(paths, st)
        records[sid] = StageRecord(
            status=StageStatus.PENDING,
            kb_doc=st.get("kb_doc", ""),
            kb_doc_path=str(kb) if kb.is_file() else "",
        )
    return records


def find_run_for_bug(paths: PipelinePaths, bug_id: str) -> Path | None:
    runs = paths.test_bed_root / "00_runs"
    if not runs.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for d in runs.iterdir():
        if not d.is_dir():
            continue
        wf = d / "workflow_state.json"
        if wf.is_file():
            import json

            try:
                data = json.loads(wf.read_text(encoding="utf-8"))
                if str(data.get("bug_id")) == str(bug_id):
                    candidates.append((data.get("updated_at", ""), d))
            except Exception:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def load_state(run_dir: Path) -> WorkflowState | None:
    import json

    wf = run_dir / "workflow_state.json"
    if not wf.is_file():
        return None
    raw = json.loads(wf.read_text(encoding="utf-8"))
    stage_records: dict[str, StageRecord] = {}
    for sid, rec in (raw.get("stages") or {}).items():
        blocker = None
        if rec.get("blocker"):
            b = rec["blocker"]
            blocker = Blocker(
                step=b.get("step", ""),
                reason=b.get("reason", ""),
                completed_context=b.get("completed_context") or {},
                next_actions=b.get("next_actions") or [],
            )
        stage_records[sid] = StageRecord(
            status=StageStatus(rec.get("status", "pending")),
            kb_doc=rec.get("kb_doc", ""),
            kb_doc_path=rec.get("kb_doc_path", ""),
            kb_doc_read=bool(rec.get("kb_doc_read")),
            started_at=rec.get("started_at", ""),
            completed_at=rec.get("completed_at", ""),
            outputs=rec.get("outputs") or {},
            blocker=blocker,
            log_path=rec.get("log_path", ""),
        )
    return WorkflowState(
        schema_version=raw.get("schema_version", "1.0"),
        pipeline_id=raw.get("pipeline_id", "citfix"),
        bug_id=str(raw.get("bug_id", "")),
        run_id=raw.get("run_id", ""),
        entry_mode=raw.get("entry_mode", "citfix_direct"),
        current_stage=raw.get("current_stage", ""),
        workflow_status=raw.get("workflow_status", "running"),
        updated_at=raw.get("updated_at", ""),
        stages=stage_records,
        checkpoint=raw.get("checkpoint") or {},
    )


def save_state(paths: PipelinePaths, state: WorkflowState) -> None:
    import json

    state.updated_at = _now_iso()
    run_dir = paths.test_bed_root / "00_runs" / state.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    wf = run_dir / "workflow_state.json"
    wf.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    checkpoint_md = run_dir / "CHECKPOINT.md"
    if state.workflow_status == "blocked" and state.checkpoint:
        cp = state.checkpoint
        lines = [
            f"# CIT Workflow Checkpoint — {state.run_id}",
            "",
            f"- **Bug ID**: {state.bug_id}",
            f"- **Blocked at**: {cp.get('stage', state.current_stage)}",
            f"- **Step**: {cp.get('step', '')}",
            f"- **Reason**: {cp.get('reason', '')}",
            "",
            "## Completed context",
            "",
            "```json",
            json.dumps(cp.get("completed_context") or {}, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Next actions",
            "",
        ]
        for i, act in enumerate(cp.get("next_actions") or [], 1):
            lines.append(f"{i}. {act}")
        lines.extend(["", "## Resume", "", f"```", f"/citfix {state.bug_id} --resume", f"```", ""])
        checkpoint_md.write_text("\n".join(lines), encoding="utf-8")
    elif state.workflow_status == "completed":
        checkpoint_md.write_text(
            f"# CIT Workflow Completed — {state.run_id}\n\nAll stages finished.\n",
            encoding="utf-8",
        )


def _make_run_id(bug_id: str, paths: PipelinePaths) -> str:
    date = datetime.now().strftime("%Y%m%d")
    base = f"CIT-{date}-{bug_id}"
    runs = paths.test_bed_root / "00_runs"
    if not (runs / base).exists():
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i:03d}"
        if not (runs / candidate).exists():
            return candidate
    return f"{base}-{datetime.now().strftime('%H%M%S')}"


def _set_checkpoint(state: WorkflowState, stage_id: str, blocker: Blocker) -> None:
    state.workflow_status = "blocked"
    state.current_stage = stage_id
    state.checkpoint = {
        "stage": stage_id,
        "step": blocker.step,
        "reason": blocker.reason,
        "completed_context": blocker.completed_context,
        "next_actions": blocker.next_actions,
        "resume_command": f"/citfix {state.bug_id} --resume",
    }


def run_pipeline(
    bug_id: str,
    *,
    resume: bool = False,
    run_id: str | None = None,
    force_new: bool = False,
) -> WorkflowState:
    paths, pipeline = load_pipeline_config()
    skip_ids = set(pipeline["entry_modes"]["citfix_direct"].get("skip_stages") or [])

    run_dir: Path | None = None
    state: WorkflowState | None = None

    if not force_new:
        if run_id:
            run_dir = paths.test_bed_root / "00_runs" / run_id
        else:
            run_dir = find_run_for_bug(paths, bug_id)
        if run_dir and run_dir.is_dir():
            state = load_state(run_dir)
            if state and state.workflow_status == "completed" and not resume:
                state = None
                run_dir = None

    if state is None:
        rid = run_id or _make_run_id(bug_id, paths)
        run_dir = paths.test_bed_root / "00_runs" / rid
        state = WorkflowState(
            schema_version="1.0",
            pipeline_id=pipeline["pipeline_id"],
            bug_id=str(bug_id),
            run_id=rid,
            entry_mode="citfix_direct",
            current_stage="",
            workflow_status="running",
            updated_at=_now_iso(),
            stages=_init_stage_records(pipeline, paths),
        )

    ctx = stages.RunContext(
        paths=paths,
        pipeline=pipeline,
        bug_id=str(bug_id),
        run_id=state.run_id,
        run_dir=run_dir or paths.test_bed_root / "00_runs" / state.run_id,
        skip_stage_ids=skip_ids,
    )

    for stage_cfg in pipeline["stages"]:
        sid = stage_cfg["id"]
        rec = state.stages[sid]

        if rec.status == StageStatus.COMPLETED:
            continue
        if rec.status == StageStatus.SKIPPED:
            continue
        if sid in skip_ids:
            rec.status = StageStatus.SKIPPED
            rec.completed_at = _now_iso()
            save_state(paths, state)
            continue

        # If blocked from prior run and not resuming explicitly, still stop at checkpoint
        if rec.status == StageStatus.BLOCKED and not resume and state.workflow_status == "blocked":
            _set_checkpoint(state, sid, rec.blocker or Blocker("unknown", "blocked"))
            save_state(paths, state)
            return state

        # Retry blocked stage when resuming or continuing an in-progress run
        if rec.status == StageStatus.BLOCKED:
            rec.blocker = None

        rec.status = StageStatus.RUNNING
        rec.started_at = _now_iso()
        kb = _kb_path(paths, stage_cfg)
        if kb.is_file():
            rec.kb_doc_path = str(kb)
            rec.kb_doc_read = True
        state.current_stage = sid
        save_state(paths, state)

        log_path = ctx.stage_log_path(stage_cfg)
        rec.log_path = str(log_path)

        try:
            result = stages.execute_stage(stage_cfg, ctx)
        except Exception as e:
            blocker = Blocker(
                step=f"{sid}.execute",
                reason=str(e),
                completed_context={"run_id": state.run_id, "bug_id": bug_id},
                next_actions=[
                    f"Fix error: {e}",
                    f"/citfix {bug_id} --resume",
                ],
            )
            rec.status = StageStatus.BLOCKED
            rec.blocker = blocker
            _set_checkpoint(state, sid, blocker)
            save_state(paths, state)
            return state

        if result.skipped:
            rec.status = StageStatus.SKIPPED
            rec.completed_at = _now_iso()
            save_state(paths, state)
            continue

        if result.blocker:
            rec.status = StageStatus.BLOCKED
            rec.blocker = result.blocker
            rec.outputs = result.outputs
            _set_checkpoint(state, sid, result.blocker)
            save_state(paths, state)
            return state

        rec.status = StageStatus.COMPLETED
        rec.completed_at = _now_iso()
        rec.outputs = result.outputs
        rec.blocker = None
        save_state(paths, state)

    state.workflow_status = "completed"
    state.current_stage = "14_closure"
    state.checkpoint = {"message": "All stages completed", "run_id": state.run_id}
    save_state(paths, state)
    return state
