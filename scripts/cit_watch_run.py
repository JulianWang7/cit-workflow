#!/usr/bin/env python3
"""Long-running citfix watch via APScheduler (EXP-CIT-010 supervisor).

Usage:
  pip install -r requirements-watch.txt
  python scripts/cit_watch_run.py
  python scripts/cit_watch_run.py --config config/citfix/watch.yaml

Default config has nudge_dry_run=true (log CLI only). Set nudge_dry_run: false
in config/citfix/watch.yaml to actually invoke Cursor CLI.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = str(Path(__file__).resolve().parent)
while sys.path and sys.path[0] in ("", ".", _SCRIPTS):
    sys.path.pop(0)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from citfix.watch.config import load_watch_config  # noqa: E402
from citfix.watch.scheduler import create_background_scheduler  # noqa: E402
from citfix.watch.service import run_watch_cycle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="citfix watch — APScheduler daemon")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--once-first", action="store_true", help="run one cycle before starting scheduler")
    args = p.parse_args(argv)

    cfg = load_watch_config(args.config)
    print(
        f"[citfix.watch] workspace={cfg.workspace} "
        f"dry_run={cfg.nudge_dry_run} "
        f"scan_runs={cfg.scan_runs_seconds}s"
    )
    if args.once_first:
        summary = run_watch_cycle(cfg)
        print(
            f"[citfix.watch] first cycle: runs={summary['runs_scanned']} "
            f"nudges={summary['nudges']} escalations={summary['escalations']}"
        )

    try:
        sched = create_background_scheduler(cfg)
    except ImportError as e:
        print(str(e), file=sys.stderr)
        return 1

    stop = {"flag": False}

    def _stop(*_args: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    sched.start()
    print("[citfix.watch] scheduler started — Ctrl+C to stop")
    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        print("[citfix.watch] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
