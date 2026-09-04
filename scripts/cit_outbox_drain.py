#!/usr/bin/env python3
"""Drain citfix outbox_events.jsonl to configured channels.

  python scripts/cit_outbox_drain.py --run-dir runs/<PRODUCT>/CIT-...
  python scripts/cit_outbox_drain.py --run-dir ... --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from citfix.outbox import drain_outbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Drain citfix outbox to channels")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--force", action="store_true", help="re-deliver even if dedupe_key seen")
    args = ap.parse_args()
    if not args.run_dir.is_dir():
        print(f"ERROR: run-dir not found: {args.run_dir}", file=sys.stderr)
        return 2
    summary = drain_outbox(args.run_dir, REPO, force=bool(args.force))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
