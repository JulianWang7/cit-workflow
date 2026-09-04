"""Tests for closure_ops writeback / outbox."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citfix.closure_ops import (
    append_outbox_event,
    build_commit_message,
    post_closure_side_effects,
    push_target_from_upstream,
    sync_change_json,
)


class ClosureOpsTests(unittest.TestCase):
    def test_commit_message_gate_format(self) -> None:
        msg = build_commit_message(
            "81097",
            description="MIC threshold wrong",
            solution="85 to 88 in cit_common_config",
            product="SLB783 - Android14",
            id_kind="BugID",
        )
        self.assertTrue(msg.startswith("[SLB783 - Android14][BugID]81097[Description]"))
        self.assertIn("[Solution]", msg)

    def test_commit_message_task_kind(self) -> None:
        msg = build_commit_message(
            "123",
            description="x",
            solution="y",
            product="P",
            id_kind="TaskID",
        )
        self.assertEqual(msg, "[P][TaskID]123[Description]x[Solution]y")

    def test_push_target(self) -> None:
        self.assertEqual(
            push_target_from_upstream("origin/LA.UM.12.2.1"),
            "origin HEAD:refs/for/LA.UM.12.2.1",
        )

    def test_sync_and_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "CIT-X"
            ch = run / "08_changes" / "output" / "change_1.json"
            ch.parent.mkdir(parents=True)
            ch.write_text(
                json.dumps({"bug_id": "1", "committed": False, "pushed": False}),
                encoding="utf-8",
            )
            closure = {
                "marker": "CLOSURE_DONE",
                "closure_mode": "local_commit_only",
                "commit": "abc1234567890",
                "pushed": False,
                "push_skipped": True,
                "zentao_updated": False,
                "message": "[SLB783][BugID]81097[Description]x[Solution]y",
            }
            summary = post_closure_side_effects(run, "1", closure)
            data = json.loads(ch.read_text(encoding="utf-8"))
            self.assertTrue(data["committed"])
            self.assertFalse(data.get("pushed"))
            self.assertEqual(data["commit"], "abc1234567890")
            outbox = Path(summary["outbox"])
            self.assertTrue(outbox.is_file())
            self.assertTrue(summary.get("closure_report"))
            self.assertTrue(Path(summary["closure_report"]).is_file())
            drain = summary.get("outbox_drain") or {}
            self.assertGreaterEqual(int(drain.get("delivered") or 0), 1)
            notify = run / "14_closure" / "output" / "notifications"
            self.assertTrue(notify.is_dir())
            self.assertTrue(any(notify.glob("*.json")))

    def test_append_outbox_dedupe_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            p = append_outbox_event(run, {"event_type": "t", "dedupe_key": "a"})
            append_outbox_event(run, {"event_type": "t", "dedupe_key": "b"})
            self.assertEqual(len(p.read_text(encoding="utf-8").strip().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
