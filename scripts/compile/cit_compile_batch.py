#!/usr/bin/env python3
"""Batch compile planner for multiple bugs/devices (queue shell; sequential by default)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ROUTER = ROOT / "scripts" / "compile" / "cit_compile_router.py"
RUNNER = ROOT / "scripts" / "compile" / "cit_compile_run.py"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="CIT compile batch")
    ap.add_argument("--batch", type=Path, required=True, help="job_batch.json")
    ap.add_argument("--route-only", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--promote", action="store_true")
    args = ap.parse_args()

    batch = _load(args.batch)
    results = []
    # Device exclusive: group by device_serial, process one at a time per device
    jobs = sorted(batch.get("jobs") or [], key=lambda j: (j.get("priority", 100), j.get("bug_id")))
    for job in jobs:
        req = Path(job["request_ref"])
        case_dir = req.parent
        plan_path = case_dir / "compile_plan.json"
        cmd = [
            sys.executable,
            str(ROUTER),
            "--request",
            str(req),
            "--out",
            str(plan_path),
        ]
        rc = subprocess.call(cmd)
        entry = {"bug_id": job.get("bug_id"), "route_rc": rc, "plan": str(plan_path)}
        if rc == 0 and not args.route_only:
            rcmd = [
                sys.executable,
                str(RUNNER),
                "--plan",
                str(plan_path),
                "--case-dir",
                str(case_dir),
            ]
            if args.execute:
                rcmd.append("--execute")
            if args.promote:
                rcmd.append("--promote")
            entry["run_rc"] = subprocess.call(rcmd)
        results.append(entry)

    out = {
        "batch_id": batch.get("batch_id"),
        "finished_at": _now(),
        "results": results,
    }
    out_path = args.batch.parent / "batch_result.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if all(r.get("route_rc", 1) == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
