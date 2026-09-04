"""Outbox drain: deliver closure events to configured channels.

Channels are intentional footholds — file is always on; webhook/wecom are opt-in.
Gerrit submit/merge is intentionally NOT a channel.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def default_channels_config(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "config" / "citfix" / "outbox_channels.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {
        "schema_version": "1.0",
        "channels": {
            "file": {"enabled": True},
            "webhook": {"enabled": False, "url": ""},
            "wecom": {"enabled": False, "webhook_url": ""},
        },
    }


def outbox_path(run_dir: Path) -> Path:
    return run_dir / "14_closure" / "output" / "outbox_events.jsonl"


def delivered_path(run_dir: Path) -> Path:
    return run_dir / "14_closure" / "output" / "outbox_delivered.jsonl"


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _delivered_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for ev in read_events(path):
        k = str(ev.get("dedupe_key") or "")
        if k:
            keys.add(k)
        # also track delivery records
        dk = str(ev.get("source_dedupe_key") or "")
        if dk:
            keys.add(dk)
    return keys


def _post_json(url: str, payload: dict[str, Any], timeout: float = 15.0) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)


def deliver_event(
    event: dict[str, Any],
    channels_cfg: dict[str, Any],
    *,
    run_dir: Path,
) -> list[dict[str, Any]]:
    """Deliver one event; return list of delivery records."""
    ch = channels_cfg.get("channels") if isinstance(channels_cfg.get("channels"), dict) else {}
    records: list[dict[str, Any]] = []
    dedupe = str(event.get("dedupe_key") or f"{run_dir.name}:{event.get('event_type')}")

    file_ch = ch.get("file") if isinstance(ch.get("file"), dict) else {}
    if file_ch.get("enabled", True):
        notify_dir = run_dir / "14_closure" / "output" / "notifications"
        notify_dir.mkdir(parents=True, exist_ok=True)
        safe = dedupe.replace(":", "_").replace("/", "_")[:120]
        npath = notify_dir / f"{safe}.json"
        doc = {
            "channel": "file",
            "delivered_at": _now_iso(),
            "event": event,
        }
        npath.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        records.append(
            {
                "channel": "file",
                "ok": True,
                "path": str(npath),
                "source_dedupe_key": dedupe,
                "delivered_at": _now_iso(),
            }
        )

    webhook = ch.get("webhook") if isinstance(ch.get("webhook"), dict) else {}
    if webhook.get("enabled") and str(webhook.get("url") or "").strip():
        ok, detail = _post_json(str(webhook["url"]), {"source": "citfix", "event": event})
        records.append(
            {
                "channel": "webhook",
                "ok": ok,
                "detail": detail,
                "source_dedupe_key": dedupe,
                "delivered_at": _now_iso(),
            }
        )

    wecom = ch.get("wecom") if isinstance(ch.get("wecom"), dict) else {}
    if wecom.get("enabled") and str(wecom.get("webhook_url") or "").strip():
        text = (
            f"[citfix] {event.get('event_type')} bug={event.get('bug_id')} "
            f"mode={event.get('closure_mode')} pushed={event.get('pushed')}"
        )
        ok, detail = _post_json(
            str(wecom["webhook_url"]),
            {"msgtype": "text", "text": {"content": text}},
        )
        records.append(
            {
                "channel": "wecom",
                "ok": ok,
                "detail": detail,
                "source_dedupe_key": dedupe,
                "delivered_at": _now_iso(),
            }
        )

    return records


def drain_outbox(
    run_dir: Path,
    repo_root: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Drain undelivered outbox events. Returns summary."""
    src = outbox_path(run_dir)
    dst = delivered_path(run_dir)
    cfg = default_channels_config(repo_root)
    events = read_events(src)
    done = set() if force else _delivered_keys(dst)
    delivered_n = 0
    skipped_n = 0
    failures: list[str] = []

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("a", encoding="utf-8") as f:
        for ev in events:
            key = str(ev.get("dedupe_key") or "")
            if key and key in done:
                skipped_n += 1
                continue
            records = deliver_event(ev, cfg, run_dir=run_dir)
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if not rec.get("ok"):
                    failures.append(f"{rec.get('channel')}:{rec.get('detail')}")
            if key:
                done.add(key)
            delivered_n += 1

    return {
        "outbox": str(src),
        "delivered_log": str(dst),
        "events_seen": len(events),
        "delivered": delivered_n,
        "skipped": skipped_n,
        "failures": failures,
        "drained_at": _now_iso(),
    }


def write_closure_report(
    run_dir: Path,
    bug_id: str,
    closure: dict[str, Any],
    *,
    drain_summary: dict[str, Any] | None = None,
) -> Path:
    """Aggregate a human-readable completion report under 14 output."""
    out = run_dir / "14_closure" / "output" / "closure_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "1.0",
        "bug_id": str(bug_id),
        "run_id": run_dir.name,
        "marker": closure.get("marker"),
        "closure_mode": closure.get("closure_mode"),
        "commit": closure.get("commit"),
        "pushed": closure.get("pushed"),
        "zentao_updated": closure.get("zentao_updated"),
        "change_id": closure.get("change_id"),
        "gerrit_url": closure.get("gerrit_url"),
        "human_gate_status": closure.get("human_gate_status"),
        "message": closure.get("message"),
        "worker": closure.get("worker"),
        "closed_at": closure.get("closed_at"),
        "outbox_drain": drain_summary,
        "generated_at": _now_iso(),
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out
