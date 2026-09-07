"""Serial project-batch orchestration for /citfix project <alias>."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from citfix.models import RunContext
from citfix.paths import (
    UNASSIGNED_PROJECT,
    PipelinePaths,
    load_pipeline_config,
    sanitize_product_dirname,
)
from citfix.project_match import CandidateBug, DiscoverResult, discover_project_cit_bugs
from citfix.run_log import emit_event
from citfix.stages.req_extract import _stage_01_req_parse, _stage_02_bug_task_extract
from citfix.stages.util_io import _write_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass
class BatchItem:
    bug_id: str
    title: str
    status: str = "pending"  # pending|running|completed|blocked|skipped
    run_id: str = ""
    reason: str = ""


@dataclass
class ProjectBatchState:
    schema_version: str = "1.0"
    batch_id: str = ""
    entry: str = "citfix_project"
    project_alias: str = ""
    product_name: str = ""
    product_id: str = ""
    zentao_user: str = ""
    device_serial: str | None = None
    force_verify_auto: bool = True
    serial: bool = True
    created_at: str = ""
    updated_at: str = ""
    queue: list[BatchItem] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    current_index: int = 0
    batch_status: str = "pending"  # pending|running|completed|blocked|empty
    formal_run_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "entry": self.entry,
            "project_alias": self.project_alias,
            "product_name": self.product_name,
            "product_id": self.product_id,
            "zentao_user": self.zentao_user,
            "device_serial": self.device_serial,
            "force_verify_auto": self.force_verify_auto,
            "serial": self.serial,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "queue": [asdict(q) for q in self.queue],
            "rejected": self.rejected,
            "current_index": self.current_index,
            "batch_status": self.batch_status,
            "formal_run_dir": self.formal_run_dir,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProjectBatchState":
        queue = [
            BatchItem(**{k: x.get(k) for k in ("bug_id", "title", "status", "run_id", "reason")})
            for x in (raw.get("queue") or [])
            if isinstance(x, dict) and x.get("bug_id")
        ]
        return cls(
            schema_version=str(raw.get("schema_version") or "1.0"),
            batch_id=str(raw.get("batch_id") or ""),
            entry=str(raw.get("entry") or "citfix_project"),
            project_alias=str(raw.get("project_alias") or ""),
            product_name=str(raw.get("product_name") or ""),
            product_id=str(raw.get("product_id") or ""),
            zentao_user=str(raw.get("zentao_user") or ""),
            device_serial=raw.get("device_serial"),
            force_verify_auto=bool(raw.get("force_verify_auto", True)),
            serial=bool(raw.get("serial", True)),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            queue=queue,
            rejected=list(raw.get("rejected") or []),
            current_index=int(raw.get("current_index") or 0),
            batch_status=str(raw.get("batch_status") or "pending"),
            formal_run_dir=str(raw.get("formal_run_dir") or ""),
        )


def batch_root(paths: PipelinePaths, product_name: str, batch_id: str) -> Path:
    """Intermediate batch dir: runs_work/projects/<PRODUCT>/batches/<batch_id>/."""
    return (
        paths.projects_root()
        / sanitize_product_dirname(product_name)
        / "batches"
        / batch_id
    )


def _candidate_row(c: CandidateBug) -> dict[str, Any]:
    return {
        "bug_id": c.bug_id,
        "title": c.title,
        "status": c.status,
        "severity": c.severity,
        "product_id": c.product_id,
        "product_name": c.product_name,
        "assigned_to": c.assigned_to,
        "cit_ok": c.cit_ok,
        "verify_mode": c.verify_mode,
        "source_tool": "zentao_my_bugs",
    }


def _formal_dir(state: ProjectBatchState | None = None, formal: Path | None = None) -> Path | None:
    if formal is not None:
        return Path(formal)
    if state and state.formal_run_dir:
        return Path(state.formal_run_dir)
    return None


def _emit_batch(
    formal: Path | None,
    *,
    event: str,
    summary: str,
    batch_id: str = "",
    bug_id: str = "",
    stage_id: str = "",
    level: str = "info",
    refs: dict[str, Any] | None = None,
) -> None:
    """Append to formal batch logs/run_events.jsonl (actor=cli for project orchestration)."""
    if formal is None:
        return
    emit_event(
        formal,
        actor="cli",
        event=event,
        summary=summary,
        run_id=batch_id or Path(formal).name,
        bug_id=str(bug_id),
        stage_id=stage_id,
        level=level,
        refs=refs or {},
    )


def _stage_map(pipeline: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(s["id"]): s for s in (pipeline.get("stages") or []) if s.get("id")}


def _move_tree(src: Path, dst: Path) -> Path:
    if src.resolve() == dst.resolve():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    if src.exists():
        shutil.move(str(src), str(dst))
    else:
        dst.mkdir(parents=True, exist_ok=True)
    return dst


def build_batch_from_discover(
    alias: str,
    discover: DiscoverResult,
    *,
    device_serial: str | None = None,
) -> ProjectBatchState:
    now = _now_iso()
    date = datetime.now().strftime("%Y%m%d")
    prod = discover.matched_product
    pname = prod.name if prod else "_unassigned"
    pid = prod.product_id if prod else ""
    batch_id = f"CIT-BATCH-{date}-{sanitize_product_dirname(alias).upper()}"
    rejected = [
        {**_candidate_row(c), "reject_reason": c.reject_reason}
        for c in discover.rejected
    ]
    queue = [
        BatchItem(bug_id=c.bug_id, title=c.title, status="pending")
        for c in discover.accepted
    ]
    status = "empty" if not queue else "pending"
    return ProjectBatchState(
        batch_id=batch_id,
        project_alias=alias,
        product_name=pname,
        product_id=pid,
        zentao_user=discover.zentao_user,
        device_serial=device_serial,
        force_verify_auto=True,
        serial=True,
        created_at=now,
        updated_at=now,
        queue=queue,
        rejected=rejected,
        current_index=0,
        batch_status=status,
    )


def build_batch_from_stage_outputs(
    alias: str,
    bug_ids_doc: dict[str, Any],
    *,
    device_serial: str | None = None,
    batch_id: str | None = None,
    formal_run_dir: str = "",
) -> ProjectBatchState:
    """Build batch state from 02_bug_task_extract output."""
    now = _now_iso()
    date = datetime.now().strftime("%Y%m%d")
    bid = batch_id or str(bug_ids_doc.get("batch_id") or "")
    if not bid:
        bid = f"CIT-BATCH-{date}-{sanitize_product_dirname(alias).upper()}"
    accepted = list(bug_ids_doc.get("accepted") or [])
    queue: list[BatchItem] = []
    if accepted:
        for row in accepted:
            if not isinstance(row, dict) or not row.get("bug_id"):
                continue
            queue.append(
                BatchItem(
                    bug_id=str(row["bug_id"]),
                    title=str(row.get("title") or ""),
                    status="pending",
                )
            )
    else:
        for bid_s in bug_ids_doc.get("bug_ids") or []:
            queue.append(BatchItem(bug_id=str(bid_s), title="", status="pending"))
    status = "empty" if not queue else "pending"
    return ProjectBatchState(
        batch_id=bid,
        project_alias=str(bug_ids_doc.get("project_alias") or alias),
        product_name=str(bug_ids_doc.get("product_name") or UNASSIGNED_PROJECT),
        product_id=str(bug_ids_doc.get("product_id") or ""),
        zentao_user=str(bug_ids_doc.get("zentao_user") or ""),
        device_serial=device_serial or bug_ids_doc.get("device_serial"),
        force_verify_auto=True,
        serial=True,
        created_at=now,
        updated_at=now,
        queue=queue,
        rejected=list(bug_ids_doc.get("rejected") or []),
        current_index=0,
        batch_status=status,
        formal_run_dir=formal_run_dir,
    )


def persist_batch_state(paths: PipelinePaths, state: ProjectBatchState) -> Path:
    """Write batch_state.json under intermediate batches/ (stage trees already mirrored)."""
    root = batch_root(paths, state.product_name, state.batch_id)
    root.mkdir(parents=True, exist_ok=True)
    state.updated_at = _now_iso()
    _write_json(root / "batch_state.json", state.to_dict())
    return root


def persist_batch_artifacts(
    paths: PipelinePaths,
    state: ProjectBatchState,
    discover: DiscoverResult | None = None,
) -> Path:
    """Compatibility writer: ensure 01/02 JSON exist under batches/ + batch_state."""
    root = batch_root(paths, state.product_name, state.batch_id)
    root.mkdir(parents=True, exist_ok=True)

    cand_path = root / "01_req_parse" / "output" / "candidate_rows.json"
    ids_path = root / "02_bug_task_extract" / "output" / "bug_ids.json"

    if not cand_path.is_file():
        candidates = []
        if discover:
            candidates = [_candidate_row(c) for c in discover.candidates]
        else:
            candidates = [
                {
                    "bug_id": q.bug_id,
                    "title": q.title,
                    "assigned_to": state.zentao_user,
                    "product_name": state.product_name,
                    "source_tool": "zentao_my_bugs",
                }
                for q in state.queue
            ]
        _write_json(
            cand_path,
            {
                "schema_version": "1.0",
                "marker": "CANDIDATE_ROWS_READY",
                "batch_id": state.batch_id,
                "project_alias": state.project_alias,
                "product_name": state.product_name,
                "product_id": state.product_id,
                "zentao_user": state.zentao_user,
                "source": "zentao_my_bugs",
                "rows": candidates,
                "device_serial": state.device_serial,
            },
        )
    if not ids_path.is_file():
        _write_json(
            ids_path,
            {
                "schema_version": "1.0",
                "marker": "BUG_IDS_READY",
                "batch_id": state.batch_id,
                "bug_ids": [q.bug_id for q in state.queue],
                "rejected": state.rejected,
                "gate": "cit_title_gate+assignedTo+product_alias+verify_mode_auto",
                "serial": True,
                "force_verify_auto": True,
                "device_serial": state.device_serial,
            },
        )
    return persist_batch_state(paths, state)


def load_batch_state(path: Path) -> ProjectBatchState:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ProjectBatchState.from_dict(raw)


def find_latest_batch(paths: PipelinePaths, alias: str) -> Path | None:
    """Find latest batch_state.json whose project_alias matches (case-insensitive)."""
    projects = paths.projects_root()
    if not projects.is_dir():
        return None
    alias_l = alias.strip().lower()
    candidates: list[tuple[str, Path]] = []
    for proj in projects.iterdir():
        batches = proj / "batches"
        if not batches.is_dir():
            continue
        for bdir in batches.iterdir():
            wf = bdir / "batch_state.json"
            if not wf.is_file():
                continue
            try:
                data = json.loads(wf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("project_alias") or "").strip().lower() != alias_l:
                pname = str(data.get("product_name") or "").lower()
                if alias_l not in pname and alias_l not in str(data.get("batch_id") or "").lower():
                    continue
            candidates.append((str(data.get("updated_at") or ""), wf))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def run_batch_intake(
    alias: str,
    *,
    device_serial: str | None = None,
    batch_id: str | None = None,
    discover_fn: Callable[..., DiscoverResult] | None = None,
) -> tuple[ProjectBatchState, Path, Path]:
    """Run 01/02 auto handlers → formal runs/<PRODUCT>/<batch_id>/ + batches/<batch_id>/.

    Returns (state, batch_intermediate_dir, formal_dir).
    """
    paths, pipeline = load_pipeline_config()
    stages = _stage_map(pipeline)
    if "01_req_parse" not in stages or "02_bug_task_extract" not in stages:
        raise RuntimeError("pipeline missing 01_req_parse / 02_bug_task_extract")

    date = datetime.now().strftime("%Y%m%d")
    bid = batch_id or f"CIT-BATCH-{date}-{sanitize_product_dirname(alias).upper()}"

    formal = paths.formal_run_dir(bid, UNASSIGNED_PROJECT)
    inter = batch_root(paths, UNASSIGNED_PROJECT, bid)
    formal.mkdir(parents=True, exist_ok=True)
    inter.mkdir(parents=True, exist_ok=True)

    entry_modes = pipeline.get("entry_modes") or {}
    mode_cfg = entry_modes.get("citfix_batch_discover") or {}
    skip_ids = set(mode_cfg.get("skip_stages") or [])

    ctx = RunContext(
        paths=paths,
        pipeline=pipeline,
        bug_id=bid,
        run_id=bid,
        run_dir=formal,
        intermediate_dir=inter,
        skip_stage_ids=skip_ids,
        entry_mode="citfix_batch_discover",
        force_verify_auto=True,
        device_serial_override=device_serial,
        project_alias=alias,
    )

    _emit_batch(
        formal,
        event="batch_start",
        summary=f"project intake start alias={alias} batch={bid}",
        batch_id=bid,
        refs={"project_alias": alias, "device_serial": device_serial or ""},
    )

    # Optional: inject discover_fn into stage 01 via monkeypatch of module symbol
    if discover_fn is not None:
        import citfix.stages.req_extract as req_mod

        orig = req_mod.discover_project_cit_bugs
        req_mod.discover_project_cit_bugs = discover_fn  # type: ignore[assignment]
        try:
            r1 = _stage_01_req_parse(ctx, stages["01_req_parse"], ctx.stage_log_path(stages["01_req_parse"]))
        finally:
            req_mod.discover_project_cit_bugs = orig  # type: ignore[assignment]
    else:
        r1 = _stage_01_req_parse(ctx, stages["01_req_parse"], ctx.stage_log_path(stages["01_req_parse"]))

    if r1.blocker:
        _emit_batch(
            formal,
            event="batch_blocked",
            summary=f"01_req_parse blocked: {r1.blocker.reason}",
            batch_id=bid,
            stage_id="01_req_parse",
            level="error",
            refs={"step": r1.blocker.step},
        )
        raise RuntimeError(r1.blocker.reason)

    cand_path = formal / "01_req_parse" / "output" / "candidate_rows.json"
    cand = json.loads(cand_path.read_text(encoding="utf-8"))
    product = str(cand.get("product_name") or UNASSIGNED_PROJECT)
    rows = cand.get("rows") if isinstance(cand.get("rows"), list) else []
    _emit_batch(
        formal,
        event="batch_01_done",
        summary=f"01 candidates={len(rows)} product={product}",
        batch_id=bid,
        stage_id="01_req_parse",
        refs={"candidate_count": len(rows), "product_name": product},
    )

    # Relocate under real product once known
    formal_new = paths.formal_run_dir(bid, product)
    inter_new = batch_root(paths, product, bid)
    formal = _move_tree(formal, formal_new)
    inter = _move_tree(inter, inter_new)
    ctx.run_dir = formal
    ctx.intermediate_dir = inter

    r2 = _stage_02_bug_task_extract(
        ctx, stages["02_bug_task_extract"], ctx.stage_log_path(stages["02_bug_task_extract"])
    )
    if r2.blocker:
        _emit_batch(
            formal,
            event="batch_blocked",
            summary=f"02_bug_task_extract blocked: {r2.blocker.reason}",
            batch_id=bid,
            stage_id="02_bug_task_extract",
            level="error",
            refs={"step": r2.blocker.step},
        )
        raise RuntimeError(r2.blocker.reason)

    ids_path = formal / "02_bug_task_extract" / "output" / "bug_ids.json"
    ids_doc = json.loads(ids_path.read_text(encoding="utf-8"))
    state = build_batch_from_stage_outputs(
        alias,
        ids_doc,
        device_serial=device_serial,
        batch_id=bid,
        formal_run_dir=str(formal),
    )
    batch_dir = persist_batch_state(paths, state)
    _emit_batch(
        formal,
        event="batch_ready",
        summary=(
            f"batch ready queue={len(state.queue)} rejected={len(state.rejected)} "
            f"status={state.batch_status}"
        ),
        batch_id=bid,
        stage_id="02_bug_task_extract",
        refs={
            "bug_ids": [q.bug_id for q in state.queue],
            "rejected_count": len(state.rejected),
            "batch_status": state.batch_status,
            "product_name": state.product_name,
        },
    )
    # Ensure intermediate mirror of 02 exists (handler already mirrors; re-assert path)
    return state, batch_dir, formal


def run_project_batch(
    alias: str,
    *,
    device_serial: str | None = None,
    status_only: bool = False,
    dry_run: bool = False,
    resume: bool = False,
    force_new: bool = False,
    run_pipeline_fn: Callable[..., Any] | None = None,
    discover_fn: Callable[..., DiscoverResult] | None = None,
) -> tuple[ProjectBatchState, Path]:
    """Discover via 01/02 stage handlers (unless resume) and serially run each accepted bug.

    Returns (batch_state, batch_dir) where batch_dir is intermediate batches/<id>/.
    """
    from citfix.engine import run_pipeline

    paths, _ = load_pipeline_config()
    pipeline_fn = run_pipeline_fn or run_pipeline

    state: ProjectBatchState | None = None
    batch_path: Path | None = None

    if resume and not force_new:
        batch_path = find_latest_batch(paths, alias)
        if batch_path:
            state = load_batch_state(batch_path)
            if device_serial:
                state.device_serial = device_serial

    if state is None or force_new:
        state, batch_dir, _formal = run_batch_intake(
            alias, device_serial=device_serial, discover_fn=discover_fn
        )
    else:
        batch_dir = batch_path.parent if batch_path else persist_batch_state(paths, state)
        _emit_batch(
            _formal_dir(state),
            event="batch_resume",
            summary=f"resume batch {state.batch_id} status={state.batch_status}",
            batch_id=state.batch_id,
            refs={"current_index": state.current_index, "queue_len": len(state.queue)},
        )

    formal = _formal_dir(state)

    if status_only or dry_run:
        state.batch_status = state.batch_status if state.queue else "empty"
        persist_batch_state(paths, state)
        _emit_batch(
            formal,
            event="batch_dry_run" if dry_run else "batch_status",
            summary=f"status_only/dry_run queue={len(state.queue)} status={state.batch_status}",
            batch_id=state.batch_id,
            refs={"batch_status": state.batch_status, "bug_ids": [q.bug_id for q in state.queue]},
        )
        return state, batch_dir

    if not state.queue:
        state.batch_status = "empty"
        persist_batch_state(paths, state)
        _emit_batch(
            formal,
            event="batch_empty",
            summary="no auto-eligible bugs in queue",
            batch_id=state.batch_id,
            level="warn",
            refs={"rejected_count": len(state.rejected)},
        )
        return state, batch_dir

    state.batch_status = "running"
    persist_batch_state(paths, state)
    _emit_batch(
        formal,
        event="batch_running",
        summary=f"serial run start queue={len(state.queue)}",
        batch_id=state.batch_id,
        refs={"bug_ids": [q.bug_id for q in state.queue]},
    )

    for idx, item in enumerate(state.queue):
        if item.status in ("completed", "skipped"):
            continue
        state.current_index = idx
        item.status = "running"
        state.updated_at = _now_iso()
        persist_batch_state(paths, state)
        _emit_batch(
            formal,
            event="batch_item_start",
            summary=f"start bug {item.bug_id} ({idx + 1}/{len(state.queue)})",
            batch_id=state.batch_id,
            bug_id=item.bug_id,
            refs={"index": idx, "title": item.title[:80]},
        )

        wf_state = pipeline_fn(
            item.bug_id,
            resume=True,
            force_new=False,
            entry_mode="citfix_project",
            force_verify_auto=True,
            device_serial=state.device_serial,
            project_alias=state.project_alias,
        )
        item.run_id = getattr(wf_state, "run_id", "") or ""
        st = getattr(wf_state, "workflow_status", "") or ""
        if st == "completed":
            item.status = "completed"
            _emit_batch(
                formal,
                event="batch_item_done",
                summary=f"bug {item.bug_id} completed run={item.run_id}",
                batch_id=state.batch_id,
                bug_id=item.bug_id,
                refs={"run_id": item.run_id, "workflow_status": st},
            )
        elif st == "blocked":
            item.status = "blocked"
            item.reason = str(
                (getattr(wf_state, "checkpoint", None) or {}).get("reason") or "blocked"
            )
            state.batch_status = "blocked"
            persist_batch_state(paths, state)
            _emit_batch(
                formal,
                event="batch_blocked",
                summary=f"bug {item.bug_id} blocked: {item.reason}",
                batch_id=state.batch_id,
                bug_id=item.bug_id,
                level="warn",
                refs={"run_id": item.run_id, "reason": item.reason},
            )
            return state, batch_dir
        else:
            item.status = "blocked"
            item.reason = f"ended with status={st}"
            state.batch_status = "blocked"
            persist_batch_state(paths, state)
            _emit_batch(
                formal,
                event="batch_blocked",
                summary=f"bug {item.bug_id} ended status={st}",
                batch_id=state.batch_id,
                bug_id=item.bug_id,
                level="warn",
                refs={"run_id": item.run_id, "workflow_status": st},
            )
            return state, batch_dir

        persist_batch_state(paths, state)

    state.batch_status = "completed"
    state.updated_at = _now_iso()
    persist_batch_state(paths, state)
    _emit_batch(
        formal,
        event="batch_completed",
        summary=f"project batch completed ({len(state.queue)} bugs)",
        batch_id=state.batch_id,
        refs={"bug_ids": [q.bug_id for q in state.queue]},
    )
    return state, batch_dir
