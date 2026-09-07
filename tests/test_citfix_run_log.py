"""Tests for formal run_events / stage log helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citfix.models import RunContext
from citfix.run_log import (
    append_run_event,
    emit_event,
    read_run_events,
    redact_event,
    redact_text,
    run_events_path,
    stage_log_file,
)


class RunLogTests(unittest.TestCase):
    def test_append_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "runs" / "P" / "CIT-20260907-1"
            path = append_run_event(
                run_dir,
                {
                    "run_id": "CIT-20260907-1",
                    "bug_id": "1",
                    "stage_id": "00_run_registry",
                    "actor": "engine",
                    "event": "stage_start",
                    "level": "info",
                    "summary": "start",
                    "refs": {},
                },
                raise_on_error=True,
            )
            self.assertIsNotNone(path)
            self.assertTrue(run_events_path(run_dir).is_file())
            rows = read_run_events(run_dir)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "stage_start")
            self.assertEqual(rows[0]["actor"], "engine")

    def test_redact_secrets(self) -> None:
        text = redact_text("Authorization: Bearer abc.def password=hunter2")
        self.assertNotIn("hunter2", text)
        self.assertIn("***", text)
        row = redact_event(
            {
                "summary": "token=secret123",
                "refs": {"api_key": "xyz", "path": "ok"},
            }
        )
        self.assertEqual(row["refs"]["api_key"], "***")
        self.assertEqual(row["refs"]["path"], "ok")
        self.assertNotIn("secret123", row["summary"])

    def test_skip_bad_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            p = run_events_path(run_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                '{"ts":"t","run_id":"r","bug_id":"1","stage_id":"","actor":"engine",'
                '"event":"ok","level":"info","summary":"a","refs":{}}\n'
                "NOT_JSON\n"
                '{"ts":"t2","run_id":"r","bug_id":"1","stage_id":"","actor":"engine",'
                '"event":"ok2","level":"info","summary":"b","refs":{}}\n',
                encoding="utf-8",
            )
            rows = read_run_events(run_dir)
            self.assertEqual([r["event"] for r in rows], ["ok", "ok2"])

    def test_stage_log_path_under_formal_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            formal = root / "runs" / "Prod" / "CIT-20260907-99"
            formal.mkdir(parents=True)
            ctx = RunContext(
                paths=None,
                pipeline={},
                bug_id="99",
                run_id="CIT-20260907-99",
                run_dir=formal,
                intermediate_dir=root / "runs_work",
                skip_stage_ids=set(),
            )
            p = ctx.stage_log_path({"id": "03_zentao_fetch", "dir": "03_zentao_fetch"})
            self.assertEqual(p, stage_log_file(formal, "CIT-20260907-99", "03_zentao_fetch"))
            self.assertTrue(str(p).replace("\\", "/").endswith("logs/stages/CIT-20260907-99_03_zentao_fetch.log"))

    def test_emit_failure_does_not_raise_by_default(self) -> None:
        # Invalid path on Windows: empty drive-like — use a file as "directory"
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "not_a_dir"
            bogus.write_text("x", encoding="utf-8")
            # Parent exists but path/logs cannot be created under a file — use parent that is a file
            result = emit_event(
                bogus,
                actor="engine",
                event="stage_start",
                summary="should fail softly",
                run_id="r",
                bug_id="1",
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
