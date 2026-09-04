"""Deterministic closure helpers for 14_closure (writeback + outbox)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def build_commit_message(
    bug_id: str,
    *,
    description: str,
    solution: str,
    product: str = "UNKNOWN",
    id_kind: str = "BugID",
) -> str:
    """CIT commit message (English): ``[Product][BugID|TaskID]<id>[Description]…[Solution]…``.

    Description/Solution must be English summaries for the product source tree.
    Push/ZenTao writeback remain reserved; default does not auto-push.
    """
    prod = " ".join(str(product or "UNKNOWN").split()).strip("[]") or "UNKNOWN"
    kind_raw = str(id_kind or "BugID").strip()
    kind = "TaskID" if kind_raw.lower() in ("task", "taskid", "task_id") else "BugID"
    bid = str(bug_id or "").strip() or "0"
    desc = " ".join(str(description or "").split()) or "CIT fix"
    sol = " ".join(str(solution or "").split()) or "see change bundle"
    line = f"[{prod}][{kind}]{bid}[Description]{desc}[Solution]{sol}"
    # Soft cap for hooks; keep tags intact, trim description/solution if needed
    if len(line) > 120:
        overhead = len(f"[{prod}][{kind}]{bid}[Description][Solution]") + 6
        budget = max(20, 120 - overhead)
        half = max(8, budget // 2)
        desc = desc[:half]
        sol = sol[: max(8, budget - len(desc))]
        line = f"[{prod}][{kind}]{bid}[Description]{desc}[Solution]{sol}"
    return line


def upsert_outbox_event(run_dir: Path, event: dict[str, Any]) -> Path:
    """Write/replace one outbox line by ``dedupe_key`` (overwrite stale SHA/message)."""
    out_dir = run_dir / "14_closure" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "outbox_events.jsonl"
    payload = dict(event)
    payload.setdefault("emitted_at", _now_iso())
    payload.setdefault("run_id", run_dir.name)
    key = str(payload.get("dedupe_key") or "")
    existing: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                existing.append(obj)
    if key:
        kept = [e for e in existing if str(e.get("dedupe_key") or "") != key]
        # Preserve non-matching events; drop prior same-key (stale commit SHA)
        kept.append(payload)
        lines = kept
    else:
        lines = existing + [payload]
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines),
        encoding="utf-8",
    )
    return path


def resync_closure_artifacts(
    run_dir: Path,
    bug_id: str,
    *,
    repo_root: Path | None = None,
    drain: bool = True,
    force_redeliver: bool = True,
) -> dict[str, Any]:
    """Authoritative re-sync: ``closure_<id>.json`` → change/outbox/report.

    Use after Worker re-commit (new SHA) so report/outbox never keep an old SHA.
    """
    closure_path = run_dir / "14_closure" / "output" / f"closure_{bug_id}.json"
    if not closure_path.is_file():
        raise FileNotFoundError(str(closure_path))
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    if not isinstance(closure, dict):
        raise ValueError("closure JSON root must be object")
    summary = post_closure_side_effects(
        run_dir,
        bug_id,
        closure,
        repo_root=repo_root,
        drain=drain,
        force_redeliver=force_redeliver,
    )
    summary["closure_path"] = str(closure_path)
    summary["authoritative_commit"] = closure.get("commit")
    summary["authoritative_message"] = closure.get("message")
    return summary


def push_target_from_upstream(branch_upstream: str) -> str:
    """``origin/LA.UM…`` → ``origin HEAD:refs/for/LA.UM…``."""
    u = str(branch_upstream or "").strip()
    if u.startswith("origin/"):
        branch = u[len("origin/") :]
    elif u.startswith("refs/for/"):
        return f"origin HEAD:{u}"
    else:
        branch = u or "master"
    return f"origin HEAD:refs/for/{branch}"


def sync_change_json(
    change_path: Path,
    *,
    committed: bool | None = None,
    pushed: bool | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """Update 08 change_*.json in place; return updated dict.

    On re-commit: previous ``commit`` moves to ``prior_commits[]``; ``commit``
    becomes the authoritative SHA (no stale dual-SHA left as primary).
    """
    data: dict[str, Any] = {}
    if change_path.is_file():
        data = json.loads(change_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}
    if committed is not None:
        data["committed"] = bool(committed)
    if pushed is not None:
        data["pushed"] = bool(pushed)
    if commit:
        prev = str(data.get("commit") or "").strip()
        new = str(commit).strip()
        if prev and new and prev != new:
            prior = data.get("prior_commits")
            if not isinstance(prior, list):
                prior = []
            if prev not in prior:
                prior.append(prev)
            data["prior_commits"] = prior
            data["superseded_commit"] = prev
        if not str(data.get("source_commit") or "").strip():
            data["source_commit"] = prev or new
        data["commit"] = new
    data["closure_synced_at"] = _now_iso()
    change_path.parent.mkdir(parents=True, exist_ok=True)
    change_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return data


def append_outbox_event(
    run_dir: Path,
    event: dict[str, Any],
) -> Path:
    """Append one JSON line (legacy). Prefer ``upsert_outbox_event`` for closure_done."""
    out_dir = run_dir / "14_closure" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "outbox_events.jsonl"
    payload = dict(event)
    payload.setdefault("emitted_at", _now_iso())
    payload.setdefault("run_id", run_dir.name)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def post_closure_side_effects(
    run_dir: Path,
    bug_id: str,
    closure: dict[str, Any],
    *,
    repo_root: Path | None = None,
    drain: bool = True,
    force_redeliver: bool = False,
) -> dict[str, Any]:
    """After 14 JSON validates: sync 08 + outbox + optional drain/report.

    ``closure`` is authoritative for commit SHA + message. Outbox ``closure_done``
    is upserted by dedupe_key so a re-commit overwrites the stale SHA line.
    """
    from .outbox import drain_outbox, write_closure_report

    change_path = run_dir / "08_changes" / "output" / f"change_{bug_id}.json"
    mode = str(closure.get("closure_mode") or "local_commit_only").lower()
    commit = closure.get("commit")
    committed = bool(commit) and not closure.get("commit_skipped")
    pushed = closure.get("pushed") is True

    # Reserved foothold: full mode will later require push + zentao (not enforced yet).
    closure.setdefault("push_gate_reserved", True)
    closure.setdefault("zentao_close_gate_reserved", True)

    synced = {}
    if change_path.is_file() or committed or pushed:
        synced = sync_change_json(
            change_path,
            committed=committed if committed or mode == "full" else None,
            pushed=pushed if mode == "full" or pushed else (False if closure.get("push_skipped") else None),
            commit=str(commit) if commit else None,
        )

    event = {
        "event_type": "closure_done",
        "bug_id": str(bug_id),
        "closure_mode": mode,
        "marker": closure.get("marker"),
        "commit": commit,
        "pushed": pushed,
        "zentao_updated": closure.get("zentao_updated"),
        "gerrit_url": closure.get("gerrit_url"),
        "change_id": closure.get("change_id"),
        "message": closure.get("message"),
        "dedupe_key": f"{run_dir.name}:closure_done:{bug_id}",
        "authoritative": True,
    }
    outbox = upsert_outbox_event(run_dir, event)

    drain_summary = None
    report_path = None
    root = repo_root
    if root is None:
        root = Path(__file__).resolve().parents[1]
    if drain:
        drain_summary = drain_outbox(
            run_dir, root, force=bool(force_redeliver)
        )
        report_path = str(
            write_closure_report(run_dir, bug_id, closure, drain_summary=drain_summary)
        )
    else:
        report_path = str(write_closure_report(run_dir, bug_id, closure, drain_summary=None))

    return {
        "change_synced": bool(synced),
        "outbox": str(outbox),
        "event": event,
        "outbox_drain": drain_summary,
        "closure_report": report_path,
    }
