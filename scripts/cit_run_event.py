#!/usr/bin/env python3
"""Append one CitFix run event to formal runs/<PRODUCT>/<run_id>/logs/run_events.jsonl.

Agent milestones (LOG-07)::

  python scripts/cit_run_event.py --run-dir <formal> --bug-id 81097 \\
    --stage 07_analysis --event agent_stage_enter --summary "start analyze"

  python scripts/cit_run_event.py --run-dir <formal> --bug-id 81097 \\
    --stage 07_analysis --event agent_checkpoint --level warn \\
    --summary "blocked: need more logs"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from citfix.run_log import ACTORS, LEVELS, emit_event, run_events_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Append a citfix run_events.jsonl row")
    p.add_argument(
        "--run-dir",
        required=True,
        help="Formal run dir: runs/<PRODUCT>/<run_id>",
    )
    p.add_argument("--run-id", default="", help="Defaults to run-dir name")
    p.add_argument("--bug-id", default="")
    p.add_argument("--stage", default="", dest="stage_id")
    p.add_argument(
        "--actor",
        default="agent",
        choices=sorted(ACTORS),
    )
    p.add_argument(
        "--event",
        required=True,
        help="e.g. agent_stage_enter | agent_gate_pass | agent_gate_fail | agent_checkpoint",
    )
    p.add_argument("--summary", required=True)
    p.add_argument("--level", default="info", choices=sorted(LEVELS))
    p.add_argument(
        "--refs-json",
        default="",
        help='Optional JSON object string for refs, e.g. \'{"path":"..."}\'',
    )
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (_REPO / run_dir).resolve()
    refs: dict = {}
    if args.refs_json.strip():
        refs = json.loads(args.refs_json)
        if not isinstance(refs, dict):
            print("--refs-json must be a JSON object", file=sys.stderr)
            return 2

    path = emit_event(
        run_dir,
        actor=args.actor,
        event=args.event,
        summary=args.summary,
        run_id=args.run_id or run_dir.name,
        bug_id=str(args.bug_id),
        stage_id=str(args.stage_id),
        level=args.level,
        refs=refs,
        raise_on_error=True,
    )
    print(path or run_events_path(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
