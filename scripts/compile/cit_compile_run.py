#!/usr/bin/env python3
"""Execute a compile_plan: source_verified / adb_push + optional promote to runs/09|10."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout or "", p.stderr or ""


def build_result_from_plan(
    plan: dict[str, Any],
    *,
    file_sha256: str | None,
    verification: str,
    plan_ref: str,
) -> dict[str, Any]:
    art0 = (plan.get("artifacts_expected") or [{}])[0]
    mode = plan.get("build_mode")
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "bug_id": plan.get("bug_id"),
        "run_id": plan.get("run_id"),
        "attempt": plan.get("attempt", 1),
        "marker": "COMPILE_DONE",
        "build_mode": mode,
        "build_scope": plan.get("build_scope"),
        "plan_ref": plan_ref,
        "rule_id": plan.get("rule_id"),
        "built_at": _now(),
    }
    if mode == "source_verified":
        result.update(
            {
                "source_server_path": art0.get("source_server_path"),
                "source_file_sha256": file_sha256 or "",
                "changed_file": art0.get("relative_path") or art0.get("name"),
                "verification": verification,
                "success": bool(file_sha256),
                "status": "success" if file_sha256 else "pending_server_hash",
            }
        )
    elif mode == "jenkins":
        result.update(
            {
                "jenkins": {
                    "job": plan.get("jenkins_job"),
                    "enabled": plan.get("jenkins_enabled"),
                    "status": "not_configured" if not plan.get("jenkins_enabled") else "pending",
                },
                "success": False,
                "status": "jenkins_not_configured"
                if not plan.get("jenkins_enabled")
                else "pending_trigger",
            }
        )
    else:
        result.update(
            {
                "success": False,
                "status": f"executor_stub_for_{mode}",
                "verification": verification,
            }
        )
    return result


def artifact_result_from_plan(
    plan: dict[str, Any],
    build: dict[str, Any],
    *,
    build_ref: str,
    device_sha256: str | None,
    deploy_pending: bool,
    deploy_note: str,
) -> dict[str, Any]:
    arts = []
    for exp in plan.get("artifacts_expected") or []:
        sha = build.get("source_file_sha256") or ""
        item = {
            "name": exp.get("name"),
            "source_server_path": exp.get("source_server_path"),
            "remote_device_path": exp.get("remote_device_path"),
            "sha256": sha,
            "verified": bool(sha),
        }
        if device_sha256:
            item["device_sha256"] = device_sha256
            item["verified"] = bool(sha) and device_sha256 == sha
        arts.append(item)
    return {
        "schema_version": "1.0",
        "bug_id": plan.get("bug_id"),
        "run_id": plan.get("run_id"),
        "attempt": plan.get("attempt", 1),
        "artifact_mode": plan.get("artifact_mode") or plan.get("deploy_mode"),
        "build_ref": build_ref,
        "deploy_pending": deploy_pending,
        "deploy_note": deploy_note,
        "device_serial": plan.get("device_serial"),
        "artifacts": arts,
        "registered_at": _now(),
        "marker": "ARTIFACT_READY",
    }


def promote_to_run(run_id: str, bug_id: str, build: dict[str, Any], artifact: dict[str, Any]) -> None:
    run_root = ROOT / "runs" / run_id
    bdir = run_root / "09_jenkins_build" / "output"
    adir = run_root / "10_artifacts" / "output"
    bdir.mkdir(parents=True, exist_ok=True)
    adir.mkdir(parents=True, exist_ok=True)
    (bdir / f"build_{bug_id}.json").write_text(
        json.dumps(build, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (adir / f"artifact_{bug_id}.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="CIT compile plan runner")
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--case-dir", type=Path, help="default: plan parent")
    ap.add_argument("--local-file", type=Path, help="optional local mirror for sha256 / adb push")
    ap.add_argument("--server-sha256", default="", help="sha256 already taken on build server")
    ap.add_argument("--execute", action="store_true", help="attempt adb push when possible")
    ap.add_argument("--promote", action="store_true", help="write runs/<run_id>/09 and 10 JSON")
    ap.add_argument("--adb", default="adb", help="adb binary")
    args = ap.parse_args()

    plan = _load(args.plan)
    if plan.get("status") == "blocked":
        print(json.dumps({"error": "plan blocked", "reason": plan.get("reason")}, ensure_ascii=False))
        return 2

    case_dir = args.case_dir or args.plan.parent
    case_dir.mkdir(parents=True, exist_ok=True)
    plan_ref = str(args.plan.resolve())

    file_sha = args.server_sha256.strip() or None
    verification = "sha256 supplied via --server-sha256"
    if not file_sha and args.local_file and args.local_file.is_file():
        file_sha = _sha256_file(args.local_file)
        verification = f"local sha256 of {args.local_file}"
    if not file_sha and plan.get("build_mode") == "source_verified":
        verification = (
            "pending: provide --server-sha256 from ssh sha256sum on source_server_path "
            "or --local-file mirror"
        )

    build = build_result_from_plan(
        plan, file_sha256=file_sha, verification=verification, plan_ref=plan_ref
    )
    build_path = case_dir / "build_result.json"
    build_path.write_text(json.dumps(build, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    device_sha = None
    deploy_pending = True
    deploy_note = "dry-run or no device push yet"
    serial = plan.get("device_serial") or ""

    if args.execute and plan.get("deploy_mode") == "adb_push" and file_sha:
        art0 = (plan.get("artifacts_expected") or [{}])[0]
        remote = art0.get("remote_device_path")
        local = args.local_file
        if not serial:
            deploy_note = "device_serial empty — cannot adb push; keep deploy_pending"
        elif not local or not local.is_file():
            deploy_note = "adb_push needs --local-file (pulled config) for this runner"
        else:
            cmds = [
                [args.adb, "-s", serial, "root"],
                [args.adb, "-s", serial, "remount"],
                [args.adb, "-s", serial, "push", str(local), str(remote)],
            ]
            ok = True
            logs: list[dict[str, Any]] = []
            for c in cmds:
                rc, out, err = _run(c)
                logs.append({"cmd": c, "rc": rc, "out": out[-500:], "err": err[-500:]})
                if rc != 0 and ("push" in c or "remount" in c):
                    ok = False
            if ok:
                rc, out, err = _run(
                    [
                        args.adb,
                        "-s",
                        serial,
                        "shell",
                        f"sha256sum {remote} 2>/dev/null || echo HASH_UNAVAILABLE",
                    ]
                )
                logs.append({"cmd": "device_hash", "rc": rc, "out": out, "err": err})
                token = (out or "").strip().split()[0] if out.strip() else ""
                if len(token) == 64:
                    device_sha = token
                    if device_sha == file_sha:
                        deploy_pending = False
                        deploy_note = f"adb push ok; device sha256 matches ({serial})"
                    else:
                        deploy_note = f"adb push done; device hash mismatch: {device_sha}"
                else:
                    deploy_pending = True
                    deploy_note = "adb push completed; device sha256 unavailable"
            else:
                deploy_note = f"adb push failed; last={logs[-1]}"
            (case_dir / "adb_push_log.json").write_text(
                json.dumps(logs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    elif plan.get("deploy_mode") == "adb_push" and not args.execute:
        deploy_note = "plan is adb_push; re-run with --execute --local-file --server-sha256 to deploy"

    artifact = artifact_result_from_plan(
        plan,
        build,
        build_ref=str(build_path.resolve()),
        device_sha256=device_sha,
        deploy_pending=deploy_pending,
        deploy_note=deploy_note,
    )
    art_path = case_dir / "artifact_result.json"
    art_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.promote and plan.get("run_id") and plan.get("bug_id"):
        formal_build = (
            ROOT
            / "runs"
            / str(plan["run_id"])
            / "09_jenkins_build"
            / "output"
            / f"build_{plan['bug_id']}.json"
        )
        artifact["build_ref"] = str(formal_build)
        promote_to_run(str(plan["run_id"]), str(plan["bug_id"]), build, artifact)
        art_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "build": str(build_path),
                "artifact": str(art_path),
                "build_mode": build.get("build_mode"),
                "deploy_pending": artifact.get("deploy_pending"),
                "promoted": bool(args.promote),
            },
            ensure_ascii=False,
        )
    )
    if plan.get("build_mode") == "source_verified" and not build.get("source_file_sha256"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
