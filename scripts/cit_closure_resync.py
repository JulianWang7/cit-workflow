#!/usr/bin/env python3
"""Re-sync 14 closure artifacts from authoritative closure_<id>.json.

Use after Worker re-commit so report/outbox match the latest SHA/message.

  python scripts/cit_closure_resync.py --run-dir "runs/SLB783 - Android14/CIT-20260903-81097" --bug-id 81097
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = str(Path(__file__).resolve().parent)
while sys.path and sys.path[0] in ("", ".", _SCRIPTS):
    sys.path.pop(0)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from citfix.closure_ops import resync_closure_artifacts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Resync closure report/outbox from closure_*.json")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--bug-id", required=True)
    ap.add_argument("--no-drain", action="store_true")
    args = ap.parse_args()
    run_dir = args.run_dir
    if not run_dir.is_absolute():
        run_dir = REPO / run_dir
    if not run_dir.is_dir():
        print(f"ERROR: run-dir not found: {run_dir}", file=sys.stderr)
        return 2
    summary = resync_closure_artifacts(
        run_dir,
        str(args.bug_id),
        repo_root=REPO,
        drain=not args.no_drain,
        force_redeliver=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # Verify report matches closure
    closure = json.loads(
        (run_dir / "14_closure" / "output" / f"closure_{args.bug_id}.json").read_text(
            encoding="utf-8"
        )
    )
    report = json.loads(
        (run_dir / "14_closure" / "output" / "closure_report.json").read_text(encoding="utf-8")
    )
    ok = report.get("commit") == closure.get("commit") and report.get("message") == closure.get(
        "message"
    )
    print("VERIFY:", "PASS" if ok else "FAIL — report still diverges from closure_*.json")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
