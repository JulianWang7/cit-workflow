#!/usr/bin/env python3
"""CLI for /citfix resumable CIT workflow.

Usage:
  python scripts/citfix.py 97203
  python scripts/citfix.py 97203 --resume
  python scripts/citfix.py 97203 --resume --run-id CIT-20260901-97203
  python scripts/citfix.py 97203 --status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from citfix import find_run_for_bug, load_state, run_pipeline  # noqa: E402
from citfix.paths import load_pipeline_config  # noqa: E402


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
    run_dir = load_pipeline_config()[0].test_bed_root / "00_runs" / state.run_id
    print(f"Checkpoint file: {run_dir / 'CHECKPOINT.md'}")


def _print_status(state) -> None:
    print(f"run_id: {state.run_id}  bug_id: {state.bug_id}  status: {state.workflow_status}")
    print(f"current_stage: {state.current_stage}")
    for sid, rec in state.stages.items():
        print(f"  {sid}: {rec.status.value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CIT /citfix workflow")
    parser.add_argument("bug_id", help="Zentao bug id, e.g. 97203")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    parser.add_argument("--run-id", default=None, help="Specific run_id")
    parser.add_argument("--status", action="store_true", help="Show workflow status only")
    parser.add_argument("--new", action="store_true", help="Force new run (ignore existing checkpoint)")
    args = parser.parse_args()

    paths, _ = load_pipeline_config()

    if args.status:
        run_dir = None
        if args.run_id:
            run_dir = paths.test_bed_root / "00_runs" / args.run_id
        else:
            run_dir = find_run_for_bug(paths, args.bug_id)
        if not run_dir:
            print(f"No workflow run found for bug {args.bug_id}", file=sys.stderr)
            return 1
        state = load_state(run_dir)
        if not state:
            print("workflow_state.json missing", file=sys.stderr)
            return 1
        _print_status(state)
        return 0

    state = run_pipeline(
        args.bug_id,
        resume=args.resume or not args.new,
        run_id=args.run_id,
        force_new=args.new,
    )

    if state.workflow_status == "completed":
        print(f"OK — all stages completed for bug {args.bug_id} (run_id={state.run_id})")
        return 0

    if state.workflow_status == "blocked":
        _print_blocker(state)
        return 2

    print(f"Workflow ended with status: {state.workflow_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
