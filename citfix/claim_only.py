"""Claim-only stub: serially intervene on queued bugs without running stages 03+."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from citfix.paths import PipelinePaths, to_repo_relative
from citfix.stages.util_io import _now_iso, _write_json


@dataclass
class ClaimResult:
    """Mimic WorkflowState fields used by project_batch serial loop."""

    run_id: str
    bug_id: str
    workflow_status: str = "completed"
    checkpoint: dict[str, Any] | None = None


def _make_run_id(bug_id: str) -> str:
    date = datetime.now().strftime("%Y%m%d")
    return f"CIT-{date}-{bug_id}"


def claim_bug_for_debug(
    bug_id: str,
    *,
    paths: PipelinePaths,
    product_name: str,
    project_alias: str = "",
    device_serial: str | None = None,
    batch_id: str = "",
    **_kwargs: Any,
) -> ClaimResult:
    """Create formal + intermediate run stubs proving the batch can intervene.

    Does **not** execute 03–14. Writes ``claim.json`` with marker ``CLAIMED_FOR_DEBUG``.
    """
    bid = str(bug_id)
    rid = _make_run_id(bid)
    formal = paths.formal_run_dir(rid, product_name)
    inter = paths.intermediate_run_dir(rid, product_name)
    formal.mkdir(parents=True, exist_ok=True)
    inter.mkdir(parents=True, exist_ok=True)

    claim = {
        "schema_version": "1.0",
        "marker": "CLAIMED_FOR_DEBUG",
        "bug_id": bid,
        "run_id": rid,
        "batch_id": batch_id,
        "project_alias": project_alias,
        "product_name": product_name,
        "entry_mode": "citfix_project",
        "claim_only": True,
        "device_serial": device_serial,
        "claimed_at": _now_iso(),
        "note": "Serial batch intervened; stages 03+ not started (claim-only / debug).",
        "formal_run_dir": to_repo_relative(paths.repo_root, formal),
        "intermediate_run_dir": to_repo_relative(paths.repo_root, inter),
    }
    run_json = {
        "schema_version": "1.0",
        "run_id": rid,
        "bug_id": bid,
        "entry_mode": "citfix_project",
        "project_alias": project_alias,
        "trigger": f"/citfix {bid} (claim-only from project batch)",
        "status": "CLAIMED_FOR_DEBUG",
        "claim_only": True,
        "started_at": _now_iso(),
        "formal_run_dir": claim["formal_run_dir"],
        "intermediate_run_dir": claim["intermediate_run_dir"],
    }
    _write_json(formal / "claim.json", claim)
    _write_json(formal / "run.json", run_json)
    _write_json(inter / "claim.json", claim)
    _write_json(inter / "run.json", run_json)
    _write_json(
        inter / "workflow_state.json",
        {
            "schema_version": "1.0",
            "bug_id": bid,
            "run_id": rid,
            "entry_mode": "citfix_project",
            "workflow_status": "completed",
            "current_stage": "claim_only",
            "updated_at": _now_iso(),
            "checkpoint": {
                "message": "claim-only: intervened, no 03+",
                "marker": "CLAIMED_FOR_DEBUG",
            },
            "stages": {},
        },
    )
    return ClaimResult(
        run_id=rid,
        bug_id=bid,
        workflow_status="completed",
        checkpoint={"reason": "claim_only", "marker": "CLAIMED_FOR_DEBUG"},
    )


def make_claim_pipeline_fn(
    paths: PipelinePaths,
    *,
    product_name: str,
    project_alias: str = "",
    batch_id: str = "",
) -> Any:
    """Adapter matching ``run_pipeline(...)`` kwargs used by ``run_project_batch``."""

    alias_default = project_alias
    product = product_name
    bid_default = batch_id

    def _fn(
        bug_id: str,
        *,
        device_serial: str | None = None,
        project_alias: str | None = None,
        **kwargs: Any,
    ) -> ClaimResult:
        return claim_bug_for_debug(
            bug_id,
            paths=paths,
            product_name=product,
            project_alias=str(project_alias or alias_default or ""),
            device_serial=device_serial,
            batch_id=bid_default,
        )

    return _fn
