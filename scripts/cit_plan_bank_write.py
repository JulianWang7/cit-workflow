#!/usr/bin/env python3
"""Write / upsert plan_bank JSON from a cit-prepare run snapshot.

Directory layout (token-friendly for agents):
  plan_bank/<PROJECT>/
    project_info.json          # env binding (read once per project)
    index.json                 # thin catalog — agent reads this first
    problems/<zentao_id>.json  # one problem per file
    history/plan_YYYYMMDD.json # optional daily archive snapshot

Usage:
  python scripts/cit_plan_bank_write.py --run-dir runs/<PRODUCT>/CIT-20260901-81097
  python scripts/cit_plan_bank_write.py --migrate-legacy
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PLAN_BANK = REPO / "plan_bank"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be object")
    return data


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _project_name(task: dict, context: dict, workspace: dict) -> str:
    source = context.get("source") if isinstance(context.get("source"), dict) else {}
    name = (
        task.get("product")
        or source.get("project")
        or workspace.get("project")
        or "UNKNOWN"
    )
    return str(name).strip() or "UNKNOWN"


def _plan_date_from_run(run_dir: Path, run_id: str, override: str | None) -> str:
    if override:
        return override
    m = re.search(r"(20\d{6})", run_id or "")
    if m:
        return m.group(1)
    m = re.search(r"(20\d{6})", run_dir.name)
    if m:
        return m.group(1)
    return datetime.now().strftime("%Y%m%d")


def _default_branches(workspace: dict, context: dict) -> list[dict[str, str]]:
    """Skeleton branch entries from known paths; branch/head filled later via SSH."""
    source = context.get("source") if isinstance(context.get("source"), dict) else {}
    rows: list[dict[str, str]] = []
    code_root = str(workspace.get("code_root") or source.get("code_root") or "")
    cit_path = str(
        workspace.get("cit_source_path")
        or source.get("cit_source_path")
        or workspace.get("meiglink_root")
        or ""
    )
    if code_root:
        rows.append({"tree": "code", "path": code_root, "branch": "", "head": ""})
    if cit_path:
        rows.append({"tree": "cit", "path": cit_path, "branch": "", "head": ""})
    return rows


def _merge_branches(
    existing: list[Any], incoming: list[dict[str, str]]
) -> list[dict[str, str]]:
    by_path: dict[str, dict[str, str]] = {}
    for item in existing + incoming:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        prev = by_path.get(path, {})
        by_path[path] = {
            "tree": str(item.get("tree") or prev.get("tree") or ""),
            "path": path,
            "branch": str(item.get("branch") or prev.get("branch") or ""),
            "head": str(item.get("head") or prev.get("head") or ""),
        }
    return list(by_path.values())


def _problem_rel(zentao_id: str) -> str:
    return f"problems/{zentao_id}.json"


def _index_entry(problem: dict[str, Any]) -> dict[str, str]:
    zid = str(problem.get("zentao_id") or "")
    return {
        "zentao_id": zid,
        "status": str(problem.get("status") or ""),
        "description": str(problem.get("description") or "")[:120],
        "path": _problem_rel(zid),
        "updated": str(problem.get("updated") or ""),
    }


def rebuild_index(plan_root: Path, project_name: str) -> Path:
    """Scan problems/*.json and write a thin index.json for agents."""
    problems_dir = plan_root / "problems"
    entries: list[dict[str, str]] = []
    if problems_dir.is_dir():
        for path in sorted(problems_dir.glob("*.json")):
            try:
                data = _load(path)
            except Exception:
                continue
            if not data.get("zentao_id"):
                data["zentao_id"] = path.stem
            entries.append(_index_entry(data))
    index = {
        "schema_version": "1.0",
        "project_name": project_name,
        "updated": _now_iso(),
        "problems": entries,
    }
    index_path = plan_root / "index.json"
    _dump(index_path, index)
    return index_path


def upsert_problem(
    plan_root: Path,
    *,
    description: str,
    zentao_id: str,
    project_name: str,
    status: str,
) -> Path:
    if not zentao_id:
        raise ValueError("zentao_id required for problems/<id>.json")
    path = plan_root / "problems" / f"{zentao_id}.json"
    if path.is_file():
        data = _load(path)
        prev_path = str(data.get("analysis_conclusion_path") or "")
    else:
        data = {"schema_version": "1.0"}
        prev_path = ""

    data.update(
        {
            "schema_version": data.get("schema_version") or "1.0",
            "description": description,
            "zentao_id": str(zentao_id),
            "project_name": project_name,
            "status": status,
            "analysis_conclusion_path": prev_path,
            "updated": _now_iso(),
        }
    )
    _dump(path, data)
    return path


def archive_history_snapshot(
    plan_root: Path,
    date: str,
    *,
    problem: dict[str, Any] | None = None,
) -> Path:
    """Append/replace a daily history file (full problem payloads for that day)."""
    hist_path = plan_root / "history" / f"plan_{date}.json"
    if hist_path.is_file():
        data = _load(hist_path)
    else:
        data = {"schema_version": "1.0", "plan_date": date, "problems": []}

    problems = data.get("problems")
    if not isinstance(problems, list):
        problems = []

    if problem is not None:
        zid = str(problem.get("zentao_id") or "")
        replaced = False
        for i, old in enumerate(problems):
            if isinstance(old, dict) and str(old.get("zentao_id")) == zid:
                # Prefer keeping filled analysis path
                merged = dict(old)
                merged.update(problem)
                if not merged.get("analysis_conclusion_path"):
                    merged["analysis_conclusion_path"] = old.get(
                        "analysis_conclusion_path"
                    ) or ""
                problems[i] = merged
                replaced = True
                break
        if not replaced:
            problems.append(problem)

    data["schema_version"] = "1.0"
    data["plan_date"] = date
    data["problems"] = problems
    data["updated"] = _now_iso()
    _dump(hist_path, data)
    return hist_path


def upsert_project_info(
    info_path: Path,
    *,
    project_name: str,
    code_root: str,
    cit_source_root: str,
    device_serial: str,
    server: str,
    repo_branches: list[dict[str, str]],
) -> dict[str, Any]:
    if info_path.is_file():
        data = _load(info_path)
    else:
        data = {"schema_version": "1.0"}

    existing_branches = (
        data.get("repo_branches") if isinstance(data.get("repo_branches"), list) else []
    )
    data.update(
        {
            "schema_version": data.get("schema_version") or "1.0",
            "project_name": project_name,
            "code_root": code_root,
            "cit_source_root": cit_source_root,
            "device_serial": device_serial,
            "server": server,
            "repo_branches": _merge_branches(existing_branches, repo_branches),
        }
    )
    _dump(info_path, data)
    return data


def write_from_run(
    run_dir: Path,
    *,
    plan_date: str | None = None,
    branches_override: list[dict[str, str]] | None = None,
) -> dict[str, Path]:
    out = run_dir / "06_context_snapshot" / "output"
    if not out.is_dir():
        if (run_dir / "task_ref.json").is_file():
            out = run_dir
        else:
            raise FileNotFoundError(f"missing snapshot output under {run_dir}")

    task = _load(out / "task_ref.json")
    workspace = _load(out / "workspace.json")
    context = _load(out / "context.json")

    project = _project_name(task, context, workspace)
    run_id = str(context.get("run_id") or run_dir.name)
    date = _plan_date_from_run(run_dir, run_id, plan_date)

    plan_root = PLAN_BANK / project
    info_path = plan_root / "project_info.json"
    zentao_id = str(task.get("bug_id") or "")

    source = context.get("source") if isinstance(context.get("source"), dict) else {}
    cit_root = str(
        workspace.get("cit_source_path")
        or source.get("cit_source_path")
        or workspace.get("meiglink_root")
        or ""
    )
    code_root = str(workspace.get("code_root") or source.get("code_root") or "")
    device_serial = str(workspace.get("device_serial") or "")
    server = str(workspace.get("server") or "")

    branches = (
        branches_override
        if branches_override is not None
        else _default_branches(workspace, context)
    )

    problem_path = upsert_problem(
        plan_root,
        description=str(task.get("title") or ""),
        zentao_id=zentao_id,
        project_name=project,
        status=str(task.get("status") or ""),
    )
    problem_data = _load(problem_path)
    history_path = archive_history_snapshot(plan_root, date, problem=problem_data)
    index_path = rebuild_index(plan_root, project)
    upsert_project_info(
        info_path,
        project_name=project,
        code_root=code_root,
        cit_source_root=cit_root,
        device_serial=device_serial,
        server=server,
        repo_branches=branches,
    )
    return {
        "project_info": info_path,
        "problem": problem_path,
        "index": index_path,
        "history": history_path,
        # backward-compatible alias for stage outputs
        "plans": problem_path,
    }


def migrate_legacy_plan_bank() -> list[str]:
    """Convert plan_YYYYMMDD/plans.json → problems/ + index.json + history/."""
    log: list[str] = []
    if not PLAN_BANK.is_dir():
        return ["no plan_bank directory"]

    for project_dir in sorted(p for p in PLAN_BANK.iterdir() if p.is_dir()):
        project_name = project_dir.name
        legacy_dirs = sorted(project_dir.glob("plan_*"))
        if not legacy_dirs:
            # still ensure index exists if problems/ already present
            if (project_dir / "problems").is_dir():
                rebuild_index(project_dir, project_name)
                log.append(f"{project_name}: rebuilt index only")
            continue

        # Process in date order so later entries overwrite earlier for same id
        # but analysis_conclusion_path is preserved when non-empty.
        for legacy in legacy_dirs:
            plans_file = legacy / "plans.json"
            if not plans_file.is_file():
                continue
            raw = _load(plans_file)
            m = re.search(r"plan_(20\d{6})", legacy.name)
            date = m.group(1) if m else datetime.now().strftime("%Y%m%d")

            # Archive full legacy payload
            hist = project_dir / "history" / f"plan_{date}.json"
            hist_body = {
                "schema_version": "1.0",
                "plan_date": date,
                "problems": raw.get("problems") if isinstance(raw.get("problems"), list) else [],
                "migrated_from": str(plans_file.as_posix()),
                "updated": _now_iso(),
            }
            _dump(hist, hist_body)

            for item in hist_body["problems"]:
                if not isinstance(item, dict):
                    continue
                zid = str(item.get("zentao_id") or "").strip()
                if not zid:
                    continue
                path = project_dir / "problems" / f"{zid}.json"
                if path.is_file():
                    existing = _load(path)
                    merged = dict(existing)
                    merged.update(
                        {
                            "description": item.get("description")
                            or existing.get("description")
                            or "",
                            "zentao_id": zid,
                            "project_name": item.get("project_name")
                            or existing.get("project_name")
                            or project_name,
                            "status": item.get("status") or existing.get("status") or "",
                            "updated": _now_iso(),
                        }
                    )
                    # Prefer non-empty analysis path
                    new_ac = str(item.get("analysis_conclusion_path") or "")
                    old_ac = str(existing.get("analysis_conclusion_path") or "")
                    merged["analysis_conclusion_path"] = new_ac or old_ac
                    _dump(path, merged)
                else:
                    payload = {
                        "schema_version": "1.0",
                        "description": str(item.get("description") or ""),
                        "zentao_id": zid,
                        "project_name": str(
                            item.get("project_name") or project_name
                        ),
                        "status": str(item.get("status") or ""),
                        "analysis_conclusion_path": str(
                            item.get("analysis_conclusion_path") or ""
                        ),
                        "updated": _now_iso(),
                    }
                    _dump(path, payload)

            shutil.rmtree(legacy)
            log.append(f"{project_name}: migrated {legacy.name} → history/plan_{date}.json")

        rebuild_index(project_dir, project_name)
        log.append(f"{project_name}: index.json written")

    return log


def main() -> int:
    parser = argparse.ArgumentParser(description="Write cit-workflow plan_bank JSON")
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="cit-workflow/runs/<run_id> or runs_work/.../runs/<run_id> or .../06_context_snapshot/output",
    )
    parser.add_argument("--plan-date", default=None, help="YYYYMMDD for history/plan_<date>.json")
    parser.add_argument(
        "--branches-json",
        default=None,
        help='Optional JSON array: [{"tree","path","branch","head"}, ...]',
    )
    parser.add_argument(
        "--migrate-legacy",
        action="store_true",
        help="Convert plan_YYYYMMDD/plans.json layout to problems/+index.json+history/",
    )
    args = parser.parse_args()

    if args.migrate_legacy:
        for line in migrate_legacy_plan_bank():
            print(line)
        return 0

    if not args.run_dir:
        parser.error("--run-dir is required unless --migrate-legacy")

    branches = None
    if args.branches_json:
        raw = json.loads(args.branches_json)
        if not isinstance(raw, list):
            print("--branches-json must be a JSON array", file=sys.stderr)
            return 2
        branches = []
        for row in raw:
            if isinstance(row, dict):
                branches.append(
                    {
                        "tree": str(row.get("tree") or ""),
                        "path": str(row.get("path") or ""),
                        "branch": str(row.get("branch") or ""),
                        "head": str(row.get("head") or ""),
                    }
                )

    try:
        written = write_from_run(
            args.run_dir, plan_date=args.plan_date, branches_override=branches
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"project_info: {written['project_info']}")
    print(f"problem:      {written['problem']}")
    print(f"index:        {written['index']}")
    print(f"history:      {written['history']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
