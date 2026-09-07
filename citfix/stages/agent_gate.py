"""Agent stage brief + output gate checks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from citfix.closure_ops import post_closure_side_effects
from citfix.models import Blocker, RunContext, StageResult
from citfix.paths import to_repo_relative
from citfix.plan_bank_sync import sync_analysis_conclusion_path
from citfix.run_log import emit_event
from citfix.stages.util_io import (
    _mirror_tree,
    _now_iso,
    _promote_to_formal,
    _write_json,
)
from citfix.stages.validate_outputs import _validate_agent_output, _verify_mode_from_context

def _skill_entries(stage_cfg: dict[str, Any], ctx: RunContext) -> list[dict[str, Any]]:
    """Resolve project skill entries for an agent stage (07/08/…)."""
    entries: list[dict[str, Any]] = []
    raw = stage_cfg.get("skills")
    if isinstance(raw, list) and raw:
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rel = str(item.get("path") or f".cursor/skills/{name}/SKILL.md").replace("\\", "/")
            abs_path = ctx.paths.repo_root / Path(rel)
            entries.append(
                {
                    "name": name,
                    "path": rel,
                    "absolute_path": to_repo_relative(ctx.paths.repo_root, abs_path),
                    "exists": abs_path.is_file(),
                    "role": item.get("role") or "primary",
                    "marker": item.get("marker") or "",
                }
            )
        return entries

    for hint in stage_cfg.get("skill_hints") or []:
        name = str(hint).strip()
        if not name or "/" in name:
            continue
        abs_path = ctx.paths.repo_root / ".cursor" / "skills" / name / "SKILL.md"
        if not abs_path.is_file():
            continue
        rel = f".cursor/skills/{name}/SKILL.md"
        entries.append(
            {
                "name": name,
                "path": rel,
                "absolute_path": to_repo_relative(ctx.paths.repo_root, abs_path),
                "exists": True,
                "role": "primary",
                "marker": "",
            }
        )
    return entries


def _resolve_input_paths(ctx: RunContext, stage_cfg: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for rel in stage_cfg.get("inputs_from") or []:
        rel_fmt = str(rel).format(bug_id=ctx.bug_id, run_id=ctx.run_id)
        formal = ctx.run_dir / Path(rel_fmt)
        inter = ctx.intermediate_dir / Path(rel_fmt)
        if formal.is_file() or formal.is_dir():
            paths.append(to_repo_relative(ctx.paths.repo_root, formal))
        elif inter.is_file() or inter.is_dir():
            paths.append(to_repo_relative(ctx.paths.repo_root, inter))
        else:
            paths.append(to_repo_relative(ctx.paths.repo_root, formal))
    return paths


def _write_agent_brief(
    ctx: RunContext,
    stage_cfg: dict[str, Any],
    *,
    out_path: Path,
    skills: list[dict[str, Any]],
) -> Path:
    """Write agent_brief under intermediate (test-bed), not formal runs/."""
    stage_inter = ctx.intermediate_stage_root(stage_cfg)
    inter = stage_inter / "intermediate"
    inter.mkdir(parents=True, exist_ok=True)
    formal_out = out_path if out_path.is_absolute() else (ctx.stage_root(stage_cfg) / out_path)
    # Prefer formal path for required_output; also document intermediate twin
    if not str(formal_out).startswith(str(ctx.run_dir)):
        formal_out = ctx.stage_root(stage_cfg) / "output" / Path(out_path).name
    inter_out = ctx.intermediate_stage_root(stage_cfg) / "output" / formal_out.name
    inputs = _resolve_input_paths(ctx, stage_cfg)
    formal_rel = to_repo_relative(ctx.paths.repo_root, formal_out)
    inter_rel = to_repo_relative(ctx.paths.repo_root, inter_out)
    run_rel = to_repo_relative(ctx.paths.repo_root, ctx.run_dir)
    inter_run_rel = to_repo_relative(ctx.paths.repo_root, ctx.intermediate_dir)
    brief = {
        "schema_version": "1.0",
        "run_id": ctx.run_id,
        "bug_id": ctx.bug_id,
        "stage_id": stage_cfg["id"],
        "title": stage_cfg.get("title"),
        "executor": "agent",
        "skills": skills,
        "skill_load_order": [s["name"] for s in skills],
        "inputs": inputs,
        "required_output": formal_rel,
        "required_output_intermediate": inter_rel,
        "formal_run_dir": run_rel,
        "intermediate_run_dir": inter_run_rel,
        "run_events": to_repo_relative(ctx.paths.repo_root, ctx.run_events_path()),
        "kb_doc": stage_cfg.get("kb_doc"),
        "kb_file": stage_cfg.get("kb_file"),
        "protocol": (
            "Read each skill SKILL.md in skill_load_order; execute fully; "
            "write required_output (formal runs/); engine mirrors to intermediate test-bed; "
            "append milestone events to run_events when entering/leaving stage; "
            "then /citfix --resume"
        ),
        "forbidden": ["~/.cursor/skills/bugfix-*", "user-level skills"],
        "created_at": _now_iso(),
    }
    if stage_cfg.get("id") == "14_closure":
        worker_cmd = (
            f'python scripts/cit_closure_run.py --run-dir "{run_rel}" '
            f"--bug-id {ctx.bug_id}"
        )
        full_cmd = (
            f'python scripts/cit_closure_run.py --run-dir "{run_rel}" '
            f"--bug-id {ctx.bug_id} --mode full"
        )
        evidence_cmd = (
            f'python scripts/cit_closure_run.py --run-dir "{run_rel}" '
            f"--bug-id {ctx.bug_id} --mode evidence_only"
        )
        brief["preferred_worker"] = {
            "script": "scripts/cit_closure_run.py",
            "default": worker_cmd,
            "local_commit_only": worker_cmd,
            "full_reserved": full_cmd,
            "evidence_only": evidence_cmd,
            "outbox_drain": (
                f'python scripts/cit_outbox_drain.py --run-dir "{run_rel}"'
            ),
            "note": (
                "Default: local commit only (no push). "
                "--mode full reserved for later push+zentao. Then /citfix --resume"
            ),
        }
        brief["protocol"] = (
            "Prefer scripts/cit_closure_run.py (default local_commit_only; "
            "full is opt-in reserved); or cit-submit skill; write closure JSON; "
            "engine validates, syncs 08, drains outbox; then /citfix --resume"
        )
    brief_path = inter / "agent_brief.json"
    _write_json(brief_path, brief)

    lines = [
        f"# Agent Brief — {stage_cfg['id']} (run {ctx.run_id})",
        "",
        f"Bug: **{ctx.bug_id}**",
        "",
        "## Paths",
        "",
        f"- Formal (正式产物): `{run_rel}`",
        f"- Intermediate (中间产物): `{inter_run_rel}`",
        f"- Run events: `{to_repo_relative(ctx.paths.repo_root, ctx.run_events_path())}`",
        "",
        "## Load project skills (in order)",
        "",
    ]
    for i, s in enumerate(skills, 1):
        status = "OK" if s.get("exists") else "MISSING"
        lines.append(f"{i}. `{s['name']}` ({s.get('role')}) — `{s['path']}` [{status}]")
        lines.append(f"   - path: `{s['absolute_path']}`")
    if stage_cfg.get("id") == "14_closure":
        lines.extend(
            [
                "",
                "## Preferred worker (deterministic)",
                "",
                "```powershell",
                f'cd "{ctx.paths.repo_root}"',
                f'.\\.venv\\Scripts\\python.exe scripts\\cit_closure_run.py --run-dir "{run_rel}" --bug-id {ctx.bug_id}',
                "```",
                "",
                "Default: commit only (no push). Opt-in reserved: `--mode full`. Probe: `--mode evidence_only`.",
                "",
            ]
        )
    lines.extend(["", "## Inputs", ""])
    for p in inputs:
        lines.append(f"- `{p}`")
    lines.extend(
        [
            "",
            "## Required output (formal)",
            "",
            f"- `{formal_out}`",
            "",
            "## Intermediate twin (auto-mirrored / also accepted)",
            "",
            f"- `{inter_out}`",
            "",
            "## After done",
            "",
            "```text",
            f"/citfix {ctx.bug_id} --resume",
            "```",
            "",
            "Do **not** load user-level bugfix-* skills.",
            "",
        ]
    )
    (inter / "AGENT_BRIEF.md").write_text("\n".join(lines), encoding="utf-8")
    emit_event(
        ctx.run_dir,
        actor="agent",
        event="agent_stage_enter",
        summary=f"AGENT_BRIEF written for {stage_cfg.get('id')}",
        run_id=str(ctx.run_id),
        bug_id=str(ctx.bug_id),
        stage_id=str(stage_cfg.get("id") or ""),
        refs={
            "agent_brief": to_repo_relative(ctx.paths.repo_root, brief_path),
            "required_output": formal_rel,
            "run_events": to_repo_relative(ctx.paths.repo_root, ctx.run_events_path()),
        },
    )
    return brief_path


def _agent_stage_check(
    ctx: RunContext,
    stage_cfg: dict[str, Any],
    *,
    output_rel: str,
    default_blocker_reason: str,
) -> StageResult:
    sid = stage_cfg["id"]
    context_path = ctx.run_dir / "06_context_snapshot" / "output" / "context.json"
    context_inter = ctx.intermediate_dir / "06_context_snapshot" / "output" / "context.json"
    if not context_path.is_file() and context_inter.is_file():
        _promote_to_formal(context_inter, context_path)
    context: dict[str, Any] = {}
    if context_path.is_file():
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except Exception:
            context = {}
        gates = context.get("gates") if isinstance(context.get("gates"), dict) else {}
        validation = context.get("validation") if isinstance(context.get("validation"), dict) else {}
        if sid == "07_analysis" and not gates.get("ready_for_analyze"):
            return StageResult(
                blocker=Blocker(
                    step="ready_for_analyze",
                    reason="; ".join(validation.get("errors") or []) or "gates.ready_for_analyze=false",
                    completed_context={"gates": gates, "validation": validation, "context": str(context_path)},
                    next_actions=[
                        "Fix 06 context gates (attachments/workspace) then resume",
                        "Or set allow_missing_attachments=true in plan_bank project_info.json",
                        f"/citfix {ctx.bug_id} --resume",
                    ],
                )
            )
        if sid in ("11_qfil_flash", "12_cit_test") and not gates.get("ready_for_reproduce"):
            return StageResult(
                blocker=Blocker(
                    step="ready_for_reproduce",
                    reason="; ".join(validation.get("errors") or []) or "gates.ready_for_reproduce=false",
                    completed_context={"gates": gates, "validation": validation},
                    next_actions=[
                        "Set device_serial in plan_bank/<PRODUCT>/project_info.json",
                        "Re-run 06 or update workspace.json then /citfix --resume",
                    ],
                )
            )
        if sid in ("11_qfil_flash", "12_cit_test"):
            label_ok = bool(
                gates.get("ready_for_flash_verify")
                or (isinstance(context.get("source"), dict) and context["source"].get("device_label_verified"))
            )
            # Prefer explicit gate; fall back to source field for older context.json
            src = context.get("source") if isinstance(context.get("source"), dict) else {}
            verified = bool(src.get("device_label_verified") or gates.get("device_label_verified"))
            pipe_gates = ctx.pipeline.get("gates") if isinstance(ctx.pipeline.get("gates"), dict) else {}
            require_label = pipe_gates.get("require_device_label_verified")
            if require_label is None:
                require_label = True
            if require_label and not verified:
                return StageResult(
                    blocker=Blocker(
                        step="device_label_verified",
                        reason=(
                            "device_label_verified=false blocks flash/verify — "
                            "confirm device serial/CIT label then set "
                            "context.source.device_label_verified=true (and gates.ready_for_flash_verify)"
                        ),
                        completed_context={
                            "gates": gates,
                            "source.device_label_verified": src.get("device_label_verified"),
                            "context": str(context_path),
                        },
                        next_actions=[
                            "adb/MCP: verify device matches plan_bank device_serial + CIT product label",
                            "Edit 06 context.json: source.device_label_verified=true; "
                            "gates.ready_for_flash_verify=true; gates.device_label_verified=true",
                            "Or set plan_bank/<PRODUCT>/project_info.json device_label_verified=true and re-run 06",
                            f"/citfix {ctx.bug_id} --resume",
                        ],
                    )
                )
            if require_label and gates.get("ready_for_flash_verify") is False and not label_ok:
                return StageResult(
                    blocker=Blocker(
                        step="ready_for_flash_verify",
                        reason="gates.ready_for_flash_verify=false",
                        completed_context={"gates": gates, "validation": validation},
                        next_actions=[
                            "Set device_label_verified=true after label check",
                            f"/citfix {ctx.bug_id} --resume",
                        ],
                    )
                )

    if sid in ("11_qfil_flash", "12_cit_test"):
        art_path = ctx.run_dir / "10_artifacts" / "output" / f"artifact_{ctx.bug_id}.json"
        if art_path.is_file():
            try:
                art = json.loads(art_path.read_text(encoding="utf-8"))
            except Exception:
                art = {}
            if art.get("deploy_pending") is True:
                return StageResult(
                    blocker=Blocker(
                        step="deploy_pending",
                        reason="10_artifacts.deploy_pending=true — finish device deploy before flash/test",
                        completed_context={"artifact": str(art_path)},
                        next_actions=[
                            "adb deploy artifacts and set deploy_pending=false with verified hashes",
                            f"/citfix {ctx.bug_id} --resume",
                        ],
                    )
                )

    stage_root = ctx.stage_root(stage_cfg)
    stage_inter = ctx.intermediate_stage_root(stage_cfg)
    out_rel = output_rel.format(bug_id=ctx.bug_id)
    out_path = stage_root / out_rel
    out_inter = stage_inter / out_rel
    if out_inter.is_file():
        _promote_to_formal(out_inter, out_path)

    if sid == "08_changes":
        rc = ctx.run_dir / "07_analysis" / "output" / f"root_cause_{ctx.bug_id}.json"
        rc_inter = ctx.intermediate_dir / "07_analysis" / "output" / f"root_cause_{ctx.bug_id}.json"
        if rc_inter.is_file():
            _promote_to_formal(rc_inter, rc)
        if not rc.is_file():
            return StageResult(
                blocker=Blocker(
                    step="08_input",
                    reason=f"Missing 07 output: {rc}",
                    completed_context={"expected": str(rc), "intermediate": str(rc_inter)},
                    next_actions=[
                        "Complete 07_analysis (cit-analyze) first",
                        f"/citfix {ctx.bug_id} --resume",
                    ],
                )
            )

    skills = _skill_entries(stage_cfg, ctx)
    brief_path = _write_agent_brief(ctx, stage_cfg, out_path=out_path, skills=skills)
    brief_md = stage_inter / "intermediate" / "AGENT_BRIEF.md"

    missing_skills = [s["name"] for s in skills if not s.get("exists")]
    if missing_skills:
        return StageResult(
            blocker=Blocker(
                step="skill_missing",
                reason=f"Project skills missing: {', '.join(missing_skills)}",
                completed_context={"skills": skills, "agent_brief": str(brief_path)},
                next_actions=[
                    f"Add SKILL.md under .cursor/skills/<name>/ for: {', '.join(missing_skills)}",
                    f"/citfix {ctx.bug_id} --resume",
                ],
            )
        )

    if out_path.is_file():
        v_errs = _validate_agent_output(
            sid,
            out_path,
            context=context,
            run_dir=ctx.run_dir,
            bug_id=str(ctx.bug_id),
        )
        if v_errs:
            step = "human_gate" if sid == "13_human_gate" and any(
                "pending_human" in e for e in v_errs
            ) else "output_validation"
            emit_event(
                ctx.run_dir,
                actor="agent",
                event="agent_gate_fail",
                summary=f"{sid} output validation failed ({len(v_errs)} errors)",
                run_id=str(ctx.run_id),
                bug_id=str(ctx.bug_id),
                stage_id=sid,
                level="warn",
                refs={
                    "step": step,
                    "validation_errors": v_errs[:20],
                    "expected_output": to_repo_relative(ctx.paths.repo_root, out_path),
                },
            )
            emit_event(
                ctx.run_dir,
                actor="engine",
                event="gate_fail",
                summary=f"{sid} output validation failed ({len(v_errs)} errors)",
                run_id=str(ctx.run_id),
                bug_id=str(ctx.bug_id),
                stage_id=sid,
                level="warn",
                refs={
                    "step": step,
                    "validation_errors": v_errs[:20],
                    "expected_output": to_repo_relative(ctx.paths.repo_root, out_path),
                },
            )
            return StageResult(
                outputs={"agent_brief": str(brief_path), "rejected_output": str(out_path)},
                blocker=Blocker(
                    step=step,
                    reason="; ".join(v_errs),
                    completed_context={
                        "expected_output": to_repo_relative(ctx.paths.repo_root, out_path),
                        "intermediate_twin": to_repo_relative(ctx.paths.repo_root, out_inter),
                        "validation_errors": v_errs,
                        "agent_brief": to_repo_relative(ctx.paths.repo_root, brief_path),
                        "skills": skills,
                        "verify_mode": _verify_mode_from_context(context),
                    },
                    next_actions=[
                        f"Fix or replace invalid output: {to_repo_relative(ctx.paths.repo_root, out_path)}",
                        "For 12: split auto_assertions vs human_assertions; VERIFY_PASS needs auto all true",
                        "For 13: auto→auto_bypassed/not_required; human/hybrid→approved with approved_by",
                        "For 14: run scripts/cit_closure_run.py or cit-submit → marker=CLOSURE_DONE",
                        f"Read AGENT_BRIEF: {brief_md}",
                        f"/citfix {ctx.bug_id} --resume",
                    ],
                ),
            )
        side_outputs: dict[str, str] = {}
        if sid == "07_analysis":
            product = ""
            if isinstance(context.get("source"), dict):
                product = str(context["source"].get("project") or "")
            if not product:
                product = str(getattr(ctx, "product", None) or "")
            rel, path_errs = sync_analysis_conclusion_path(
                ctx.paths.repo_root,
                product=product or "_unassigned",
                bug_id=str(ctx.bug_id),
                run_id=str(ctx.run_id),
                root_cause_path=out_path,
            )
            if path_errs:
                return StageResult(
                    outputs={
                        "primary": str(out_path),
                        "agent_brief": str(brief_path),
                        "plan_bank_path_attempt": rel,
                    },
                    blocker=Blocker(
                        step="plan_bank_path",
                        reason="; ".join(path_errs),
                        completed_context={
                            "expected_analysis_conclusion_path": rel,
                            "root_cause": to_repo_relative(ctx.paths.repo_root, out_path),
                            "errors": path_errs,
                            "note": "run artifacts kept; fix path/layout then resume",
                        },
                        next_actions=[
                            f"Ensure formal file exists: {rel}",
                            "Do not continue 08 until plan_bank analysis_conclusion_path is valid",
                            f"/citfix {ctx.bug_id} --resume",
                        ],
                    ),
                )
            side_outputs["analysis_conclusion_path"] = rel
        if sid == "14_closure":
            try:
                closure_data = json.loads(out_path.read_text(encoding="utf-8"))
            except Exception:
                closure_data = {}
            if isinstance(closure_data, dict):
                summary = post_closure_side_effects(
                    ctx.run_dir,
                    str(ctx.bug_id),
                    closure_data,
                    repo_root=ctx.paths.repo_root,
                    force_redeliver=True,
                )
                side_outputs["outbox"] = str(summary.get("outbox") or "")
                if summary.get("closure_report"):
                    side_outputs["closure_report"] = str(summary["closure_report"])
                if summary.get("outbox_drain"):
                    side_outputs["outbox_drain"] = json.dumps(
                        summary["outbox_drain"], ensure_ascii=False
                    )
                # Mirror 08 change if updated
                ch = ctx.run_dir / "08_changes" / "output" / f"change_{ctx.bug_id}.json"
                if ch.is_file():
                    _mirror_tree(ctx.run_dir / "08_changes" / "output", ctx.intermediate_dir / "08_changes" / "output")
                    side_outputs["change_synced"] = str(ch)
        _mirror_tree(stage_root / "output", stage_inter / "output")
        emit_event(
            ctx.run_dir,
            actor="agent",
            event="agent_gate_pass",
            summary=f"{sid} output accepted",
            run_id=str(ctx.run_id),
            bug_id=str(ctx.bug_id),
            stage_id=sid,
            refs={
                "primary": to_repo_relative(ctx.paths.repo_root, out_path),
                "agent_brief": to_repo_relative(ctx.paths.repo_root, brief_path),
            },
        )
        return StageResult(
            outputs={
                "primary": str(out_path),
                "intermediate": str(out_inter),
                "agent_brief": str(brief_path),
                **side_outputs,
            }
        )

    hint_names = [s["name"] for s in skills] or list(stage_cfg.get("skill_hints") or [])
    out_rel = to_repo_relative(ctx.paths.repo_root, out_path)
    inter_rel = to_repo_relative(ctx.paths.repo_root, out_inter)
    brief_rel = to_repo_relative(ctx.paths.repo_root, brief_path)
    brief_md_rel = to_repo_relative(ctx.paths.repo_root, brief_md)
    load_lines = [
        f"Read AGENT_BRIEF: {brief_md_rel}",
        f"Read agent_brief.json: {brief_rel}",
    ]
    for s in skills:
        load_lines.append(f"Load and execute project skill: {s['path']} ({s.get('role')})")
    load_lines.extend(
        [
            f"Write formal output: {out_rel}",
            f"(Also OK to write intermediate twin first: {inter_rel} — engine promotes)",
            f"Append milestone: python scripts/cit_run_event.py --run-dir \"{to_repo_relative(ctx.paths.repo_root, ctx.run_dir)}\" "
            f"--bug-id {ctx.bug_id} --stage {sid} --event agent_checkpoint "
            f"--summary \"waiting for work\" --level warn",
            f"Resume: /citfix {ctx.bug_id} --resume",
        ]
    )
    emit_event(
        ctx.run_dir,
        actor="agent",
        event="agent_checkpoint",
        summary=default_blocker_reason,
        run_id=str(ctx.run_id),
        bug_id=str(ctx.bug_id),
        stage_id=sid,
        level="warn",
        refs={
            "expected_output": out_rel,
            "agent_brief": brief_rel,
        },
    )
    return StageResult(
        outputs={"agent_brief": brief_rel},
        blocker=Blocker(
            step=stage_cfg["id"],
            reason=default_blocker_reason,
            completed_context={
                "run_id": ctx.run_id,
                "bug_id": ctx.bug_id,
                "expected_output": out_rel,
                "intermediate_twin": inter_rel,
                "kb_doc": stage_cfg.get("kb_doc"),
                "skill_hints": hint_names,
                "skills": skills,
                "agent_brief": brief_rel,
            },
            next_actions=load_lines,
        ),
    )

