"""Enumerate active workflow_state.json and batch_state.json files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class RunWatchView:
    bug_id: str
    run_id: str
    workflow_status: str
    current_stage: str
    updated_at: str
    updated_at_dt: datetime | None
    stage_status: str
    checkpoint_reason: str
    product: str
    intermediate_dir: Path
    formal_dir: Path | None
    state_path: Path
    claim_only: bool = False
    raw: dict[str, Any] | None = None


@dataclass
class BatchWatchView:
    batch_id: str
    batch_status: str
    project_alias: str
    product_name: str
    current_index: int
    updated_at: str
    updated_at_dt: datetime | None
    current_bug_id: str
    current_item_status: str
    batch_dir: Path
    state_path: Path


def parse_iso(ts: str) -> datetime | None:
    s = (ts or "").strip()
    if not s:
        return None
    # 2026-09-03T13:00:00+0800 or +08:00
    try:
        if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def view_from_state_file(
    state_path: Path,
    *,
    intermediate_dir: Path | None = None,
    formal_dir: Path | None = None,
) -> RunWatchView | None:
    data = _load_json(state_path)
    if not data:
        return None
    run_dir = state_path.parent
    inter = intermediate_dir or run_dir
    formal = formal_dir
    # Infer layout when caller did not pass formal/intermediate
    parts = [str(p) for p in state_path.resolve().parts]
    if formal is None and "runs_work" not in parts and len(parts) >= 4 and parts[-4] == "runs":
        formal = run_dir
    if intermediate_dir is None and "runs_work" in parts:
        inter = run_dir
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else {}
    cur = str(data.get("current_stage") or "")
    st_rec = stages.get(cur) if isinstance(stages.get(cur), dict) else {}
    stage_status = str(st_rec.get("status") or "")
    cp = data.get("checkpoint") if isinstance(data.get("checkpoint"), dict) else {}
    claim = (inter / "claim.json").is_file() or bool(
        (data.get("checkpoint") or {}).get("marker") == "CLAIMED_FOR_DEBUG"
    )
    run_json = _load_json(inter / "run.json") or _load_json(run_dir / "run.json") or {}
    if run_json.get("claim_only") or run_json.get("status") == "CLAIMED_FOR_DEBUG":
        claim = True

    updated = str(data.get("updated_at") or "")
    return RunWatchView(
        bug_id=str(data.get("bug_id") or ""),
        run_id=str(data.get("run_id") or run_dir.name),
        workflow_status=str(data.get("workflow_status") or ""),
        current_stage=cur,
        updated_at=updated,
        updated_at_dt=parse_iso(updated),
        stage_status=stage_status,
        checkpoint_reason=str(cp.get("reason") or ""),
        product=str(data.get("product") or ""),
        intermediate_dir=inter,
        formal_dir=formal,
        state_path=state_path,
        claim_only=claim,
        raw=data,
    )


def iter_active_runs(projects_root: Path) -> list[RunWatchView]:
    """Scan runs_work/projects/*/runs/*/workflow_state.json for non-terminal runs."""
    out: list[RunWatchView] = []
    if not projects_root.is_dir():
        return out
    for proj in projects_root.iterdir():
        runs = proj / "runs"
        if not runs.is_dir():
            continue
        for run_dir in runs.iterdir():
            wf = run_dir / "workflow_state.json"
            if not wf.is_file():
                continue
            view = view_from_state_file(wf, intermediate_dir=run_dir)
            if not view:
                continue
            if view.workflow_status in ("completed",):
                continue
            if view.claim_only:
                continue
            # skip batch discover pseudo-runs
            if str(view.run_id).startswith("CIT-BATCH-"):
                continue
            out.append(view)
    return out


def iter_active_runs_formal(runs_root: Path) -> list[RunWatchView]:
    """Scan formal runs/<PRODUCT>/<run_id>/workflow_state.json."""
    out: list[RunWatchView] = []
    if not runs_root.is_dir():
        return out
    for product_dir in runs_root.iterdir():
        if not product_dir.is_dir():
            continue
        for run_dir in product_dir.iterdir():
            if not run_dir.is_dir():
                continue
            if str(run_dir.name).startswith("CIT-BATCH-"):
                continue
            wf = run_dir / "workflow_state.json"
            if not wf.is_file():
                continue
            view = view_from_state_file(wf, formal_dir=run_dir, intermediate_dir=run_dir)
            if not view:
                continue
            if view.workflow_status in ("completed",):
                continue
            if view.claim_only:
                continue
            out.append(view)
    return out


def merge_run_views(*groups: list[RunWatchView]) -> list[RunWatchView]:
    """Dedupe by run_id; prefer entries that already have formal_dir set."""
    by_id: dict[str, RunWatchView] = {}
    for group in groups:
        for view in group:
            rid = view.run_id
            prev = by_id.get(rid)
            if prev is None:
                by_id[rid] = view
                continue
            # Prefer formal state path; keep intermediate_dir from runs_work when available
            if view.formal_dir is not None:
                if prev.intermediate_dir and "runs_work" in str(prev.intermediate_dir):
                    view.intermediate_dir = prev.intermediate_dir
                by_id[rid] = view
            elif prev.formal_dir is None:
                by_id[rid] = view
            else:
                if view.intermediate_dir and "runs_work" in str(view.intermediate_dir):
                    prev.intermediate_dir = view.intermediate_dir
    return list(by_id.values())


def iter_batches(projects_root: Path) -> list[BatchWatchView]:
    out: list[BatchWatchView] = []
    if not projects_root.is_dir():
        return out
    for proj in projects_root.iterdir():
        batches = proj / "batches"
        if not batches.is_dir():
            continue
        for bdir in batches.iterdir():
            sp = bdir / "batch_state.json"
            if not sp.is_file():
                continue
            data = _load_json(sp)
            if not data:
                continue
            status = str(data.get("batch_status") or "")
            if status in ("completed", "empty"):
                continue
            queue = list(data.get("queue") or [])
            idx = int(data.get("current_index") or 0)
            cur = queue[idx] if 0 <= idx < len(queue) and isinstance(queue[idx], dict) else {}
            updated = str(data.get("updated_at") or "")
            out.append(
                BatchWatchView(
                    batch_id=str(data.get("batch_id") or bdir.name),
                    batch_status=status,
                    project_alias=str(data.get("project_alias") or ""),
                    product_name=str(data.get("product_name") or proj.name),
                    current_index=idx,
                    updated_at=updated,
                    updated_at_dt=parse_iso(updated),
                    current_bug_id=str(cur.get("bug_id") or ""),
                    current_item_status=str(cur.get("status") or ""),
                    batch_dir=bdir,
                    state_path=sp,
                )
            )
    return out
