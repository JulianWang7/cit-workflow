#!/usr/bin/env python3
"""CLI for /citfix resumable CIT workflow.

Usage:
  python scripts/citfix.py 97203
  python scripts/citfix.py 97203 --resume
  python scripts/citfix.py 97203 --status
  python scripts/citfix.py 97203 --debug
  python scripts/citfix.py project slb783
  python scripts/citfix.py project slb783 --device SERIAL
  python scripts/citfix.py project slb783 --dry-run
  python scripts/citfix.py project slb783 --fixture fixtures/project_discover
  python scripts/citfix.py project slb783 --resume
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = str(Path(__file__).resolve().parent)
# Running `python scripts/citfix.py` puts scripts/ on sys.path[0], which shadows
# the citfix package with this file. Prefer the repo package root.
while sys.path and sys.path[0] in ("", ".", _SCRIPTS):
    sys.path.pop(0)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from citfix import find_run_for_bug, load_state, run_pipeline  # noqa: E402
from citfix.cli_parse import CitfixCommand, parse_citfix_tokens  # noqa: E402
from citfix.paths import UNASSIGNED_PROJECT, load_pipeline_config, read_product_from_run  # noqa: E402
from citfix.project_batch import run_project_batch  # noqa: E402


def _formal_for_state(paths, state):
    found = paths.find_formal_run_dir(state.run_id)
    if found:
        return found
    prod = read_product_from_run(
        paths.find_intermediate_run_dir(state.run_id) or Path(".")
    ) or UNASSIGNED_PROJECT
    return paths.formal_run_dir(state.run_id, prod)


def _print_blocker(state) -> None:
    cp = state.checkpoint or {}
    print("\n=== WORKFLOW BLOCKED ===")
    print(f"run_id:    {state.run_id}")
    print(f"stage:     {cp.get('stage', state.current_stage)}")
    print(f"step:      {cp.get('step', '')}")
    print(f"reason:    {cp.get('reason', '')}")
    print("\n--- completed context ---")
    print(json.dumps(cp.get("completed_context") or {}, ensure_ascii=False, indent=2))
    print("\n--- next actions ---")
    for i, act in enumerate(cp.get("next_actions") or [], 1):
        print(f"  {i}. {act}")
    print(f"\nResume: /citfix {state.bug_id} --resume")
    paths = load_pipeline_config()[0]
    formal = _formal_for_state(paths, state)
    inter = paths.find_intermediate_run_dir(state.run_id) or paths.intermediate_run_dir(
        state.run_id
    )
    print(f"Formal (正式产物):      {formal}")
    print(f"Intermediate (中间产物): {inter}")
    print(f"Checkpoint file:         {inter / 'CHECKPOINT.md'}")


def _print_status(state) -> None:
    paths = load_pipeline_config()[0]
    formal = _formal_for_state(paths, state)
    inter = paths.find_intermediate_run_dir(state.run_id) or paths.intermediate_run_dir(
        state.run_id
    )
    print(f"run_id: {state.run_id}  bug_id: {state.bug_id}  status: {state.workflow_status}")
    print(f"current_stage: {state.current_stage}")
    print(f"entry_mode:    {getattr(state, 'entry_mode', '')}")
    print(f"formal:        {formal}")
    print(f"intermediate:  {inter}")
    for sid, rec in state.stages.items():
        print(f"  {sid}: {rec.status.value}")


def _print_debug(state) -> None:
    """Manual debug dump: paths, checkpoint, key artifacts."""
    paths = load_pipeline_config()[0]
    formal = _formal_for_state(paths, state)
    inter = paths.find_intermediate_run_dir(state.run_id) or paths.intermediate_run_dir(
        state.run_id
    )
    print("=== CITFIX DEBUG ===")
    print(f"bug_id:          {state.bug_id}")
    print(f"run_id:          {state.run_id}")
    print(f"workflow_status: {state.workflow_status}")
    print(f"current_stage:   {state.current_stage}")
    print(f"entry_mode:      {getattr(state, 'entry_mode', '')}")
    print(f"formal_run_dir:  {formal}")
    print(f"intermediate:    {inter}")
    print(f"checkpoint_md:   {inter / 'CHECKPOINT.md'}")
    print(f"workflow_state:  {inter / 'workflow_state.json'}")
    cp = state.checkpoint or {}
    if cp:
        print("\n--- checkpoint ---")
        print(json.dumps(cp, ensure_ascii=False, indent=2))
    print("\n--- stages ---")
    for sid, rec in state.stages.items():
        line = f"  {sid}: {rec.status.value}"
        if rec.log_path:
            line += f"  log={rec.log_path}"
        print(line)
    print("\n--- key artifacts (formal) ---")
    for rel in (
        f"03_zentao_fetch/output/zentao_fetch_results.json",
        f"04_result_normalize/output/tasks.json",
        f"06_context_snapshot/output/context.json",
        f"07_analysis/output/root_cause_{state.bug_id}.json",
        f"08_changes/output/change_{state.bug_id}.json",
        f"CHECKPOINT.md",
    ):
        p = formal / rel
        print(f"  [{'OK' if p.is_file() else '--'}] {p}")
    print("\nResume: /citfix {0} --resume".format(state.bug_id))
    print("Agent debug entry: cursor-agent --workspace <cit-workflow> \"/citfix {0} --resume\"".format(state.bug_id))


def _print_batch(state, batch_dir: Path) -> None:
    print(f"batch_id:      {state.batch_id}")
    print(f"alias:         {state.project_alias}")
    print(f"product:       {state.product_name} (#{state.product_id})")
    print(f"zentao_user:   {state.zentao_user}")
    print(f"device_serial: {state.device_serial or '(from plan_bank)'}")
    print(f"batch_status:  {state.batch_status}")
    print(f"batch_dir:     {batch_dir}")
    print(f"queue ({len(state.queue)}):")
    for i, q in enumerate(state.queue):
        mark = ">" if i == state.current_index and state.batch_status == "running" else " "
        print(f"  {mark} [{q.status}] #{q.bug_id} {q.title[:60]}")
        if q.reason:
            print(f"      reason: {q.reason}")
    if state.rejected:
        print(f"rejected ({len(state.rejected)}):")
        for r in state.rejected[:20]:
            print(
                f"  - #{r.get('bug_id')}: {r.get('reject_reason')} "
                f"| {str(r.get('title') or '')[:50]}"
            )


def _load_bug_state(paths, cmd):
    bug_id = cmd.bug_id or ""
    run_dir = None
    if cmd.run_id:
        run_dir = paths.find_intermediate_run_dir(cmd.run_id) or paths.find_formal_run_dir(
            cmd.run_id
        )
    else:
        run_dir = find_run_for_bug(paths, bug_id)
    if not run_dir:
        return None
    return load_state(run_dir)


def _run_bug(cmd) -> int:
    paths, _ = load_pipeline_config()
    bug_id = cmd.bug_id or ""

    if cmd.status or cmd.debug:
        state = _load_bug_state(paths, cmd)
        if not state:
            print(f"No workflow run found for bug {bug_id}", file=sys.stderr)
            return 1
        if cmd.debug:
            _print_debug(state)
        else:
            _print_status(state)
        return 0

    state = run_pipeline(
        bug_id,
        resume=cmd.resume or not cmd.new,
        run_id=cmd.run_id,
        force_new=cmd.new,
        entry_mode="citfix_direct",
    )

    if cmd.debug:
        _print_debug(state)

    if state.workflow_status == "completed":
        print(f"OK — all stages completed for bug {bug_id} (run_id={state.run_id})")
        return 0

    if state.workflow_status == "blocked":
        _print_blocker(state)
        return 2

    print(f"Workflow ended with status: {state.workflow_status}")
    return 0


def _run_project(cmd) -> int:
    alias = cmd.project_alias or ""
    discover_fn = None
    if cmd.fixture:
        from citfix.project_fixture import make_discover_fn_from_fixture

        fix = Path(cmd.fixture)
        if not fix.is_absolute():
            fix = REPO / fix
        discover_fn = make_discover_fn_from_fixture(fix)
        print(f"[fixture] using offline discover from {fix}")

    fixture_mode = discover_fn is not None
    claim_only = bool(cmd.claim_only)
    # Fixture alone → stop after 01/02; with --claim-only → serial claim stubs (no 03+).
    dry_run = bool(cmd.dry_run) or (fixture_mode and not claim_only)

    paths, _ = load_pipeline_config()

    try:
        if claim_only:
            from citfix.claim_only import make_claim_pipeline_fn
            from citfix.project_batch import run_batch_intake, run_project_batch as _rpb

            state, batch_dir, _formal = run_batch_intake(
                alias,
                device_serial=cmd.device_serial,
                discover_fn=discover_fn,
            )
            if not state.queue:
                _print_batch(state, batch_dir)
                print("\n[claim-only] queue empty — nothing to intervene.")
                return 0
            claim_fn = make_claim_pipeline_fn(
                paths,
                product_name=state.product_name,
                project_alias=state.project_alias,
                batch_id=state.batch_id,
            )
            print(
                f"[claim-only] serial intervene {len(state.queue)} bugs "
                f"(no stages 03+); batch_id={state.batch_id}"
            )
            state, batch_dir = _rpb(
                alias,
                device_serial=cmd.device_serial,
                dry_run=False,
                resume=True,
                force_new=False,
                run_pipeline_fn=claim_fn,
            )
        else:
            state, batch_dir = run_project_batch(
                alias,
                device_serial=cmd.device_serial,
                status_only=cmd.status and not fixture_mode and not dry_run,
                dry_run=dry_run,
                resume=(cmd.resume and not cmd.new and not fixture_mode),
                force_new=cmd.new or fixture_mode,
                discover_fn=discover_fn,
            )
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    _print_batch(state, batch_dir)
    if cmd.debug or fixture_mode or claim_only:
        print("\n=== BATCH DEBUG ===")
        print(f"01 candidates: {batch_dir / '01_req_parse' / 'output' / 'candidate_rows.json'}")
        print(f"02 bug_ids:    {batch_dir / '02_bug_task_extract' / 'output' / 'bug_ids.json'}")
        print(f"batch_state:   {batch_dir / 'batch_state.json'}")
        if state.formal_run_dir:
            print(f"formal batch:  {state.formal_run_dir}")
        if claim_only:
            for item in state.queue:
                if item.run_id:
                    print(f"  claimed #{item.bug_id} → runs/.../{item.run_id}/claim.json")

    if claim_only:
        print(
            f"\n[claim-only] done — batch_status={state.batch_status}; "
            "each queue item has run_id + CLAIMED_FOR_DEBUG (03+ not run)."
        )
        return 0 if state.batch_status == "completed" else 2

    if cmd.status or dry_run or cmd.debug or (fixture_mode and not claim_only):
        if fixture_mode and not claim_only:
            print(
                "\n[fixture] 01/02 finished offline — child pipelines not started. "
                "Add --claim-only to serially intervene without 03+."
            )
        return 0

    if state.batch_status == "empty":
        print(
            "\nNo auto-eligible CIT bugs for this project under current user. "
            "Check assignedTo / CIT title / verify_mode=auto filters."
        )
        return 0

    if state.batch_status == "completed":
        print(f"\nOK — project batch completed ({len(state.queue)} bugs)")
        return 0

    if state.batch_status == "blocked":
        cur = state.queue[state.current_index] if state.queue else None
        print("\n=== PROJECT BATCH BLOCKED ===")
        if cur:
            print(f"bug_id: {cur.bug_id}  run_id: {cur.run_id}")
            print(f"reason: {cur.reason}")
            print(f"Resume bug:     /citfix {cur.bug_id} --resume")
            print(f"Resume batch:   /citfix project {alias} --resume")
            print(f"Debug bug:      /citfix {cur.bug_id} --debug")
        return 2

    print(f"\nBatch ended with status: {state.batch_status}")
    return 0


def _run_diff(cmd) -> int:
    from citfix.bug_diff import collect_bug_diffs, format_bug_diffs_text

    paths, _ = load_pipeline_config()
    ids = list(cmd.bug_ids) or ([cmd.bug_id] if cmd.bug_id else [])
    doc = collect_bug_diffs(paths, [str(x) for x in ids])
    if cmd.as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(format_bug_diffs_text(doc))
    return 0 if doc.get("ok_count") == doc.get("count") else 1


def _run_bugs_serial(cmd) -> int:
    """Serial multi-bug: /citfix 81097 97203 — one pipeline after another."""
    ids = [str(x) for x in (cmd.bug_ids or ())]
    if not ids:
        print("ERROR: no bug ids", file=sys.stderr)
        return 1
    print(f"[citfix] serial multi-bug: {', '.join(ids)} ({len(ids)} runs)")
    worst = 0
    for i, bid in enumerate(ids, 1):
        print(f"\n======== [{i}/{len(ids)}] /citfix {bid} ========")
        sub = CitfixCommand(
            kind="bug",
            bug_id=bid,
            bug_ids=(bid,),
            resume=cmd.resume,
            status=cmd.status,
            new=cmd.new,
            run_id=None,
            dry_run=cmd.dry_run,
            debug=cmd.debug,
            as_json=cmd.as_json,
            raw=f"/citfix {bid}",
        )
        if cmd.status or cmd.debug:
            rc = _run_bug(sub)
            worst = max(worst, rc)
            continue
        rc = _run_bug(sub)
        worst = max(worst, rc)
        if rc == 2:
            print(
                f"\n[citfix] blocked on #{bid} — remaining ids not started: "
                f"{ids[i:]}"
            )
            print(f"Resume: /citfix {bid} --resume")
            print(f"Or continue rest: /citfix {' '.join(ids[i:])}")
            return 2
        if rc != 0:
            print(f"\n[citfix] stop on #{bid} rc={rc}; remaining: {ids[i:]}")
            return rc
    print(f"\n[citfix] multi-bug finished ({len(ids)} ids), worst_rc={worst}")
    return worst


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        cmd = parse_citfix_tokens(argv)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        print(
            "\nExamples:\n"
            "  python scripts/citfix.py 81097\n"
            "  python scripts/citfix.py 81097 97203\n"
            "  python scripts/citfix.py diff 81097 97203\n"
            "  python scripts/citfix.py 81097 --debug\n"
            "  python scripts/citfix.py project slb783\n"
            "  python scripts/citfix.py project slb783 --device SERIAL --dry-run\n"
            "  python scripts/citfix.py project slb783 --fixture fixtures/project_discover",
            file=sys.stderr,
        )
        return 1

    if cmd.kind == "project":
        return _run_project(cmd)
    if cmd.kind == "diff":
        return _run_diff(cmd)
    if cmd.kind == "bugs":
        return _run_bugs_serial(cmd)
    return _run_bug(cmd)


if __name__ == "__main__":
    sys.exit(main())
