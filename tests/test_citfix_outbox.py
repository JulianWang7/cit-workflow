"""Tests for outbox drain."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citfix.outbox import drain_outbox, write_closure_report


class OutboxDrainTests(unittest.TestCase):
    def test_drain_file_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "CIT-R"
            cfg = root / "config" / "citfix"
            cfg.mkdir(parents=True)
            (cfg / "outbox_channels.json").write_text(
                json.dumps(
                    {
                        "channels": {
                            "file": {"enabled": True},
                            "webhook": {"enabled": False},
                            "wecom": {"enabled": False},
                        }
                    }
                ),
                encoding="utf-8",
            )
            out = run / "14_closure" / "output"
            out.mkdir(parents=True)
            ev = {
                "event_type": "closure_done",
                "bug_id": "1",
                "dedupe_key": "CIT-R:closure_done:1",
            }
            (out / "outbox_events.jsonl").write_text(
                json.dumps(ev, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            s1 = drain_outbox(run, root)
            self.assertEqual(s1["delivered"], 1)
            self.assertEqual(s1["failures"], [])
            s2 = drain_outbox(run, root)
            self.assertEqual(s2["delivered"], 0)
            self.assertEqual(s2["skipped"], 1)
            notes = list((out / "notifications").glob("*.json"))
            self.assertEqual(len(notes), 1)

    def test_closure_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "CIT-R"
            p = write_closure_report(
                run,
                "9",
                {"marker": "CLOSURE_DONE", "closure_mode": "evidence_only"},
                drain_summary={"delivered": 0},
            )
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["bug_id"], "9")
            self.assertEqual(data["marker"], "CLOSURE_DONE")


if __name__ == "__main__":
    unittest.main()
