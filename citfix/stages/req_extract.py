"""Auto stages 01_req_parse / 02_bug_task_extract — task intake from ZenTao lists."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from citfix.models import Blocker, RunContext, StageResult
from citfix.paths import to_repo_relative
from citfix.project_match import CandidateBug, discover_project_cit_bugs
from citfix.stages.util_io import _log, _mirror_tree, _now_iso, _write_json


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


def _resolve_project_alias(ctx: RunContext, stage_root: Path) -> str:
    qpath = stage_root / "input" / "query.json"
    if qpath.is_file():
        try:
            raw = json.loads(qpath.read_text(encoding="utf-8"))
            alias = str(raw.get("project_alias") or raw.get("alias") or "").strip()
            if alias:
                return alias
        except Exception:
            pass
    alias = str(getattr(ctx, "project_alias", None) or "").strip()
    return alias


def _stage_01_req_parse(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    """Pull ZenTao candidate rows (my_bugs) for a project alias → candidate_rows.json."""
    stage_root = ctx.stage_root(stage_cfg)
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / "input").mkdir(parents=True, exist_ok=True)
    (stage_root / "output").mkdir(parents=True, exist_ok=True)
    (stage_root / "intermediate").mkdir(parents=True, exist_ok=True)

    alias = _resolve_project_alias(ctx, stage_root)
    if not alias:
        # Direct single-bug path should skip 01; if invoked, emit one stub row from bug_id
        if str(ctx.bug_id).isdigit():
            rows = [
                {
                    "bug_id": str(ctx.bug_id),
                    "title": "",
                    "status": "",
                    "severity": "",
                    "product_id": "",
                    "product_name": "",
                    "assigned_to": "",
                    "cit_ok": True,
                    "verify_mode": "auto",
                    "source_tool": "citfix_direct_stub",
                }
            ]
            out = {
                "schema_version": "1.0",
                "marker": "CANDIDATE_ROWS_READY",
                "run_id": ctx.run_id,
                "project_alias": "",
                "product_name": "",
                "product_id": "",
                "zentao_user": "",
                "source": "citfix_direct_stub",
                "rows": rows,
                "device_serial": getattr(ctx, "device_serial_override", None),
                "parsed_at": _now_iso(),
            }
            out_path = stage_root / "output" / "candidate_rows.json"
            _write_json(out_path, out)
            _write_json(stage_root / "intermediate" / "discover_meta.json", {"mode": "stub"})
            _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
            _log(log_path, f"01 stub candidate for bug {ctx.bug_id}")
            return StageResult(
                outputs={
                    "candidate_rows.json": to_repo_relative(ctx.paths.repo_root, out_path),
                }
            )
        return StageResult(
            blocker=Blocker(
                step="query",
                reason="01_req_parse requires project_alias (query.json or ctx.project_alias)",
                next_actions=[
                    "Write 01_req_parse/input/query.json with {project_alias: ...}",
                    "Or run /citfix project <alias>",
                ],
            )
        )

    # Persist query for audit
    _write_json(
        stage_root / "input" / "query.json",
        {
            "project_alias": alias,
            "source": "zentao_my_bugs",
            "device_serial": getattr(ctx, "device_serial_override", None),
            "force_verify_auto": bool(getattr(ctx, "force_verify_auto", True)),
        },
    )

    try:
        discover = discover_project_cit_bugs(alias, repo_root=ctx.paths.repo_root)
    except Exception as e:  # noqa: BLE001
        return StageResult(
            blocker=Blocker(
                step="zentao_my_bugs",
                reason=f"discover failed: {e}",
                next_actions=[
                    "Check ~/.bugfix-flow/zentao.yaml and ZenTao MCP",
                    f"/citfix project {alias} --dry-run",
                ],
            )
        )

    if discover.errors:
        return StageResult(
            blocker=Blocker(
                step="product_match",
                reason="; ".join(discover.errors),
                completed_context={"alias": alias, "errors": discover.errors},
                next_actions=[
                    "Use a sharper product alias (e.g. slb783)",
                    "Ensure plan_bank/<PRODUCT>/ exists or ZenTao list_products works",
                ],
            )
        )

    prod = discover.matched_product
    rows = [_candidate_row(c) for c in discover.candidates]
    out = {
        "schema_version": "1.0",
        "marker": "CANDIDATE_ROWS_READY",
        "run_id": ctx.run_id,
        "batch_id": ctx.run_id,
        "project_alias": alias,
        "product_name": prod.name if prod else "",
        "product_id": prod.product_id if prod else "",
        "zentao_user": discover.zentao_user,
        "source": "zentao_my_bugs",
        "rows": rows,
        "device_serial": getattr(ctx, "device_serial_override", None),
        "parsed_at": _now_iso(),
    }
    out_path = stage_root / "output" / "candidate_rows.json"
    _write_json(out_path, out)
    _write_json(
        stage_root / "intermediate" / "discover_raw.json",
        {
            "accepted_count": len(discover.accepted),
            "rejected_count": len(discover.rejected),
            "candidate_count": len(discover.candidates),
            "errors": discover.errors,
        },
    )
    # Stash full discover for 02 (same run)
    _write_json(
        stage_root / "intermediate" / "discover_result.json",
        {
            "accepted": [_candidate_row(c) for c in discover.accepted],
            "rejected": [
                {**_candidate_row(c), "reject_reason": c.reject_reason} for c in discover.rejected
            ],
        },
    )
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
    _log(log_path, f"01 candidates={len(rows)} alias={alias} product={out['product_name']}")
    return StageResult(
        outputs={"candidate_rows.json": to_repo_relative(ctx.paths.repo_root, out_path)}
    )


def _stage_02_bug_task_extract(
    ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path
) -> StageResult:
    """Filter candidates → bug_ids.json (CIT + assignedTo + verify_mode=auto)."""
    stage_root = ctx.stage_root(stage_cfg)
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / "output").mkdir(parents=True, exist_ok=True)
    (stage_root / "intermediate").mkdir(parents=True, exist_ok=True)
    (stage_root / "input").mkdir(parents=True, exist_ok=True)

    cand_path = (
        ctx.run_dir / "01_req_parse" / "output" / "candidate_rows.json"
    )
    if not cand_path.is_file():
        return StageResult(
            blocker=Blocker(
                step="input",
                reason=f"Missing {cand_path} — run 01_req_parse first",
                next_actions=["Complete 01_req_parse", f"/citfix {ctx.bug_id} --resume"],
            )
        )

    cand_doc = json.loads(cand_path.read_text(encoding="utf-8"))
    discover_path = ctx.run_dir / "01_req_parse" / "intermediate" / "discover_result.json"

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if discover_path.is_file():
        disc = json.loads(discover_path.read_text(encoding="utf-8"))
        accepted = list(disc.get("accepted") or [])
        rejected = list(disc.get("rejected") or [])
    else:
        # Re-filter rows if discover stash missing (e.g. stub or hand-edited candidates)
        alias = str(cand_doc.get("project_alias") or getattr(ctx, "project_alias", "") or "")
        if alias:
            discover = discover_project_cit_bugs(alias, repo_root=ctx.paths.repo_root)
            accepted = [_candidate_row(c) for c in discover.accepted]
            rejected = [
                {**_candidate_row(c), "reject_reason": c.reject_reason} for c in discover.rejected
            ]
        else:
            for row in cand_doc.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                if row.get("source_tool") == "citfix_direct_stub" and row.get("bug_id"):
                    accepted.append(row)
                elif row.get("cit_ok") and str(row.get("verify_mode") or "auto").lower() == "auto":
                    accepted.append(row)
                else:
                    rejected.append({**row, "reject_reason": "filter_without_discover"})

    force_auto = bool(getattr(ctx, "force_verify_auto", False)) or bool(
        cand_doc.get("force_verify_auto")
    )
    if force_auto:
        kept: list[dict[str, Any]] = []
        for row in accepted:
            mode = str(row.get("verify_mode") or "auto").lower()
            if mode != "auto":
                rejected.append({**row, "reject_reason": f"verify_mode={mode} (force_auto)"})
            else:
                kept.append(row)
        accepted = kept

    bug_ids = [str(r.get("bug_id")) for r in accepted if r.get("bug_id")]
    out = {
        "schema_version": "1.0",
        "marker": "BUG_IDS_READY",
        "run_id": ctx.run_id,
        "batch_id": str(cand_doc.get("batch_id") or ctx.run_id),
        "project_alias": cand_doc.get("project_alias"),
        "product_name": cand_doc.get("product_name"),
        "product_id": cand_doc.get("product_id"),
        "zentao_user": cand_doc.get("zentao_user"),
        "bug_ids": bug_ids,
        "accepted": accepted,
        "rejected": rejected,
        "gate": "cit_title_gate+assignedTo+product_alias+verify_mode_auto",
        "serial": True,
        "force_verify_auto": True,
        "device_serial": cand_doc.get("device_serial")
        or getattr(ctx, "device_serial_override", None),
        "extracted_at": _now_iso(),
    }
    out_path = stage_root / "output" / "bug_ids.json"
    _write_json(out_path, out)
    # Compatibility with 03_zentao_fetch input shape
    _write_json(stage_root / "output" / "bug_task_ids.json", {"bug_ids": bug_ids})
    _write_json(stage_root / "input" / "candidate_rows.json", cand_doc)
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
    _log(log_path, f"02 bug_ids={len(bug_ids)} rejected={len(rejected)}")
    return StageResult(
        outputs={
            "bug_ids.json": to_repo_relative(ctx.paths.repo_root, out_path),
            "bug_task_ids.json": to_repo_relative(
                ctx.paths.repo_root, stage_root / "output" / "bug_task_ids.json"
            ),
        }
    )
