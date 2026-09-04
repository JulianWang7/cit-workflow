"""Lookup commit / change diff summaries for one or more bug ids.

Reuses formal run artifacts (08 change_*.json, 14 closure_*.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from citfix.engine import find_run_for_bug
from citfix.paths import PipelinePaths, read_product_from_run, to_repo_relative


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _formal_from_inter(paths: PipelinePaths, inter: Path) -> Path | None:
    # Prefer sibling formal under runs/<product>/<run_id>
    rid = inter.name
    found = paths.find_formal_run_dir(rid)
    if found:
        return found
    prod = read_product_from_run(inter)
    if prod:
        cand = paths.formal_run_dir(rid, prod)
        if cand.is_dir():
            return cand
    return None


def collect_bug_diff(paths: PipelinePaths, bug_id: str) -> dict[str, Any]:
    """Build a diff summary for one bug from the latest run artifacts."""
    inter = find_run_for_bug(paths, bug_id)
    if not inter:
        # also scan formal runs/*/CIT-*-{bug}
        formal_hit: Path | None = None
        runs = paths.repo_root / "runs"
        if runs.is_dir():
            for p in runs.glob(f"*/CIT-*-{bug_id}"):
                if p.is_dir():
                    formal_hit = p
                    break
        if not formal_hit:
            return {
                "bug_id": bug_id,
                "ok": False,
                "error": f"no run found for bug {bug_id}",
            }
        formal = formal_hit
    else:
        formal = _formal_from_inter(paths, inter) or inter

    product = read_product_from_run(formal) or ""
    change = _load_json(formal / "08_changes" / "output" / f"change_{bug_id}.json")
    closure = _load_json(formal / "14_closure" / "output" / f"closure_{bug_id}.json")
    report = _load_json(formal / "14_closure" / "output" / "closure_report.json")
    root_cause = _load_json(formal / "07_analysis" / "output" / f"root_cause_{bug_id}.json")

    commit = (
        str(closure.get("commit") or "").strip()
        or str(report.get("commit") or "").strip()
        or str(change.get("commit") or "").strip()
    )
    message = (
        str(closure.get("message") or "").strip()
        or str(report.get("message") or "").strip()
        or ""
    )
    files = change.get("files_changed") if isinstance(change.get("files_changed"), list) else []
    patch_summary = str(change.get("patch_summary") or "").strip()
    prior = change.get("prior_commits") if isinstance(change.get("prior_commits"), list) else []

    gerrit = closure.get("gerrit_url") or report.get("gerrit_url")
    code_root = str(closure.get("code_root") or change.get("code_root") or "")
    server = str(closure.get("server") or change.get("server") or "")

    links: dict[str, str] = {
        "formal_run": to_repo_relative(paths.repo_root, formal),
        "change_json": to_repo_relative(
            paths.repo_root, formal / "08_changes" / "output" / f"change_{bug_id}.json"
        ),
        "closure_json": to_repo_relative(
            paths.repo_root, formal / "14_closure" / "output" / f"closure_{bug_id}.json"
        ),
    }
    if gerrit:
        links["gerrit"] = str(gerrit)
    if commit and code_root:
        links["remote_hint"] = (
            f"ssh:{server} git -C {code_root} show --stat {commit[:12]}"
            if server
            else f"git -C {code_root} show --stat {commit[:12]}"
        )

    ok = bool(commit or files or patch_summary)
    return {
        "bug_id": bug_id,
        "ok": ok,
        "product": product,
        "run_id": formal.name,
        "commit": commit or None,
        "prior_commits": prior,
        "message": message or None,
        "files_changed": files,
        "patch_summary": patch_summary or None,
        "patch_sha256": change.get("patch_sha256"),
        "root_cause_brief": (str(root_cause.get("root_cause") or "")[:200] or None),
        "pushed": closure.get("pushed"),
        "zentao_updated": closure.get("zentao_updated"),
        "links": links,
        "error": None if ok else "run found but no commit/files yet",
    }


def collect_bug_diffs(paths: PipelinePaths, bug_ids: list[str]) -> dict[str, Any]:
    items = [collect_bug_diff(paths, bid) for bid in bug_ids]
    return {
        "schema_version": "1.0",
        "count": len(items),
        "ok_count": sum(1 for i in items if i.get("ok")),
        "items": items,
    }


def format_bug_diffs_text(doc: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in doc.get("items") or []:
        bid = item.get("bug_id")
        lines.append(f"=== bug {bid} ===")
        if not item.get("ok"):
            lines.append(f"  ERROR: {item.get('error')}")
            continue
        lines.append(f"  product:  {item.get('product')}")
        lines.append(f"  run_id:   {item.get('run_id')}")
        lines.append(f"  commit:   {item.get('commit')}")
        if item.get("prior_commits"):
            lines.append(f"  prior:    {', '.join(str(x) for x in item['prior_commits'])}")
        lines.append(f"  message:  {item.get('message')}")
        lines.append(f"  summary:  {item.get('patch_summary')}")
        files = item.get("files_changed") or []
        if files:
            lines.append("  files:")
            for f in files:
                lines.append(f"    - {f}")
        links = item.get("links") or {}
        for k, v in links.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
