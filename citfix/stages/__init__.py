"""Stage executors for /citfix pipeline."""
from __future__ import annotations

import json
from typing import Any

from citfix.models import Blocker, RunContext, StageResult
from citfix.paths import to_repo_relative
from citfix.run_log import emit_event
from citfix.stages.agent_gate import _agent_stage_check
from citfix.stages.auto_prepare import (
    _stage_00_run_registry,
    _stage_03_zentao_fetch,
    _stage_04_result_normalize,
    _stage_05_cursor_handoff,
    _stage_06_context_snapshot,
    _stage_06b_plan_bank,
)
from citfix.stages.req_extract import _stage_01_req_parse, _stage_02_bug_task_extract
from citfix.stages.util_io import _log
from citfix.stages.validate_outputs import _validate_agent_output

__all__ = ["execute_stage", "_validate_agent_output"]


def execute_stage(stage_cfg: dict[str, Any], ctx: RunContext) -> StageResult:
    executor = stage_cfg.get("executor", "auto")
    log_path = ctx.stage_log_path(stage_cfg)
    _log(log_path, f"start {stage_cfg['id']}")

    if executor == "skip_direct":
        _log(log_path, "skipped (entry skip_stages)")
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
        "01_req_parse": _stage_01_req_parse,
        "02_bug_task_extract": _stage_02_bug_task_extract,
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
    if not result.blocker and not result.skipped:
        result = _check_required_outputs(ctx, stage_cfg, result)
    if result.blocker:
        emit_event(
            ctx.run_dir,
            actor="engine",
            event="gate_fail",
            summary=f"{stage_cfg['id']} blocked: {result.blocker.reason}",
            run_id=str(ctx.run_id),
            bug_id=str(ctx.bug_id),
            stage_id=str(stage_cfg["id"]),
            level="warn",
            refs={
                "step": result.blocker.step,
                "log_path": to_repo_relative(ctx.paths.repo_root, log_path),
            },
        )
    _log(log_path, f"done {stage_cfg['id']} status={'blocked' if result.blocker else 'ok'}")
    return result


def _check_required_outputs(
    ctx: RunContext, stage_cfg: dict[str, Any], result: StageResult
) -> StageResult:
    """Light gate for auto stages that declare a skill completion marker (e.g. 01/02)."""
    marker = ""
    for sk in stage_cfg.get("skills") or []:
        if isinstance(sk, dict) and sk.get("marker"):
            marker = str(sk["marker"])
            break
    # Skip stages without markers (00 writes run.json; workflow_state is filled by engine).
    if not marker:
        return result
    required = list(stage_cfg.get("required_outputs") or [])
    if not required:
        return result
    stage_root = ctx.stage_root(stage_cfg)
    missing: list[str] = []
    for i, rel in enumerate(required):
        # Contract paths are relative to stage dir, or run/intermediate root for 00.
        p = stage_root / rel
        if not p.is_file():
            p2 = ctx.run_dir / rel
            p3 = ctx.intermediate_dir / rel
            if p2.is_file():
                p = p2
            elif p3.is_file():
                p = p3
            else:
                missing.append(rel)
                continue
        # Marker only on primary artifact (first required_output); compat files may omit it.
        if marker and i == 0 and p.suffix == ".json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if str(data.get("marker") or "") != marker:
                    return StageResult(
                        outputs=result.outputs,
                        blocker=Blocker(
                            step=f"{stage_cfg['id']}.marker",
                            reason=f"{rel} marker must be {marker}",
                            next_actions=[f"Re-run {stage_cfg['id']}"],
                        ),
                    )
            except Exception as e:  # noqa: BLE001
                return StageResult(
                    outputs=result.outputs,
                    blocker=Blocker(
                        step=f"{stage_cfg['id']}.read",
                        reason=f"Cannot read {rel}: {e}",
                        next_actions=[f"Re-run {stage_cfg['id']}"],
                    ),
                )
    if missing:
        return StageResult(
            outputs=result.outputs,
            blocker=Blocker(
                step=f"{stage_cfg['id']}.outputs",
                reason=f"Missing required outputs: {', '.join(missing)}",
                next_actions=[f"Re-run {stage_cfg['id']}"],
            ),
        )
    return result
