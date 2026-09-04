"""Auto stages 00 / 03–06b."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from citfix.human_cases import VERIFY_MODES, apply_runtime_verify_policy, resolve_verification
from citfix.models import Blocker, RunContext, StageResult
from citfix.paths import to_repo_relative
from citfix.stages.prepare_common import (
    _cit_title_ok,
    _compute_gates,
    _load_project_info,
    _localize_attachments,
)
from citfix.stages.util_io import (
    _ensure_repo_on_path,
    _log,
    _mirror_tree,
    _now_iso,
    _write_json,
)

def _stage_00_run_registry(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    ctx.intermediate_dir.mkdir(parents=True, exist_ok=True)
    entry = str(getattr(ctx, "entry_mode", None) or "citfix_direct")
    alias = str(getattr(ctx, "project_alias", None) or "").strip()
    if entry == "citfix_batch_discover" and alias:
        trigger = f"/citfix project {alias}"
    elif entry == "citfix_project":
        trigger = f"/citfix {ctx.bug_id} (project child)"
    else:
        trigger = f"/citfix {ctx.bug_id}"
    try:
        bug_ref: Any = int(ctx.bug_id)
    except (TypeError, ValueError):
        bug_ref = str(ctx.bug_id)
    run_json = {
        "schema_version": "1.0",
        "run_id": ctx.run_id,
        "bug_id": ctx.bug_id,
        "entry_mode": entry,
        "project_alias": alias or None,
        "trigger": trigger,
        "started_at": _now_iso(),
        "status": "RUNNING",
        "formal_run_dir": to_repo_relative(ctx.paths.repo_root, ctx.run_dir),
        "intermediate_run_dir": to_repo_relative(ctx.paths.repo_root, ctx.intermediate_dir),
    }
    _write_json(ctx.run_dir / "run.json", run_json)
    _write_json(ctx.intermediate_dir / "run.json", run_json)
    index = {
        "run_id": ctx.run_id,
        "bug_id": bug_ref,
        "entry_mode": entry,
        "project_alias": alias or None,
        "trigger": trigger,
        "pipeline": "citfix",
        "started_at": _now_iso(),
        "status": "RUNNING",
        "formal_run_dir": to_repo_relative(ctx.paths.repo_root, ctx.run_dir),
        "intermediate_run_dir": to_repo_relative(ctx.paths.repo_root, ctx.intermediate_dir),
    }
    _write_json(ctx.run_dir / "run_index.json", index)
    _write_json(ctx.intermediate_dir / "run_index.json", index)
    return StageResult(
        outputs={
            "workflow_state": to_repo_relative(
                ctx.paths.repo_root, ctx.intermediate_dir / "workflow_state.json"
            ),
            "run_json": to_repo_relative(ctx.paths.repo_root, ctx.run_dir / "run.json"),
            "intermediate_run_json": to_repo_relative(
                ctx.paths.repo_root, ctx.intermediate_dir / "run.json"
            ),
        }
    )


def _stage_03_zentao_fetch(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    _ensure_repo_on_path(ctx.paths.repo_root)
    from bugflow.core import zentao
    from bugflow.core.config import get_config_status

    cfg = get_config_status()
    zentao_ready = (cfg.get("zentao") or {}).get("ready")
    if not zentao_ready:
        return StageResult(
            blocker=Blocker(
                step="zentao_config",
                reason="Zentao not configured (zentao.yaml missing credentials)",
                completed_context={"config_status": cfg.get("zentao")},
                next_actions=[
                    "Run cit-setup or fill ~/.bugfix-flow/zentao.yaml",
                    f"/citfix {ctx.bug_id} --resume",
                ],
            )
        )

    bug_id = int(ctx.bug_id)
    try:
        bug = zentao.get_bug(bug_id)
        raw_text = zentao.get_bug_formatted(bug_id)
    except Exception as e:
        return StageResult(
            blocker=Blocker(
                step="zentao_get_bug",
                reason=str(e),
                next_actions=[f"/citfix {ctx.bug_id} --resume"],
            )
        )

    title = str(bug.get("title") or "")
    steps = str(bug.get("steps") or bug.get("description") or "")
    product = str(bug.get("product_name") or bug.get("product") or "")

    structured = {
        "bug_id": str(bug_id),
        "title": title,
        "status": str(bug.get("status") or ""),
        "severity": str(bug.get("severity") or ""),
        "priority": str(bug.get("priority") or ""),
        "assignedTo": str(bug.get("assignedTo") or bug.get("assigned_to") or ""),
        "module": str(bug.get("module") or ""),
        "type": str(bug.get("type") or ""),
        "product": product,
        "openedBuild": str(bug.get("openedBuild") or ""),
        "platform": str(bug.get("platform") or "android"),
        "openedBy": str(bug.get("openedBy") or ""),
        "openedDate": str(bug.get("openedDate") or ""),
        "resolution": bug.get("resolution"),
        "steps": steps,
        "attachments": bug.get("attachments") or [],
        "comments_count": len(bug.get("actions") or []),
        "source_tool": "zentao_get_bug",
        "cit_title_ok": _cit_title_ok(title, steps),
        "source_call": {
            "tool": "zentao_get_bug",
            "arguments": {"bug_id": bug_id},
            "invoked_at": _now_iso(),
            "status": "OK",
        },
    }

    stage_root = ctx.stage_root(stage_cfg)
    inter = stage_root / "intermediate"
    out = stage_root / "output"
    _write_json(inter / f"zentao_get_bug_{bug_id}_structured.json", structured)
    _write_json(inter / f"zentao_get_bug_{bug_id}_call.json", structured["source_call"])
    (inter / f"zentao_get_bug_{bug_id}_raw.txt").write_text(raw_text, encoding="utf-8")
    _write_json(
        out / "zentao_fetch_results.json",
        {"run_id": ctx.run_id, "bug_id": str(bug_id), "results": [structured]},
    )
    _write_json(stage_root / "input" / "bug_task_ids.json", {"bug_ids": [str(bug_id)]})

    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
    ctx._zentao_structured = structured  # type: ignore[attr-defined]
    return StageResult(
        outputs={
            "structured": str(inter / f"zentao_get_bug_{bug_id}_structured.json"),
            "fetch_results": str(out / "zentao_fetch_results.json"),
        }
    )


def _stage_04_result_normalize(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    stage_root = ctx.stage_root(stage_cfg)
    structured_path = (
        ctx.run_dir
        / "03_zentao_fetch"
        / "intermediate"
        / f"zentao_get_bug_{ctx.bug_id}_structured.json"
    )
    if not structured_path.is_file():
        return StageResult(
            blocker=Blocker(
                step="04_input",
                reason=f"Missing {structured_path}",
                next_actions=[f"/citfix {ctx.bug_id} --resume"],
            )
        )
    structured = json.loads(structured_path.read_text(encoding="utf-8"))
    title = structured.get("title", "")
    steps = structured.get("steps", "")
    cit_ok = _cit_title_ok(title, steps)
    errors: list[str] = []
    if not cit_ok:
        errors.append("CIT title gate failed: title/steps must contain CIT keyword")

    product = str(structured.get("product") or "")
    verification = resolve_verification(
        ctx.paths.repo_root,
        product or "DEFAULT",
        str(title or ""),
        str(steps or ""),
    )
    pipe_gates = ctx.pipeline.get("gates") if isinstance(ctx.pipeline.get("gates"), dict) else {}
    force_runtime_auto = bool(pipe_gates.get("verify_runtime_force_auto", True)) or bool(
        getattr(ctx, "force_verify_auto", False)
    )
    if getattr(ctx, "force_verify_auto", False):
        verification = {
            **verification,
            "force_verify_auto": True,
            "classification_source": verification.get("classification_source")
            or "citfix_project_force_auto",
            "classification_confidence": "high",
        }
    verification = apply_runtime_verify_policy(verification, force_auto=force_runtime_auto)

    task = {
        "task_id": f"{ctx.run_id}-BUG-{ctx.bug_id}",
        "bug_id": str(ctx.bug_id),
        "kind": "bug",
        "title": title,
        "status": structured.get("status"),
        "severity": structured.get("severity"),
        "priority": structured.get("priority"),
        "product": structured.get("product"),
        "module": structured.get("module"),
        "platform": structured.get("platform"),
        "assignedTo": structured.get("assignedTo"),
        "steps": steps,
        "attachments": structured.get("attachments") or [],
        "state": "READY" if cit_ok else "REJECTED",
        "cit_hint": {
            "title_has_cit_keyword": cit_ok,
            "product": structured.get("product"),
        },
        "verification": verification,
    }

    validation = {
        "ok": cit_ok and bool(structured.get("bug_id")),
        "checked_fields": [
            "bug_id",
            "title",
            "status",
            "steps",
            "attachments",
            "cit_title_ok",
            "verification.mode",
        ],
        "cit_title_gate": cit_ok,
        "verification_mode": verification.get("mode"),
        "errors": errors,
    }

    if not validation["ok"]:
        _write_json(stage_root / "intermediate" / "validation.json", validation)
        return StageResult(
            blocker=Blocker(
                step="cit_title_gate",
                reason="; ".join(errors) or "validation failed",
                completed_context={"validation": validation, "structured": structured_path},
                next_actions=["Use a CIT-titled bug or disable gate in pipeline config"],
            )
        )

    tasks_doc = {
        "schema_version": "1.0",
        "run_id": ctx.run_id,
        "normalized_at": _now_iso(),
        "source": "citfix_direct + zentao_get_bug",
        "cit_title_gate": ctx.pipeline.get("cit_title_gate"),
        "tasks": [task],
        "validation": validation,
    }
    _write_json(stage_root / "output" / "tasks.json", tasks_doc)
    _write_json(stage_root / "intermediate" / "validation.json", validation)
    if verification.get("needs_proposal"):
        _write_json(
            stage_root / "intermediate" / "human_case_proposal.json",
            {
                "schema_version": "1.0",
                "bug_id": str(ctx.bug_id),
                "product": product,
                "proposed_cases": verification.get("human_cases") or [],
                "rationale": "high_risk_keyword_without_registry_hit",
                "approve_hint": (
                    "python scripts/cit_human_case_approve.py "
                    f'--product "{product}" --from-proposal '
                    f"{stage_root / 'intermediate' / 'human_case_proposal.json'}"
                ),
            },
        )
    _write_json(stage_root / "input" / "zentao_fetch_results.json", structured)
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
    return StageResult(outputs={"tasks.json": str(stage_root / "output" / "tasks.json")})


def _stage_05_cursor_handoff(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    tasks_path = ctx.run_dir / "04_result_normalize" / "output" / "tasks.json"
    if not tasks_path.is_file():
        return StageResult(blocker=Blocker("05_input", f"Missing {tasks_path}", next_actions=[f"/citfix {ctx.bug_id} --resume"]))
    tasks_doc = json.loads(tasks_path.read_text(encoding="utf-8"))
    task = (tasks_doc.get("tasks") or [{}])[0]
    stage_root = ctx.stage_root(stage_cfg)

    prompt = (
        f"Execute CIT workflow for Bug #{ctx.bug_id} (title: {task.get('title', '')}).\n"
        f"Product: {task.get('product')} | Status: {task.get('status')}\n"
        f"Evidence root: {ctx.run_dir} (run_id={ctx.run_id})\n"
        f"Resume command: /citfix {ctx.bug_id} --resume"
    )

    bundle = {
        "run_id": ctx.run_id,
        "created_at": _now_iso(),
        "items": [
            {
                "schema_version": "1.0",
                "run_id": ctx.run_id,
                "work_id": f"{ctx.run_id}-WI-0001",
                "stage": "cursor_ready",
                "created_at": _now_iso(),
                "recommended_entry": f"/citfix {ctx.bug_id}",
                "skill_chain_hint": [
                    "citfix",
                    "cit-context-prepare",
                    "cit-analyze",
                    "cit-modify",
                    "cit-review",
                    "cit-compile",
                    "cit-reproduce",
                    "cit-verify",
                    "cit-submit",
                ],
                "cit_guide_hint": ".cursor/skills/cit-context-prepare/cit_guide.md",
                "payload": task,
                "cursor_prompt": prompt,
            }
        ],
    }
    out = stage_root / "output" / "cursor_handoff_bundle.json"
    _write_json(out, bundle)
    md = stage_root / "output" / "CURSOR_HANDOFF.md"
    md.write_text(f"# Cursor Handoff — {ctx.run_id}\n\n{prompt}\n", encoding="utf-8")
    _write_json(stage_root / "input" / "tasks.json", tasks_doc)
    _write_json(stage_root / "intermediate" / f"work_item_{ctx.bug_id}.json", bundle["items"][0])
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
    return StageResult(outputs={"cursor_handoff_bundle.json": str(out)})


def _stage_06_context_snapshot(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    tasks_path = ctx.run_dir / "04_result_normalize" / "output" / "tasks.json"
    tasks_doc = json.loads(tasks_path.read_text(encoding="utf-8"))
    task = (tasks_doc.get("tasks") or [{}])[0]
    product = str(task.get("product") or "UNKNOWN")

    proj = _load_project_info(ctx, product)
    if not proj:
        return StageResult(
            blocker=Blocker(
                step="project_info",
                reason=f"No plan_bank/{product}/project_info.json — workspace paths unknown",
                completed_context={"product": product, "task": task},
                next_actions=[
                    f"Create plan_bank/{product}/project_info.json (code_root, cit_source_root, server)",
                    "Or run cit-prepare with set_workspace first",
                    f"/citfix {ctx.bug_id} --resume",
                ],
            )
        )

    code_root = str(proj.get("code_root") or "")
    cit_root = str(proj.get("cit_source_root") or "")
    server = str(proj.get("server") or "")
    device_serial = str(
        getattr(ctx, "device_serial_override", None) or proj.get("device_serial") or ""
    )
    pipe_gates = ctx.pipeline.get("gates") if isinstance(ctx.pipeline.get("gates"), dict) else {}
    allow_missing = bool(proj.get("allow_missing_attachments")) or bool(
        pipe_gates.get("allow_missing_attachments")
    )
    require_label = pipe_gates.get("require_device_label_verified")
    if require_label is None:
        require_label = True
    require_label = bool(require_label)
    # Optional: plan_bank / project_info may pre-assert label verification
    device_label_verified = bool(proj.get("device_label_verified"))

    if not code_root or not server:
        return StageResult(
            blocker=Blocker(
                step="workspace_bind",
                reason="project_info.json missing code_root or server",
                completed_context={"project_info": proj},
                next_actions=[
                    f"Fill plan_bank/{product}/project_info.json",
                    f"/citfix {ctx.bug_id} --resume",
                ],
            )
        )

    stage_root = ctx.stage_root(stage_cfg)
    out = stage_root / "output"

    cit_hint = task.get("cit_hint") if isinstance(task.get("cit_hint"), dict) else {}
    cit_ok = bool(cit_hint.get("title_has_cit_keyword")) or _cit_title_ok(
        str(task.get("title") or ""), str(task.get("steps") or "")
    )

    localized = _localize_attachments(ctx, list(task.get("attachments") or []), log_path)

    task_ref = {
        "schema_version": "1.0",
        "bug_id": str(ctx.bug_id),
        "title": task.get("title"),
        "product": product,
        "status": task.get("status"),
        "severity": task.get("severity"),
        "assigned_to": task.get("assignedTo"),
        "steps": task.get("steps"),
        "attachments": localized,
        "cit_hint": cit_hint or {"title_has_cit_keyword": cit_ok, "product": product},
    }

    workspace = {
        "schema_version": "1.0",
        "server": server,
        "code_root": code_root,
        "device_serial": device_serial,
        "meiglink_root": str(proj.get("meiglink_root") or cit_root.rsplit("/", 1)[0] if cit_root else ""),
        "cit_source_path": cit_root,
        "updated_at": _now_iso(),
    }

    gates, validation = _compute_gates(
        server=server,
        code_root=code_root,
        device_serial=device_serial,
        cit_ok=cit_ok,
        attachments=localized,
        allow_missing_attachments=allow_missing,
        device_label_verified=device_label_verified,
        require_device_label_verified=require_label,
    )

    ver = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    if not ver:
        ver = resolve_verification(
            ctx.paths.repo_root,
            product,
            str(task.get("title") or ""),
            str(task.get("steps") or ""),
        )
    # Bootstrap: keep classified_* marks; runtime always auto unless gate disabled.
    force_runtime_auto = bool(pipe_gates.get("verify_runtime_force_auto", True)) or bool(
        getattr(ctx, "force_verify_auto", False)
    )
    if getattr(ctx, "force_verify_auto", False):
        ver = {
            **ver,
            "force_verify_auto": True,
            "classification_source": ver.get("classification_source")
            or "citfix_project_force_auto",
        }
    ver = apply_runtime_verify_policy(ver, force_auto=force_runtime_auto)
    verify_mode = str(ver.get("mode") or "auto").lower()
    if verify_mode not in VERIFY_MODES:
        verify_mode = "auto"

    context = {
        "schema_version": "1.0",
        "run_id": ctx.run_id,
        "task_ref": "task_ref.json",
        "workspace_ref": "workspace.json",
        "source": {
            "project": product,
            "code_root": code_root,
            "cit_source_path": cit_root,
            "cit_version": "cit4" if "cit4" in cit_root else ("cit3.0" if "cit3" in cit_root else ""),
            "device_label_verified": device_label_verified,
        },
        "evidence": {"attachments": localized},
        "routing": {
            "compile_mode": "gradle_or_skip",
            "verify_mode": verify_mode,
        },
        "verification": ver,
        "gates": gates,
        "validation": validation,
    }

    _write_json(out / "task_ref.json", task_ref)
    _write_json(out / "workspace.json", workspace)
    _write_json(out / "context.json", context)
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))

    outputs = {
        "task_ref.json": str(out / "task_ref.json"),
        "workspace.json": str(out / "workspace.json"),
        "context.json": str(out / "context.json"),
        "attachments_dir": str(ctx.run_dir / "06_context_snapshot" / "input" / "attachments"),
    }

    # Snapshots always written; analyze gate is enforced at 07 entry (allows 06b plan_bank).
    if not gates.get("ready_for_analyze"):
        _log(log_path, f"gates.ready_for_analyze=false errors={validation.get('errors')}")

    return StageResult(outputs=outputs)


def _stage_06b_plan_bank(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    repo_run = ctx.run_dir
    snap_out = ctx.run_dir / "06_context_snapshot" / "output"
    target = repo_run / "06_context_snapshot" / "output"
    if snap_out.resolve() != target.resolve():
        target.mkdir(parents=True, exist_ok=True)
        for name in ("task_ref.json", "workspace.json", "context.json"):
            src = snap_out / name
            if not src.is_file():
                return StageResult(
                    blocker=Blocker(
                        step="06b_input",
                        reason=f"Missing {src}",
                        next_actions=[f"/citfix {ctx.bug_id} --resume"],
                    )
                )
            shutil.copy2(src, target / name)
    else:
        for name in ("task_ref.json", "workspace.json", "context.json"):
            if not (snap_out / name).is_file():
                return StageResult(
                    blocker=Blocker(
                        step="06b_input",
                        reason=f"Missing {snap_out / name}",
                        next_actions=[f"/citfix {ctx.bug_id} --resume"],
                    )
                )

    _ensure_repo_on_path(ctx.paths.repo_root)
    scripts_dir = str(ctx.paths.repo_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from cit_plan_bank_write import write_from_run  # type: ignore[import-untyped]

        written = write_from_run(repo_run)
    except Exception as e:
        return StageResult(
            blocker=Blocker(
                step="plan_bank_write",
                reason=str(e),
                next_actions=[f"/citfix {ctx.bug_id} --resume"],
            )
        )

    return StageResult(
        outputs={
            "project_info": str(written["project_info"]),
            "problem": str(written["problem"]),
            "index": str(written["index"]),
            "history": str(written.get("history", "")),
            "plans": str(written["plans"]),  # alias → problem path
        }
    )

