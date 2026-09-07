#!/usr/bin/env python3
"""Optional one-shot: copy legacy intermediate logs/watch into formal runs/.

Does **not** delete runs_work. Safe to re-run (skips existing files unless --force).

Example::

  python scripts/cit_migrate_logs.py --dry-run
  python scripts/cit_migrate_logs.py
  python scripts/cit_migrate_logs.py --run-id CIT-20260903-81097
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from citfix.paths import load_pipeline_config  # noqa: E402


def _copy_tree(src: Path, dst: Path, *, force: bool, dry_run: bool) -> int:
    if not src.is_dir():
        return 0
    n = 0
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if target.exists() and not force:
            continue
        if dry_run:
            print(f"DRY  {item} -> {target}")
            n += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        print(f"OK   {item} -> {target}")
        n += 1
    return n


def migrate_one(
    inter_run: Path,
    formal_run: Path,
    *,
    force: bool,
    dry_run: bool,
) -> int:
    n = 0
    # Legacy per-stage logs: <stage>/logs/*.log → formal logs/stages/
    for stage_dir in inter_run.iterdir():
        if not stage_dir.is_dir():
            continue
        legacy = stage_dir / "logs"
        if legacy.is_dir():
            n += _copy_tree(legacy, formal_run / "logs" / "stages", force=force, dry_run=dry_run)
    # Intermediate run-level logs/ (if any)
    if (inter_run / "logs").is_dir():
        n += _copy_tree(inter_run / "logs", formal_run / "logs", force=force, dry_run=dry_run)
    # watch/
    if (inter_run / "watch").is_dir():
        n += _copy_tree(inter_run / "watch", formal_run / "watch", force=force, dry_run=dry_run)
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Migrate runs_work logs/watch → formal runs/")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Overwrite existing formal files")
    p.add_argument("--run-id", default="", help="Only this run_id")
    args = p.parse_args(argv)

    paths, _ = load_pipeline_config()
    projects = paths.projects_root()
    if not projects.is_dir():
        print(f"No projects root: {projects}", file=sys.stderr)
        return 1

    total = 0
    for proj in projects.iterdir():
        runs = proj / "runs"
        if not runs.is_dir():
            continue
        for inter_run in runs.iterdir():
            if not inter_run.is_dir():
                continue
            rid = inter_run.name
            if args.run_id and rid != args.run_id:
                continue
            formal = paths.find_formal_run_dir(rid)
            if formal is None:
                print(f"SKIP {rid}: no formal run dir")
                continue
            print(f"--- {rid} ---")
            total += migrate_one(inter_run, formal, force=args.force, dry_run=args.dry_run)

    print(f"Done. files={'planned' if args.dry_run else 'copied'}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
