#!/usr/bin/env python3
"""Single watch cycle (no APScheduler). Safe default: nudge_dry_run=true.

Usage:
  python scripts/cit_watch_once.py
  python scripts/cit_watch_once.py --config config/citfix/watch.yaml
  python scripts/cit_watch_once.py --json
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

from citfix.watch.config import load_watch_config  # noqa: E402
from citfix.watch.service import run_watch_cycle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="citfix watch — one scan cycle")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--json", action="store_true", help="print full JSON summary")
    args = p.parse_args(argv)
    cfg = load_watch_config(args.config)
    summary = run_watch_cycle(cfg)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"watch once: runs={summary['runs_scanned']} batches={summary['batches_scanned']} "
            f"nudges={summary['nudges']} escalations={summary['escalations']} "
            f"dry_run={summary['nudge_dry_run']}"
        )
        for r in summary.get("runs") or []:
            if r.get("decision") in ("nudge", "escalate", "retry_wait"):
                print(
                    f"  run {r.get('run_id')} bug={r.get('bug_id')} "
                    f"→ {r.get('decision')} [{r.get('error_class')}] {r.get('reason')}"
                )
        for b in summary.get("batches") or []:
            if b.get("decision") != "ok":
                print(
                    f"  batch {b.get('batch_id')} → {b.get('decision')} "
                    f"bug={b.get('bug_id')} {b.get('reason')}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
