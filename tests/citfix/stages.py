"""Stage executors for /citfix pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Blocker, RunContext, StageResult

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _log(log_path: Path, msg: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{_now_iso()}] {msg}\n")


def _mirror_tree(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.is_dir():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src_dir)
            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _cit_title_ok(title: str, steps: str = "") -> bool:
    text = f"{title}\n{steps}"
    return bool(re.search(r"CIT|【CIT】|\[CIT问题\]", text, re.I))


def _ensure_repo_on_path(repo: Path) -> None:
    s = str(repo)
    if s not in sys.path:
        sys.path.insert(0, s)


def _load_project_info(ctx: RunContext, product: str) -> dict[str, Any] | None:
    info = ctx.paths.repo_root / "plan_bank" / product / "project_info.json"
    if info.is_file():
        return json.loads(info.read_text(encoding="utf-8"))
    return None


def _agent_stage_check(
    ctx: RunContext,
    stage_cfg: dict[str, Any],
    *,
    output_rel: str,
    default_blocker_reason: str,
) -> StageResult:
    stage_root = ctx.stage_root(stage_cfg)
    out_path = stage_root / output_rel.format(bug_id=ctx.bug_id)
    if out_path.is_file():
        _mirror_tree(stage_root / "output", ctx.run_stage_mirror(stage_cfg) / "output")
        return StageResult(outputs={"primary": str(out_path)})
    hints = stage_cfg.get("skill_hints") or []
    return StageResult(
        blocker=Blocker(
            step=stage_cfg["id"],
            reason=default_blocker_reason,
            completed_context={
                "run_id": ctx.run_id,
                "bug_id": ctx.bug_id,
                "expected_output": str(out_path),
                "kb_doc": stage_cfg.get("kb_doc"),
                "skill_hints": hints,
            },
            next_actions=[
                f"Read KB doc: {stage_cfg.get('kb_file', '')}",
                f"Execute stage with skills: {', '.join(hints) if hints else 'see citfix skill'}",
                f"Write output to: {out_path}",
                f"Resume: /citfix {ctx.bug_id} --resume",
            ],
        )
    )


def execute_stage(stage_cfg: dict[str, Any], ctx: RunContext) -> StageResult:
    executor = stage_cfg.get("executor", "auto")
    log_path = ctx.stage_log_path(stage_cfg)
    _log(log_path, f"start {stage_cfg['id']}")

    if executor == "skip_direct":
        _log(log_path, "skipped (citfix_direct entry)")
        return StageResult(skipped=True)

    if executor == "agent":
        req = (stage_cfg.get("required_outputs") or ["output/result_{bug_id}.json"])[0]
        return _agent_stage_check(
            ctx,
            stage_cfg,
            output_rel=req,
            default_blocker_reason=f"Stage {stage_cfg['id']} requires agent/MCP execution; output not found.",
        )

    handlers = {
        "00_run_registry": _stage_00_run_registry,
        "03_zentao_fetch": _stage_03_zentao_fetch,
        "04_result_normalize": _stage_04_result_normalize,
        "05_cursor_handoff": _stage_05_cursor_handoff,
        "06_context_snapshot": _stage_06_context_snapshot,
        "06b_plan_bank": _stage_06b_plan_bank,
    }
    fn = handlers.get(stage_cfg["id"])
    if fn is None:
        return StageResult(
            blocker=Blocker(
                step=stage_cfg["id"],
                reason=f"No auto handler for stage {stage_cfg['id']}",
                next_actions=["Implement handler or mark as agent stage"],
            )
        )
    result = fn(ctx, stage_cfg, log_path)
    _log(log_path, f"done {stage_cfg['id']} status={'blocked' if result.blocker else 'ok'}")
    return result


def _stage_00_run_registry(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    wf = ctx.run_dir / "workflow_state.json"
    run_json = {
        "schema_version": "1.0",
        "run_id": ctx.run_id,
        "bug_id": ctx.bug_id,
        "entry_mode": "citfix_direct",
        "trigger": f"/citfix {ctx.bug_id}",
        "started_at": _now_iso(),
        "status": "RUNNING",
    }
    _write_json(ctx.run_dir / "run.json", run_json)
    index = {
        "run_id": ctx.run_id,
        "bug_id": int(ctx.bug_id),
        "entry_mode": "citfix_direct",
        "trigger": f"/citfix {ctx.bug_id}",
        "pipeline": "citfix",
        "started_at": _now_iso(),
        "status": "RUNNING",
    }
    _write_json(ctx.run_dir / "run_index.json", index)
    return StageResult(outputs={"workflow_state": str(wf), "run_json": str(ctx.run_dir / "run.json")})


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
        ctx.paths.test_bed_root
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
    }

    validation = {
        "ok": cit_ok and bool(structured.get("bug_id")),
        "checked_fields": ["bug_id", "title", "status", "steps", "attachments", "cit_title_ok"],
        "cit_title_gate": cit_ok,
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
    _write_json(stage_root / "input" / "zentao_fetch_results.json", structured)
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))
    return StageResult(outputs={"tasks.json": str(stage_root / "output" / "tasks.json")})


def _stage_05_cursor_handoff(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    tasks_path = ctx.paths.test_bed_root / "04_result_normalize" / "output" / "tasks.json"
    if not tasks_path.is_file():
        return StageResult(blocker=Blocker("05_input", f"Missing {tasks_path}", next_actions=[f"/citfix {ctx.bug_id} --resume"]))
    tasks_doc = json.loads(tasks_path.read_text(encoding="utf-8"))
    task = (tasks_doc.get("tasks") or [{}])[0]
    stage_root = ctx.stage_root(stage_cfg)

    prompt = (
        f"Execute CIT workflow for Bug #{ctx.bug_id} (title: {task.get('title', '')}).\n"
        f"Product: {task.get('product')} | Status: {task.get('status')}\n"
        f"Evidence root: {ctx.paths.test_bed_root} (run_id={ctx.run_id})\n"
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
                    "bugfix-reproduce",
                    "bugfix-analyze",
                    "bugfix-modify",
                    "bugfix-review",
                    "bugfix-compile",
                    "bugfix-verify",
                    "bugfix-submit",
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
    tasks_path = ctx.paths.test_bed_root / "04_result_normalize" / "output" / "tasks.json"
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
    device_serial = str(proj.get("device_serial") or "")

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

    task_ref = {
        "schema_version": "1.0",
        "bug_id": str(ctx.bug_id),
        "title": task.get("title"),
        "product": product,
        "status": task.get("status"),
        "severity": task.get("severity"),
        "assigned_to": task.get("assignedTo"),
        "steps": task.get("steps"),
        "attachments": [],
        "cit_hint": task.get("cit_hint") or {},
    }

    for att in task.get("attachments") or []:
        entry = dict(att)
        entry.setdefault("local_path", "")
        entry.setdefault("sha256", "")
        entry["missing"] = True
        task_ref["attachments"].append(entry)

    workspace = {
        "schema_version": "1.0",
        "server": server,
        "code_root": code_root,
        "device_serial": device_serial,
        "meiglink_root": str(proj.get("meiglink_root") or cit_root.rsplit("/", 1)[0] if cit_root else ""),
        "cit_source_path": cit_root,
        "updated_at": _now_iso(),
    }

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
            "device_label_verified": False,
        },
        "evidence": {"attachments": task_ref["attachments"]},
        "routing": {"compile_mode": "gradle_or_skip", "verify_mode": "cit"},
        "gates": {"ready_for_analyze": True},
        "validation": {"ok": True, "errors": []},
    }

    if not device_serial:
        context["validation"]["errors"].append("device_serial empty in project_info — bind device before reproduce")

    _write_json(out / "task_ref.json", task_ref)
    _write_json(out / "workspace.json", workspace)
    _write_json(out / "context.json", context)
    _mirror_tree(stage_root, ctx.run_stage_mirror(stage_cfg))

    # Also mirror to cit-workflow runs for plan_bank script
    repo_run = ctx.paths.repo_root / "runs" / ctx.run_id / "06_context_snapshot" / "output"
    repo_run.mkdir(parents=True, exist_ok=True)
    for name in ("task_ref.json", "workspace.json", "context.json"):
        shutil.copy2(out / name, repo_run / name)

    return StageResult(
        outputs={
            "task_ref.json": str(out / "task_ref.json"),
            "workspace.json": str(out / "workspace.json"),
            "context.json": str(out / "context.json"),
        }
    )


def _stage_06b_plan_bank(ctx: RunContext, stage_cfg: dict[str, Any], log_path: Path) -> StageResult:
    repo_run = ctx.paths.repo_root / "runs" / ctx.run_id
    snap_out = ctx.paths.test_bed_root / "06_context_snapshot" / "output"
    target = repo_run / "06_context_snapshot" / "output"
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
            "plans": str(written["plans"]),
        }
    )
