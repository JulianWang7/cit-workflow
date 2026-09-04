#!/usr/bin/env python3
"""Approve AI human-case proposals into config/citfix/human_case_registry/.

Usage:
  python scripts/cit_human_case_approve.py --product "SLB783 - Android14" --from-proposal <path>
  python scripts/cit_human_case_approve.py --product "SLB783 - Android14" --case-json '{"case_id":"...","match":{...},"execution":"human"}'
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from citfix.human_cases import VERIFY_MODES, load_registry, registry_path  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description="Approve human-case registry entries")
    ap.add_argument("--product", required=True)
    ap.add_argument("--from-proposal", type=Path, help="path to human_case_proposal.json")
    ap.add_argument("--case-json", help="single case object JSON string")
    ap.add_argument("--updated-by", default="operator")
    args = ap.parse_args()

    cases_in: list[dict] = []
    if args.from_proposal:
        raw = json.loads(args.from_proposal.read_text(encoding="utf-8"))
        proposed = raw.get("proposed_cases") or raw.get("cases") or []
        if isinstance(proposed, dict):
            proposed = [proposed]
        cases_in.extend(c for c in proposed if isinstance(c, dict))
    if args.case_json:
        c = json.loads(args.case_json)
        if not isinstance(c, dict):
            print("case-json must be object", file=sys.stderr)
            return 2
        cases_in.append(c)

    if not cases_in:
        print("No cases to approve", file=sys.stderr)
        return 2

    reg = load_registry(REPO, args.product)
    existing = {str(c.get("case_id")): c for c in (reg.get("cases") or []) if isinstance(c, dict)}
    for c in cases_in:
        cid = str(c.get("case_id") or "").strip()
        ex = str(c.get("execution") or "").lower()
        if not cid:
            print("skip case without case_id", file=sys.stderr)
            continue
        if ex not in VERIFY_MODES and ex != "hybrid":
            # execution on case is auto|human|hybrid
            if ex not in ("auto", "human", "hybrid"):
                print(f"skip {cid}: bad execution={ex}", file=sys.stderr)
                continue
        c = dict(c)
        c["updated_by"] = args.updated_by
        c["updated_at"] = _now()
        existing[cid] = c

    reg["product"] = args.product
    reg["schema_version"] = "1.0"
    reg["cases"] = list(existing.values())
    out = registry_path(REPO, args.product)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(reg['cases'])} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
