#!/usr/bin/env python3
"""Show commit/diff summary for one or more CIT bug ids.

  python scripts/cit_bug_diff.py 81097
  python scripts/cit_bug_diff.py 81097 97203 --json
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

from citfix.bug_diff import collect_bug_diffs, format_bug_diffs_text  # noqa: E402
from citfix.paths import load_pipeline_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CIT bug commit/diff lookup")
    ap.add_argument("bug_ids", nargs="+", help="one or more bug ids")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)
    for bid in args.bug_ids:
        if not str(bid).isdigit():
            print(f"ERROR: not a bug id: {bid}", file=sys.stderr)
            return 2
    paths, _ = load_pipeline_config()
    doc = collect_bug_diffs(paths, [str(b) for b in args.bug_ids])
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(format_bug_diffs_text(doc))
    return 0 if doc.get("ok_count") == doc.get("count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
