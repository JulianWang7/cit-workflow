"""CIT /citfix workflow engine — resumable multi-stage pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import stages
from .models import Blocker, StageRecord, StageStatus
from .paths import (
    PipelinePaths,
    UNASSIGNED_PROJECT,
    load_pipeline_config,
    read_product_from_run,
    sanitize_product_dirname,
    to_repo_relative,
)

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
    # Prefer current kb_file; fall back to legacy_kb_file ([LEGACY] EXP-CIT docs).
    name = stage_cfg.get("kb_file") or stage_cfg.get("legacy_kb_file") or ""
    return paths.kb_pipeline_root / name


def _kb_path_stored(paths: PipelinePaths, stage_cfg: dict[str, Any]) -> str:
    """Store KB path as repo-relative when possible; else absolute/external string."""
    kb = _kb_path(paths, stage_cfg)
    if kb.is_file() or kb.exists():
        rel = to_repo_relative(paths.repo_root, kb)
        # External KB (outside clone) stays absolute / unresolved string
        if rel == str(kb) or Path(rel).is_absolute():
            return str(kb)
        return rel
    # Prefer relative fragment under configured kb root when file missing
    name = stage_cfg.get("kb_file") or stage_cfg.get("legacy_kb_file") or ""
    try:
        root_rel = to_repo_relative(paths.repo_root, paths.kb_pipeline_root)
        if root_rel != str(paths.kb_pipeline_root) and not Path(root_rel).is_absolute():
            return f"{root_rel.rstrip('/')}/{name}" if name else root_rel
    except Exception:
        pass
    return str(kb) if name else ""


def _init_stage_records(pipeline: dict[str, Any], paths: PipelinePaths) -> dict[str, StageRecord]:
    records: dict[str, StageRecord] = {}
    for st in pipeline["stages"]:
        sid = st["id"]
        kb = _kb_path(paths, st)
        records[sid] = StageRecord(
            status=StageStatus.PENDING,
            kb_doc=st.get("kb_doc") or st.get("legacy_kb_doc") or "",
            kb_doc_path=_kb_path_stored(paths, st) if kb.is_file() else "",
        )
    return records


def find_run_for_bug(paths: PipelinePaths, bug_id: str) -> Path | None:
    """Find latest intermediate run dir for bug (state under projects/*/runs/)."""
    import json

    candidates: list[tuple[str, Path]] = []

    def _scan(root: Path) -> None:
        if not root.is_dir():
            return
        for d in root.iterdir():
            if not d.is_dir():
                continue
            wf = d / "workflow_state.json"
            if not wf.is_file():
                continue
            try:
                data = json.loads(wf.read_text(encoding="utf-8"))
                if str(data.get("bug_id")) == str(bug_id):
                    candidates.append((data.get("updated_at", ""), d))
            except Exception:
                continue

    projects = paths.projects_root()
    if projects.is_dir():
        for proj in projects.iterdir():
            if proj.is_dir():
                _scan(proj / "runs")
    # Legacy flat layout
    _scan(paths.test_bed_root / "00_runs")

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def resolve_product(paths: PipelinePaths, state: WorkflowState, formal_dir: Path, inter_dir: Path) -> str:
    """Resolve zentao product name for path placement under runs/<PRODUCT>/."""
    prod = (state.checkpoint or {}).get("product")
    if prod and str(prod) != UNASSIGNED_PROJECT:
        return str(prod)
    for d in (formal_dir, inter_dir):
        if not d:
            continue
        found = read_product_from_run(d)
        if found and found != UNASSIGNED_PROJECT:
            return found
        # Infer from parent dir when already under runs/<PRODUCT>/<run_id>
        try:
            parent = d.resolve().parent
            if parent.parent.resolve() == paths.runs_root.resolve() and parent.name not in (
                UNASSIGNED_PROJECT,
                state.run_id,
            ):
                return parent.name
        except Exception:
            pass
    return UNASSIGNED_PROJECT


def ensure_intermediate_under_product(
    paths: PipelinePaths,
    state: WorkflowState,
    inter_dir: Path,
    product: str,
) -> Path:
    """Move intermediate run into projects/<PRODUCT>/runs/<run_id>/ when product is known."""
    target = paths.intermediate_run_dir(state.run_id, product)
    if inter_dir.resolve() == target.resolve():
        return inter_dir
    if not inter_dir.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Already at destination or collision — keep existing target if same run
        return target if target.is_dir() else inter_dir
    inter_dir.rename(target)
    return target


def ensure_formal_under_product(
    paths: PipelinePaths,
    state: WorkflowState,
    formal_dir: Path,
    product: str,
) -> Path:
    """Move formal run into runs/<PRODUCT>/<run_id>/ when product is known.

    Also migrates legacy flat ``runs/<run_id>/`` into the product tree.
    """
    target = paths.formal_run_dir(state.run_id, product)
    if formal_dir.resolve() == target.resolve():
        return formal_dir
    if not formal_dir.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target if target.is_dir() else formal_dir
    formal_dir.rename(target)
    return target


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
                next_actions=list(b.get("next_actions") or []),
            )
        stage_records[sid] = StageRecord(
            status=StageStatus(rec.get("status", "pending")),
            kb_doc=rec.get("kb_doc", ""),
            kb_doc_path=rec.get("kb_doc_path", ""),
            kb_doc_read=bool(rec.get("kb_doc_read")),
            started_at=rec.get("started_at", ""),
            completed_at=rec.get("completed_at", ""),
            outputs=dict(rec.get("outputs") or {}),
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


def save_state(paths: PipelinePaths, state: WorkflowState, *, product: str | None = None) -> Path:
    import json

    state.updated_at = _now_iso()
    found = paths.find_formal_run_dir(state.run_id)
    prod = (
        product
        or (getattr(state, "product", None) if hasattr(state, "product") else None)
        or (state.checkpoint or {}).get("product")
        or (read_product_from_run(found) if found else None)
        or UNASSIGNED_PROJECT
    )
    formal = found or paths.formal_run_dir(state.run_id, prod)
    formal = ensure_formal_under_product(paths, state, formal, prod)
    inter = paths.find_intermediate_run_dir(state.run_id) or paths.intermediate_run_dir(
        state.run_id, prod
    )
    inter = ensure_intermediate_under_product(paths, state, inter, prod)
    formal.mkdir(parents=True, exist_ok=True)
    inter.mkdir(parents=True, exist_ok=True)

    data = state.to_dict()
    data["product"] = prod
    data["formal_run_dir"] = to_repo_relative(paths.repo_root, formal)
    data["intermediate_run_dir"] = to_repo_relative(paths.repo_root, inter)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Formal runs/<PRODUCT>/<run_id>/ is delivery source of truth.
    (formal / "workflow_state.json").write_text(payload, encoding="utf-8")
    (inter / "workflow_state.json").write_text(payload, encoding="utf-8")

    for run_dir in (formal, inter):
        checkpoint_md = run_dir / "CHECKPOINT.md"
        if state.workflow_status == "blocked" and state.checkpoint:
            cp = state.checkpoint
            lines = [
                f"# CIT Workflow Checkpoint — {state.run_id}",
                "",
                f"- **Bug ID**: {state.bug_id}",
                f"- **Product**: {prod}",
                f"- **Blocked at**: {cp.get('stage', state.current_stage)}",
                f"- **Step**: {cp.get('step', '')}",
                f"- **Reason**: {cp.get('reason', '')}",
                "",
                f"- **Formal (交付真源)**: `{to_repo_relative(paths.repo_root, formal)}`",
                f"- **Intermediate (测试中间)**: `{to_repo_relative(paths.repo_root, inter)}`",
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
            lines.extend(["", "## Resume", "", "```", f"/citfix {state.bug_id} --resume", "```", ""])
            checkpoint_md.write_text("\n".join(lines), encoding="utf-8")
        elif state.workflow_status == "completed":
            checkpoint_md.write_text(
                f"# CIT Workflow Completed — {state.run_id}\n\n"
                f"Product: {prod}\n\nAll stages finished.\n"
                f"Formal: `{to_repo_relative(paths.repo_root, formal)}`\n",
                encoding="utf-8",
            )
    return inter


def _make_run_id(bug_id: str, paths: PipelinePaths) -> str:
    date = datetime.now().strftime("%Y%m%d")
    base = f"CIT-{date}-{bug_id}"

    def _exists(rid: str) -> bool:
        if paths.find_formal_run_dir(rid) is not None:
            return True
        if paths.formal_run_dir(rid, UNASSIGNED_PROJECT).exists():
            return True
        if paths.find_intermediate_run_dir(rid) is not None:
            return True
        return False

    if not _exists(base):
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i:03d}"
        if not _exists(candidate):
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
    entry_mode: str = "citfix_direct",
    force_verify_auto: bool = False,
    device_serial: str | None = None,
    project_alias: str | None = None,
) -> WorkflowState:
    paths, pipeline = load_pipeline_config()
    entry_modes = pipeline.get("entry_modes") or {}
    mode_cfg = entry_modes.get(entry_mode) or entry_modes.get("citfix_direct") or {}
    skip_ids = set(mode_cfg.get("skip_stages") or [])
    if force_verify_auto or entry_mode in ("citfix_project", "citfix_batch_discover"):
        force_verify_auto = True

    inter_dir: Path | None = None
    state: WorkflowState | None = None

    if not force_new:
        if run_id:
            inter_dir = paths.find_intermediate_run_dir(run_id) or paths.intermediate_run_dir(
                run_id, UNASSIGNED_PROJECT
            )
        else:
            inter_dir = find_run_for_bug(paths, bug_id)
        if inter_dir and inter_dir.is_dir():
            state = load_state(inter_dir)
            if state and state.workflow_status == "completed" and not resume:
                state = None
                inter_dir = None

    if state is None:
        rid = run_id or _make_run_id(bug_id, paths)
        inter_dir = paths.intermediate_run_dir(rid, UNASSIGNED_PROJECT)
        state = WorkflowState(
            schema_version="1.0",
            pipeline_id=pipeline["pipeline_id"],
            bug_id=str(bug_id),
            run_id=rid,
            entry_mode=entry_mode,
            current_stage="",
            workflow_status="running",
            updated_at=_now_iso(),
            stages=_init_stage_records(pipeline, paths),
        )
    else:
        # Preserve project-batch flags across resume
        if entry_mode == "citfix_project":
            state.entry_mode = entry_mode
        if force_verify_auto:
            cp = dict(state.checkpoint or {})
            cp["force_verify_auto"] = True
            if device_serial:
                cp["device_serial"] = device_serial
            state.checkpoint = cp

    rid = state.run_id
    found_formal = paths.find_formal_run_dir(rid)
    formal_dir = found_formal or paths.formal_run_dir(rid, UNASSIGNED_PROJECT)
    product = resolve_product(paths, state, formal_dir, inter_dir or Path("."))
    formal_dir = ensure_formal_under_product(paths, state, formal_dir, product)
    inter_dir = paths.find_intermediate_run_dir(rid) or paths.intermediate_run_dir(rid, product)
    inter_dir = ensure_intermediate_under_product(paths, state, inter_dir, product)
    formal_dir.mkdir(parents=True, exist_ok=True)
    inter_dir.mkdir(parents=True, exist_ok=True)

    # device override: explicit arg > checkpoint
    device_override = device_serial or (state.checkpoint or {}).get("device_serial")
    force_auto = force_verify_auto or bool((state.checkpoint or {}).get("force_verify_auto"))
    alias = project_alias or (state.checkpoint or {}).get("project_alias")
    if force_auto or device_override or alias:
        cp = dict(state.checkpoint or {})
        if force_auto:
            cp["force_verify_auto"] = True
        if device_override:
            cp["device_serial"] = device_override
        if alias:
            cp["project_alias"] = str(alias)
        state.checkpoint = cp

    ctx = stages.RunContext(
        paths=paths,
        pipeline=pipeline,
        bug_id=str(bug_id),
        run_id=rid,
        run_dir=formal_dir,
        intermediate_dir=inter_dir,
        skip_stage_ids=skip_ids,
        entry_mode=state.entry_mode or entry_mode,
        force_verify_auto=force_auto,
        device_serial_override=str(device_override) if device_override else None,
        project_alias=str(alias) if alias else None,
    )

    def _relocate(product_name: str) -> str:
        nonlocal formal_dir
        formal_dir = ensure_formal_under_product(paths, state, formal_dir, product_name)
        ctx.run_dir = formal_dir
        ctx.intermediate_dir = ensure_intermediate_under_product(
            paths, state, ctx.intermediate_dir, product_name
        )
        return product_name

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
            product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
            save_state(paths, state, product=product)
            continue

        # If blocked from prior run and not resuming explicitly, still stop at checkpoint
        if rec.status == StageStatus.BLOCKED and not resume and state.workflow_status == "blocked":
            _set_checkpoint(state, sid, rec.blocker or Blocker("unknown", "blocked"))
            product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
            save_state(paths, state, product=product)
            return state

        # Retry blocked stage when resuming or continuing an in-progress run
        if rec.status == StageStatus.BLOCKED:
            rec.blocker = None

        rec.status = StageStatus.RUNNING
        rec.started_at = _now_iso()
        kb = _kb_path(paths, stage_cfg)
        if kb.is_file():
            rec.kb_doc_path = _kb_path_stored(paths, stage_cfg)
            rec.kb_doc_read = True
        state.current_stage = sid
        product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
        save_state(paths, state, product=product)

        log_path = ctx.stage_log_path(stage_cfg)
        rec.log_path = to_repo_relative(paths.repo_root, log_path)

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
            product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
            save_state(paths, state, product=product)
            return state

        if result.skipped:
            rec.status = StageStatus.SKIPPED
            rec.completed_at = _now_iso()
            product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
            save_state(paths, state, product=product)
            continue

        if result.blocker:
            rec.status = StageStatus.BLOCKED
            rec.blocker = result.blocker
            rec.outputs = result.outputs
            _set_checkpoint(state, sid, result.blocker)
            product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
            save_state(paths, state, product=product)
            return state

        rec.status = StageStatus.COMPLETED
        rec.completed_at = _now_iso()
        rec.outputs = result.outputs
        rec.blocker = None
        # After 04+, product is known — relocate formal + intermediate under <PRODUCT>/
        product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
        save_state(paths, state, product=product)

    state.workflow_status = "completed"
    state.current_stage = "14_closure"
    state.checkpoint = {"message": "All stages completed", "run_id": state.run_id}
    product = _relocate(resolve_product(paths, state, formal_dir, ctx.intermediate_dir))
    save_state(paths, state, product=product)
    return state
