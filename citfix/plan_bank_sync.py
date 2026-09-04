"""Update plan_bank analysis_conclusion_path after 07_analysis succeeds."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from citfix.artifact_paths import (
    expected_root_cause_rel,
    to_posix_rel,
    validate_root_cause_rel,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def sync_analysis_conclusion_path(
    repo_root: Path,
    *,
    product: str,
    bug_id: str,
    run_id: str,
    root_cause_path: Path,
) -> tuple[str, list[str]]:
    """Write formal relative path into plan_bank; return (rel_path, errors).

    Does not delete run artifacts. On validation failure returns errors and
    still does not silently continue with a stale path.
    """
    rel = expected_root_cause_rel(product, run_id, bug_id)
    errs = validate_root_cause_rel(
        rel, bug_id=bug_id, product=product, run_id=run_id
    )
    # Also require the file to exist at formal location
    formal = repo_root / to_posix_rel(rel)
    if not formal.is_file():
        # allow root_cause_path if it resolves to same file
        try:
            if root_cause_path.resolve() != formal.resolve():
                errs.append(f"root_cause file missing at formal path: {rel}")
        except OSError:
            errs.append(f"root_cause file missing at formal path: {rel}")

    if errs:
        return rel, errs

    plan_root = repo_root / "plan_bank" / product
    plan_root.mkdir(parents=True, exist_ok=True)
    (plan_root / "problems").mkdir(parents=True, exist_ok=True)
    problem_path = plan_root / "problems" / f"{bug_id}.json"
    data: dict[str, Any] = {"schema_version": "1.0"}
    if problem_path.is_file():
        try:
            raw = json.loads(problem_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except json.JSONDecodeError:
            pass

    old = to_posix_rel(str(data.get("analysis_conclusion_path") or ""))
    if old and old != rel:
        old_errs = validate_root_cause_rel(old, bug_id=bug_id)
        data["prior_analysis_conclusion_path"] = old
        data["prior_path_invalid"] = bool(old_errs)

    data["schema_version"] = data.get("schema_version") or "1.0"
    data["zentao_id"] = str(bug_id)
    data["project_name"] = product
    data["analysis_conclusion_path"] = rel
    data["updated"] = _now_iso()
    problem_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # Refresh index entry if present
    index_path = plan_root / "index.json"
    if index_path.is_file():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            idx = {}
        problems = idx.get("problems") if isinstance(idx, dict) else None
        if isinstance(problems, list):
            found = False
            for p in problems:
                if isinstance(p, dict) and str(p.get("zentao_id")) == str(bug_id):
                    p["analysis_conclusion_path"] = rel
                    p["updated"] = data["updated"]
                    found = True
                    break
            if not found:
                problems.append(
                    {
                        "zentao_id": str(bug_id),
                        "path": f"problems/{bug_id}.json",
                        "analysis_conclusion_path": rel,
                        "updated": data["updated"],
                    }
                )
            idx["problems"] = problems
            idx["updated"] = data["updated"]
            index_path.write_text(
                json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    return rel, []
