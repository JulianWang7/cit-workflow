#!/usr/bin/env python3
"""Deterministic 14_closure worker: commit → (optional) Gerrit push → zentao → JSON.

Default: local commit only (no push / no zentao). Push and zentao paths are
reserved behind ``--mode full`` for later; humans push manually.

Uses bugflow.core.git / zentao / gerrit (same stack as MCP), not Agent freestyle.

Examples:
  # Default — commit only, record sha; human push later:
  python scripts/cit_closure_run.py --run-dir ... --bug-id 81097

  # Dry-run evidence only (no SSH):
  python scripts/cit_closure_run.py --run-dir ... --bug-id 81097 --mode evidence_only

  # Opt-in remote closure (reserved; not default):
  python scripts/cit_closure_run.py --run-dir ... --bug-id 81097 --mode full
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bugflow.core import git as git_mod  # noqa: E402
from bugflow.core import zentao  # noqa: E402
from citfix.closure_ops import (  # noqa: E402
    append_outbox_event,
    build_commit_message,
    post_closure_side_effects,
    push_target_from_upstream,
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _rev_parse(server: str, code_root: str) -> str:
    out, err, ec = git_mod.git_exec(server, code_root, "rev-parse HEAD", timeout=20)
    if ec != 0:
        return ""
    return (out or "").strip()


def _extract_change_id(server: str, code_root: str) -> str:
    out, _, ec = git_mod.git_exec(
        server, code_root, "log -1 --format=%B", timeout=20
    )
    if ec != 0:
        return ""
    m = re.search(r"Change-Id:\s*(I[0-9a-fA-F]+)", out or "")
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="citfix 14_closure deterministic worker")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--bug-id", required=True)
    ap.add_argument(
        "--mode",
        choices=("full", "local_commit_only", "evidence_only"),
        default="local_commit_only",
        help="default local_commit_only (no push); full = reserved push+zentao",
    )
    ap.add_argument("--skip-reason", default="")
    ap.add_argument("--product", default="", help="[Product] for commit message")
    ap.add_argument(
        "--id-kind",
        choices=("BugID", "TaskID"),
        default="BugID",
        help="BugID or TaskID tag in commit message",
    )
    ap.add_argument("--message-desc", default="", help="[Description] body override")
    ap.add_argument("--message-solution", default="", help="[Solution] body override")
    ap.add_argument("--push-target", default="", help="override origin HEAD:refs/for/… (full only)")
    ap.add_argument("--zentao-status", default="resolved")
    ap.add_argument("--no-zentao", action="store_true")
    ap.add_argument("--gerrit-score", type=int, default=0, help="optional Code-Review -2..+2 (full only)")
    ap.add_argument("--gerrit-message", default="")
    ap.add_argument("--gerrit-submit", action="store_true", help="reserved; not implemented (refuse)")
    args = ap.parse_args()

    if args.gerrit_submit:
        print("ERROR: --gerrit-submit not implemented (merge must stay human/CI gated)", file=sys.stderr)
        return 2

    run = args.run_dir
    bug = str(args.bug_id)
    change = _load(run / "08_changes" / "output" / f"change_{bug}.json")
    human = _load(run / "13_human_gate" / "output" / f"human_gate_{bug}.json")
    result = _load(run / "12_cit_test" / "output" / f"result_{bug}.json")
    workspace = _load(run / "06_context_snapshot" / "output" / "workspace.json")
    context = _load(run / "06_context_snapshot" / "output" / "context.json")

    if str(result.get("marker") or "").upper() != "VERIFY_PASS":
        print("ERROR: 12 marker must be VERIFY_PASS", file=sys.stderr)
        return 2
    verify_mode = str(
        (context.get("routing") or {}).get("verify_mode")
        or human.get("verification_mode")
        or ""
    ).lower()
    hgs = str(human.get("gate_status") or "").lower()
    if verify_mode in ("human", "hybrid") and hgs != "approved":
        print(f"ERROR: human_gate not approved (got {hgs!r})", file=sys.stderr)
        return 2
    if verify_mode == "auto" and hgs not in ("not_required", "auto_bypassed", ""):
        # empty allowed if auto bypass file missing fields — prefer explicit
        if hgs and hgs not in ("not_required", "auto_bypassed"):
            print(f"ERROR: auto mode unexpected gate_status={hgs!r}", file=sys.stderr)
            return 2

    server = str(workspace.get("server") or change.get("server") or "")
    code_root = str(workspace.get("code_root") or change.get("code_root") or "")
    files = list(change.get("files_changed") or [])
    product = (
        args.product
        or str(workspace.get("product") or change.get("product") or context.get("product") or "")
        or (run.parent.name if run.parent and run.parent.name else "")
        or "UNKNOWN"
    )
    desc = args.message_desc or change.get("patch_summary") or "CIT automated fix"
    sol = args.message_solution or "apply reviewed change bundle"
    commit_msg = build_commit_message(
        bug,
        description=desc,
        solution=sol,
        product=product,
        id_kind=args.id_kind,
    )

    closure: dict = {
        "schema_version": "1.0",
        "bug_id": bug,
        "run_id": run.name,
        "marker": "CLOSURE_DONE",
        "closure_mode": args.mode,
        "message": commit_msg,
        "server": server,
        "code_root": code_root,
        "local_branch": change.get("branch_local") or "",
        "upstream": change.get("branch_upstream") or "",
        "files_changed": files,
        "human_gate_ref": str(run / "13_human_gate" / "output" / f"human_gate_{bug}.json"),
        "human_gate_status": hgs or human.get("gate_status"),
        "verify_ref": str(run / "12_cit_test" / "output" / f"result_{bug}.json"),
        "change_ref": str(run / "08_changes" / "output" / f"change_{bug}.json"),
        "worker": "cit_closure_run.py",
        "closed_at": _now(),
    }

    if args.mode == "evidence_only":
        reason = args.skip_reason or "evidence_only via cit_closure_run"
        closure.update(
            {
                "commit": None,
                "commit_skipped": True,
                "push_skipped": True,
                "zentao_skipped": True,
                "skip_reason": reason,
                "pushed": False,
                "zentao_updated": False,
            }
        )
    else:
        if not server or not code_root:
            print("ERROR: server/code_root missing in workspace/change", file=sys.stderr)
            return 2

        # Prefer add only listed files when possible
        if files:
            for rel in files:
                git_mod.git_exec(server, code_root, ["add", "--", rel], timeout=30)
            cr = git_mod.commit(server, code_root, commit_msg, add_all=False, timeout=90)
        else:
            cr = git_mod.commit(server, code_root, commit_msg, add_all=True, timeout=90)
        if not cr.get("success"):
            print(f"ERROR: commit failed: {cr.get('output')}", file=sys.stderr)
            return 1
        sha = _rev_parse(server, code_root)
        change_id = _extract_change_id(server, code_root)
        closure["commit"] = sha
        closure["change_id"] = change_id or None
        closure["commit_log"] = cr.get("output")

        if args.mode == "local_commit_only":
            # Default path: record commit; human pushes. Push/zentao code kept under full.
            closure.update(
                {
                    "pushed": False,
                    "push_skipped": True,
                    "zentao_skipped": True,
                    "zentao_updated": False,
                    "skip_reason": args.skip_reason
                    or "default_no_push: human push; gerrit/zentao interfaces reserved",
                    "product": product,
                    "id_kind": args.id_kind,
                    "push_reserved": True,
                    "zentao_reserved": True,
                }
            )
        else:
            # Reserved opt-in: Gerrit push + zentao writeback
            target = args.push_target or push_target_from_upstream(
                str(change.get("branch_upstream") or "")
            )
            pr = git_mod.push(server, code_root, target, timeout=180)
            if not pr.get("success") and not pr.get("already_exists"):
                print(f"ERROR: push failed: {pr.get('output')}", file=sys.stderr)
                return 1
            closure["pushed"] = True
            closure["push_target"] = target
            closure["push_log"] = pr.get("output")
            closure["push_already_exists"] = bool(pr.get("already_exists"))

            if args.no_zentao:
                closure["zentao_skipped"] = True
                closure["zentao_updated"] = False
                closure["skip_reason"] = (args.skip_reason or "no-zentao flag")
                # full mode engine requires zentao_updated=true — force mode note
                print(
                    "WARN: --no-zentao with full mode will fail engine gate; "
                    "use --mode local_commit_only or omit --no-zentao",
                    file=sys.stderr,
                )
            else:
                comment = (
                    f"[citfix] {commit_msg}\n"
                    f"commit={sha}\n"
                    f"change_id={change_id}\n"
                    f"verify=PASS human_gate={hgs}"
                )
                try:
                    zr = zentao.update_bug(
                        int(bug), status=args.zentao_status, comment=comment
                    )
                except Exception as e:
                    print(f"ERROR: zentao update failed: {e}", file=sys.stderr)
                    return 1
                closure["zentao_updated"] = True
                closure["zentao_status"] = args.zentao_status
                closure["zentao_log"] = zr

            if args.gerrit_score or args.gerrit_message:
                if not change_id:
                    print("WARN: no Change-Id; skip gerrit review", file=sys.stderr)
                else:
                    try:
                        from bugflow.core import gerrit as gerrit_mod

                        gr = gerrit_mod.gerrit_review(
                            change_id,
                            message=args.gerrit_message or "citfix closure",
                            score=int(args.gerrit_score or 0),
                        )
                        closure["gerrit_review"] = gr
                    except Exception as e:
                        closure["gerrit_review_error"] = str(e)
                        print(f"WARN: gerrit review failed: {e}", file=sys.stderr)

    out_dir = run / "14_closure" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"closure_{bug}.json"
    out_path.write_text(json.dumps(closure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    side = post_closure_side_effects(run, bug, closure, repo_root=REPO)
    append_outbox_event(
        run,
        {
            "event_type": "closure_worker_finished",
            "bug_id": bug,
            "closure_path": str(out_path),
            "mode": args.mode,
            "dedupe_key": f"{run.name}:closure_worker:{bug}:{args.mode}",
        },
    )

    print(f"wrote {out_path}")
    print(f"side_effects {json.dumps(side, ensure_ascii=False)}")
    print(f"Next: /citfix {bug} --resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
