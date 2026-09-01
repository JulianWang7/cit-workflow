#!/usr/bin/env python3
"""Write / upsert plan_bank JSON from a cit-prepare run snapshot.

Directory layout (English keys):
  plan_bank/<PROJECT>/
    project_info.json
    plan_<YYYYMMDD>/
      plans.json

Usage:
  python scripts/cit_plan_bank_write.py --run-dir runs/CIT-20260901-97203
  python scripts/cit_plan_bank_write.py --run-dir runs/CIT-20260901-97203 --plan-date 20260901
  python scripts/cit_plan_bank_write.py --run-dir ... --branches-json '[{"tree":"vendor",...}]'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be object")
    return data


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        rows.append(
            {
                "tree": "code",
                "path": code_root,
                "branch": "",
                "head": "",
            }
        )
    if cit_path:
        rows.append(
            {
                "tree": "cit",
                "path": cit_path,
                "branch": "",
                "head": "",
            }
        )
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


def upsert_plans(
    plans_path: Path,
    *,
    description: str,
    zentao_id: str,
    project_name: str,
    status: str,
) -> dict[str, Any]:
    if plans_path.is_file():
        data = _load(plans_path)
    else:
        data = {"schema_version": "1.0", "problems": []}

    problems = data.get("problems")
    if not isinstance(problems, list):
        problems = []

    item = {
        "description": description,
        "zentao_id": str(zentao_id),
        "project_name": project_name,
        "status": status,
        "analysis_conclusion_path": "",
    }

    replaced = False
    for i, old in enumerate(problems):
        if isinstance(old, dict) and str(old.get("zentao_id")) == str(zentao_id):
            # Keep existing analysis path if already filled
            prev_path = old.get("analysis_conclusion_path") or ""
            item["analysis_conclusion_path"] = str(prev_path)
            problems[i] = item
            replaced = True
            break
    if not replaced:
        problems.append(item)

    data["schema_version"] = data.get("schema_version") or "1.0"
    data["problems"] = problems
    _dump(plans_path, data)
    return data


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

    existing_branches = data.get("repo_branches") if isinstance(data.get("repo_branches"), list) else []
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
        # allow passing output dir directly
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

    plan_root = REPO / "plan_bank" / project
    plans_path = plan_root / f"plan_{date}" / "plans.json"
    info_path = plan_root / "project_info.json"

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

    branches = branches_override if branches_override is not None else _default_branches(workspace, context)

    upsert_plans(
        plans_path,
        description=str(task.get("title") or ""),
        zentao_id=str(task.get("bug_id") or ""),
        project_name=project,
        status=str(task.get("status") or ""),
    )
    upsert_project_info(
        info_path,
        project_name=project,
        code_root=code_root,
        cit_source_root=cit_root,
        device_serial=device_serial,
        server=server,
        repo_branches=branches,
    )
    return {"plans": plans_path, "project_info": info_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write cit-workflow plan_bank JSON")
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="runs/<run_id> or .../06_context_snapshot/output",
    )
    parser.add_argument("--plan-date", default=None, help="YYYYMMDD for plan_<date> folder")
    parser.add_argument(
        "--branches-json",
        default=None,
        help='Optional JSON array: [{"tree","path","branch","head"}, ...]',
    )
    args = parser.parse_args()

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
        written = write_from_run(args.run_dir, plan_date=args.plan_date, branches_override=branches)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"project_info: {written['project_info']}")
    print(f"plans:        {written['plans']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
