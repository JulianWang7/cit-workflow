"""Unit tests for citfix.watch classify / restore / cycle."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from citfix.watch.classify import classify_run
from citfix.watch.config import WatchConfig
from citfix.watch.restore import restore_workflow_state
from citfix.watch.scan import RunWatchView, parse_iso
from citfix.watch.service import process_run
from citfix.watch.state_io import load_watch_state, snapshots_dir


def _view(
    inter: Path,
    *,
    status: str = "running",
    stage: str = "07_analysis",
    updated: datetime | None = None,
    reason: str = "",
) -> RunWatchView:
    updated = updated or datetime.now(timezone.utc).astimezone()
    ts = updated.strftime("%Y-%m-%dT%H:%M:%S%z")
    data = {
        "schema_version": "1.0",
        "bug_id": "91001",
        "run_id": "CIT-TEST-91001",
        "workflow_status": status,
        "current_stage": stage,
        "updated_at": ts,
        "stages": {stage: {"status": "running" if status == "running" else "blocked"}},
        "checkpoint": {"reason": reason},
        "product": "SLB783 - Android14",
    }
    wf = inter / "workflow_state.json"
    inter.mkdir(parents=True, exist_ok=True)
    wf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return RunWatchView(
        bug_id="91001",
        run_id="CIT-TEST-91001",
        workflow_status=status,
        current_stage=stage,
        updated_at=ts,
        updated_at_dt=parse_iso(ts),
        stage_status=data["stages"][stage]["status"],
        checkpoint_reason=reason,
        product="SLB783 - Android14",
        intermediate_dir=inter,
        formal_dir=None,
        state_path=wf,
        claim_only=False,
        raw=data,
    )


class ClassifyTests(unittest.TestCase):
    def test_scan_intermediate_default_off(self) -> None:
        cfg = WatchConfig()
        self.assertFalse(cfg.scan_intermediate_runs)
        self.assertTrue(cfg.prefer_formal_watch)

    def test_running_fresh_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            view = _view(inter, updated=datetime.now(timezone.utc).astimezone())
            cfg = WatchConfig(stall_timeout_seconds={"default": 1200})
            d = classify_run(view, {}, cfg)
            self.assertEqual(d.action, "ok")

    def test_running_stall_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            old = datetime.now(timezone.utc).astimezone() - timedelta(seconds=2000)
            view = _view(inter, updated=old)
            cfg = WatchConfig(
                stall_timeout_seconds={"default": 1200, "07_analysis": 1200},
                nudge_max=3,
            )
            d = classify_run(view, {"nudge_count": 0}, cfg)
            self.assertEqual(d.action, "nudge")
            self.assertEqual(d.error_class, "WORKER_STALL")

    def test_nudge_exhausted_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            old = datetime.now(timezone.utc).astimezone() - timedelta(seconds=2000)
            view = _view(inter, updated=old)
            cfg = WatchConfig(stall_timeout_seconds={"default": 100}, nudge_max=2)
            d = classify_run(view, {"nudge_count": 2}, cfg)
            self.assertEqual(d.action, "escalate")

    def test_human_blocked_no_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            view = _view(
                inter,
                status="blocked",
                stage="13_human_gate",
                reason="pending_human",
                updated=datetime.now(timezone.utc).astimezone() - timedelta(seconds=900),
            )
            cfg = WatchConfig(blocked_nudge_after_seconds=600)
            d = classify_run(view, {}, cfg)
            self.assertEqual(d.action, "ok")
            self.assertEqual(d.error_class, "WAITING_HUMAN")


class ProcessAndRestoreTests(unittest.TestCase):
    def test_process_nudge_dry_run_writes_watch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            old = datetime.now(timezone.utc).astimezone() - timedelta(seconds=5000)
            view = _view(inter, updated=old)
            cfg = WatchConfig(
                workspace=Path(td),
                nudge_dry_run=True,
                stall_timeout_seconds={"default": 60},
                nudge_max=3,
            )
            result = process_run(view, cfg)
            self.assertEqual(result["decision"], "nudge")
            ws = load_watch_state(inter)
            self.assertEqual(ws.get("watch_status"), "nudge_pending")
            self.assertGreaterEqual(int(ws.get("nudge_count") or 0), 1)
            snaps = list(snapshots_dir(inter).glob("workflow_state_*.json"))
            self.assertTrue(snaps)

    def test_process_nudge_prefers_formal_watch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inter = root / "runs_work" / "projects" / "P" / "runs" / "CIT-TEST-91001"
            formal = root / "runs" / "P" / "CIT-TEST-91001"
            old = datetime.now(timezone.utc).astimezone() - timedelta(seconds=5000)
            view = _view(inter, updated=old)
            view.formal_dir = formal
            formal.mkdir(parents=True)
            cfg = WatchConfig(
                workspace=root,
                nudge_dry_run=True,
                stall_timeout_seconds={"default": 60},
                prefer_formal_watch=True,
            )
            result = process_run(view, cfg)
            self.assertEqual(result["decision"], "nudge")
            self.assertTrue((formal / "watch" / "watch_state.json").is_file())
            self.assertTrue((formal / "logs" / "run_events.jsonl").is_file())
            from citfix.run_log import read_run_events

            rows = read_run_events(formal)
            self.assertTrue(any(r.get("actor") == "watch" for r in rows))

    def test_event_heartbeat_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            formal = Path(td) / "formal"
            view = _view(
                inter,
                updated=datetime.now(timezone.utc).astimezone(),  # workflow fresh
            )
            view.formal_dir = formal
            formal.mkdir(parents=True)
            from citfix.run_log import emit_event

            emit_event(
                formal,
                actor="engine",
                event="stage_start",
                summary="old",
                run_id=view.run_id,
                bug_id=view.bug_id,
                stage_id="07_analysis",
            )
            # Backdate the event file timestamp via rewriting ts
            ev = formal / "logs" / "run_events.jsonl"
            old_ts = (datetime.now(timezone.utc).astimezone() - timedelta(seconds=5000)).strftime(
                "%Y-%m-%dT%H:%M:%S%z"
            )
            ev.write_text(
                json.dumps(
                    {
                        "ts": old_ts,
                        "run_id": view.run_id,
                        "bug_id": view.bug_id,
                        "stage_id": "07_analysis",
                        "actor": "engine",
                        "event": "stage_start",
                        "level": "info",
                        "summary": "old",
                        "refs": {},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = WatchConfig(
                stall_timeout_seconds={"default": 99999},
                event_heartbeat_enabled=True,
                event_heartbeat_timeout_seconds=60,
                nudge_max=3,
                nudge_dry_run=True,
            )
            d = classify_run(
                view,
                {},
                cfg,
                last_event_age_seconds=5000,
            )
            self.assertEqual(d.action, "nudge")
            self.assertEqual(d.error_class, "EVENT_HEARTBEAT")

    def test_restore_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inter = Path(td) / "run"
            view = _view(inter)
            snap_dir = snapshots_dir(inter)
            snap = snap_dir / "workflow_state_test.json"
            snap.write_text(view.state_path.read_text(encoding="utf-8"), encoding="utf-8")
            # corrupt current
            view.state_path.write_text("{}", encoding="utf-8")
            restore_workflow_state(inter, snap)
            data = json.loads(view.state_path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("bug_id"), "91001")


if __name__ == "__main__":
    unittest.main()
