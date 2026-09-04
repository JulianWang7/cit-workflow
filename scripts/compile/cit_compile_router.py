#!/usr/bin/env python3
"""CIT Build Router — change files → compile_plan (no side effects)."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROUTER = ROOT / "config" / "compile" / "build_router.json"
DEFAULT_POLICY = ROOT / "config" / "compile" / "cit_lineage_policy.json"
DEFAULT_JENKINS = ROOT / "config" / "compile" / "jenkins_jobs.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Minimal glob → regex: **, *, ? ; paths use forward slashes."""
    s = glob.replace("\\", "/")
    out: list[str] = []
    i = 0
    while i < len(s):
        if s.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif s.startswith("**", i):
            # trailing or mid-segment ** → any remaining path
            out.append(".*")
            i += 2
        elif s[i] == "*":
            out.append("[^/]*")
            i += 1
        elif s[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(s[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, glob: str) -> bool:
    p = path.replace("\\", "/").lstrip("./")
    g = glob.replace("\\", "/")
    if _glob_to_regex(g).match(p):
        return True
    # also allow matching when path is absolute but glob is repo-relative
    if "MeiGLink/" in p and not g.startswith("**/") and not g.startswith("MeiGLink/"):
        idx = p.find("MeiGLink/")
        if idx >= 0 and _glob_to_regex(g).match(p[idx:]):
            return True
    return bool(_glob_to_regex("**/" + g.lstrip("/")).match(p)) if not g.startswith("**/") else False


def first_matching_rule(files: list[str], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    for rule in rules:
        globs = (rule.get("when") or {}).get("path_glob_any") or []
        for f in files:
            for g in globs:
                if path_matches(f, g):
                    return rule
    return None


def map_device_path(rel: str, lineage: str, policy: dict[str, Any]) -> str:
    rel_n = rel.replace("\\", "/")
    if lineage == "cit4" and rel_n.endswith("config.yaml"):
        return "/vendor/etc/meig/config.yaml"
    # cit3 / default: mirror under /vendor/meig/apps/cit/etc/
    if "/etc/" in rel_n:
        name = rel_n.split("/")[-1]
        return f"/vendor/meig/apps/cit/etc/{name}"
    if rel_n.endswith("cit.apk"):
        return "/system/app/CITTest/CITTest.apk"
    return f"/vendor/{rel_n}" if not rel_n.startswith("vendor/") else f"/{rel_n}"


def abs_server_path(code_root: str, rel: str) -> str:
    cr = code_root.rstrip("/")
    r = rel.replace("\\", "/").lstrip("/")
    return f"{cr}/{r}"


def build_request_from_change(
    change: dict[str, Any],
    project_info: dict[str, Any],
    run_id: str,
    attempt: int = 1,
) -> dict[str, Any]:
    lineage = "cit3"
    for rb in project_info.get("repo_branches") or []:
        if rb.get("tree") == "cit" and "cit4" in str(rb.get("path") or ""):
            lineage = "cit4"
    return {
        "schema_version": "1.0",
        "bug_id": str(change.get("bug_id") or ""),
        "run_id": run_id,
        "attempt": attempt,
        "project": project_info.get("project_name") or "",
        "code_root": change.get("code_root") or project_info.get("code_root") or "",
        "server": change.get("server") or project_info.get("server") or "110",
        "cit_lineage": lineage,
        "files_changed": list(change.get("files_changed") or []),
        "source_base_sha": change.get("source_base_sha") or "",
        "patch_sha256": change.get("patch_sha256") or "",
        "device_serial": project_info.get("device_serial") or None,
        "change_ref": "",
        "requested_by": "cit_compile_router",
    }


def route(request: dict[str, Any], router: dict[str, Any], policy: dict[str, Any], jenkins: dict[str, Any]) -> dict[str, Any]:
    files = list(request.get("files_changed") or [])
    if not files:
        return {
            "schema_version": "1.0",
            "bug_id": request.get("bug_id"),
            "run_id": request.get("run_id"),
            "attempt": request.get("attempt", 1),
            "status": "blocked",
            "rule_id": "empty_files",
            "build_scope": "none",
            "build_mode": "none",
            "deploy_mode": "none",
            "artifact_mode": "none",
            "qfil_required": False,
            "jenkins_job": None,
            "jenkins_enabled": False,
            "reason": "files_changed empty",
            "locks": [],
            "artifacts_expected": [],
            "created_at": _now(),
        }

    # Forbidden path check against policy globs
    for f in files:
        for g in policy.get("forbidden_edit_globs") or []:
            if path_matches(f, g):
                return {
                    "schema_version": "1.0",
                    "bug_id": request.get("bug_id"),
                    "run_id": request.get("run_id"),
                    "attempt": request.get("attempt", 1),
                    "status": "blocked",
                    "rule_id": "forbid_meiglink_cit_source",
                    "build_scope": "none",
                    "build_mode": "none",
                    "deploy_mode": "none",
                    "artifact_mode": "none",
                    "qfil_required": False,
                    "jenkins_job": None,
                    "jenkins_enabled": False,
                    "reason": "CIT 3.0/4.0 source edits forbidden",
                    "locks": [],
                    "artifacts_expected": [],
                    "cit_lineage_policy": policy.get("lineages", {}).get(request.get("cit_lineage") or "cit3"),
                    "created_at": _now(),
                }

    rule = first_matching_rule(files, router.get("rules") or [])
    if not rule:
        return {
            "schema_version": "1.0",
            "bug_id": request.get("bug_id"),
            "run_id": request.get("run_id"),
            "status": "blocked",
            "rule_id": "no_rule",
            "build_scope": "none",
            "build_mode": "none",
            "deploy_mode": "none",
            "artifact_mode": "none",
            "qfil_required": False,
            "jenkins_job": None,
            "jenkins_enabled": False,
            "reason": "no router rule matched",
            "locks": [],
            "artifacts_expected": [],
            "created_at": _now(),
        }

    action = rule.get("action") or {}
    if action.get("status") == "blocked":
        return {
            "schema_version": "1.0",
            "bug_id": request.get("bug_id"),
            "run_id": request.get("run_id"),
            "attempt": request.get("attempt", 1),
            "status": "blocked",
            "rule_id": rule.get("id"),
            "build_scope": "none",
            "build_mode": "none",
            "deploy_mode": "none",
            "artifact_mode": "none",
            "qfil_required": False,
            "jenkins_job": None,
            "jenkins_enabled": False,
            "reason": action.get("reason") or "blocked by router",
            "locks": [],
            "artifacts_expected": [],
            "created_at": _now(),
        }

    build_mode = request.get("force_build_mode") or action.get("build_mode")
    deploy_mode = request.get("force_deploy_mode") or action.get("deploy_mode")
    job_name = action.get("jenkins_job")
    job_cfg = (jenkins.get("jobs") or {}).get(job_name or "", {})
    jenkins_enabled = bool(job_name) and bool(job_cfg.get("enabled"))

    code_root = str(request.get("code_root") or "")
    lineage = str(request.get("cit_lineage") or "cit3")
    artifacts = []
    for rel in files:
        artifacts.append(
            {
                "name": Path(rel).name,
                "source_server_path": abs_server_path(code_root, rel),
                "remote_device_path": map_device_path(rel, lineage, policy),
                "kind": "config" if build_mode == "source_verified" else "binary",
                "relative_path": rel.replace("\\", "/"),
            }
        )

    locks = []
    if code_root:
        locks.append(f"code_root:{code_root}")
    if request.get("device_serial"):
        locks.append(f"device:{request['device_serial']}")
    if job_name and jenkins_enabled:
        locks.append(f"jenkins:{job_name}")

    return {
        "schema_version": "1.0",
        "bug_id": request.get("bug_id"),
        "run_id": request.get("run_id"),
        "attempt": request.get("attempt", 1),
        "status": "ready",
        "rule_id": rule.get("id"),
        "build_scope": action.get("build_scope"),
        "build_mode": build_mode,
        "deploy_mode": deploy_mode,
        "artifact_mode": action.get("artifact_mode") or deploy_mode,
        "qfil_required": bool(action.get("qfil_required")),
        "jenkins_job": job_name,
        "jenkins_enabled": jenkins_enabled,
        "reason": action.get("notes") or f"matched rule {rule.get('id')}",
        "locks": locks,
        "revision": {
            "source_base_sha": request.get("source_base_sha") or "",
            "patch_sha256": request.get("patch_sha256") or "",
            "source_commit": None,
            "effective_revision_note": (
                "SOURCE_COMMIT optional until Jenkins path; "
                "source_verified uses file sha256 as evidence"
            ),
        },
        "artifacts_expected": artifacts,
        "cit_lineage": lineage,
        "cit_lineage_policy": policy.get("lineages", {}).get(lineage, {}),
        "project": request.get("project"),
        "code_root": code_root,
        "server": request.get("server"),
        "device_serial": request.get("device_serial"),
        "created_at": _now(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="CIT compile router")
    ap.add_argument("--change", type=Path, help="08 change_*.json")
    ap.add_argument("--request", type=Path, help="compile_request.json")
    ap.add_argument("--project-info", type=Path, help="plan_bank project_info.json")
    ap.add_argument("--run-id", default="", help="override/set run_id")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--router", type=Path, default=DEFAULT_ROUTER)
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    ap.add_argument("--jenkins", type=Path, default=DEFAULT_JENKINS)
    ap.add_argument("--out", type=Path, required=True, help="compile_plan.json path")
    ap.add_argument("--out-request", type=Path, help="optional write compile_request.json")
    args = ap.parse_args()

    router = _load(args.router)
    policy = _load(args.policy)
    jenkins = _load(args.jenkins)

    if args.request:
        request = _load(args.request)
    elif args.change and args.project_info:
        change = _load(args.change)
        project_info = _load(args.project_info)
        run_id = args.run_id or str(change.get("run_id") or "")
        if not run_id:
            raise SystemExit("--run-id required when change JSON lacks run_id")
        request = build_request_from_change(change, project_info, run_id, args.attempt)
    else:
        raise SystemExit("provide --request OR (--change and --project-info)")

    if args.run_id:
        request["run_id"] = args.run_id
    if args.out_request:
        args.out_request.parent.mkdir(parents=True, exist_ok=True)
        args.out_request.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    plan = route(request, router, policy, jenkins)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": plan.get("status"), "rule_id": plan.get("rule_id"), "out": str(args.out)}, ensure_ascii=False))
    return 0 if plan.get("status") in ("ready", "planned") else 2


if __name__ == "__main__":
    raise SystemExit(main())
