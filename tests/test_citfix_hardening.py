"""Unit tests for artifact path rules, bug diff CLI parse, runtime verify policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citfix.artifact_paths import expected_root_cause_rel, validate_root_cause_rel
from citfix.cli_parse import parse_citfix_text
from citfix.closure_ops import build_commit_message, post_closure_side_effects, upsert_outbox_event
from citfix.human_cases import apply_runtime_verify_policy


class ArtifactPathTests(unittest.TestCase):
    def test_expected_and_validate(self) -> None:
        rel = expected_root_cause_rel("SLB783 - Android14", "CIT-20260903-81097", "81097")
        self.assertEqual(
            rel,
            "runs/SLB783 - Android14/CIT-20260903-81097/07_analysis/output/root_cause_81097.json",
        )
        self.assertEqual(
            validate_root_cause_rel(
                rel, bug_id="81097", product="SLB783 - Android14", run_id="CIT-20260903-81097"
            ),
            [],
        )
        self.assertTrue(
            validate_root_cause_rel("runs/CIT-20260901-81097/07_analysis/output/root_cause_81097.json")
        )
        self.assertTrue(
            validate_root_cause_rel(
                "runs_work/projects/X/runs/CIT-1-1/07_analysis/output/root_cause_1.json"
            )
        )


class CliDiffParseTests(unittest.TestCase):
    def test_multi_ids_are_serial_bugs(self) -> None:
        cmd = parse_citfix_text("/citfix 81097 97203")
        self.assertEqual(cmd.kind, "bugs")
        self.assertEqual(cmd.bug_ids, ("81097", "97203"))

    def test_diff_keyword_only(self) -> None:
        cmd = parse_citfix_text("/citfix diff 81097 97203")
        self.assertEqual(cmd.kind, "diff")
        self.assertEqual(cmd.bug_ids, ("81097", "97203"))

    def test_diff_flag(self) -> None:
        cmd = parse_citfix_text("/citfix 81097 --diff")
        self.assertEqual(cmd.kind, "diff")

    def test_single_still_bug(self) -> None:
        cmd = parse_citfix_text("/citfix 81097 --resume")
        self.assertEqual(cmd.kind, "bug")


class RuntimeVerifyPolicyTests(unittest.TestCase):
    def test_force_auto_keeps_classified(self) -> None:
        ver = {
            "mode": "human",
            "human_cases": [{"case_id": "mic"}],
            "auto_cases": [],
            "classified_mode": "human",
            "classified_human_cases": [{"case_id": "mic"}],
            "classified_auto_cases": [],
        }
        out = apply_runtime_verify_policy(ver, force_auto=True)
        self.assertEqual(out["mode"], "auto")
        self.assertEqual(out["human_cases"], [])
        self.assertEqual(out["classified_mode"], "human")
        self.assertEqual(out["classified_human_cases"][0]["case_id"], "mic")


class ClosureUpsertTests(unittest.TestCase):
    def test_upsert_overwrites_same_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "CIT-X"
            upsert_outbox_event(
                run,
                {
                    "event_type": "closure_done",
                    "commit": "oldsha0000001",
                    "message": "old",
                    "dedupe_key": "CIT-X:closure_done:1",
                },
            )
            upsert_outbox_event(
                run,
                {
                    "event_type": "closure_done",
                    "commit": "newsha0000002",
                    "message": "新文案",
                    "dedupe_key": "CIT-X:closure_done:1",
                },
            )
            lines = (run / "14_closure" / "output" / "outbox_events.jsonl").read_text(
                encoding="utf-8"
            ).strip().splitlines()
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])
            self.assertEqual(obj["commit"], "newsha0000002")

    def test_commit_message_english_default(self) -> None:
        msg = build_commit_message(
            "81097",
            description="MIC loopback threshold low",
            solution="common threshold 85 to 88",
            product="SLB783 - Android14",
        )
        self.assertIn("MIC loopback", msg)
        self.assertIn("[Solution]common threshold 85 to 88", msg)

    def test_post_closure_report_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "CIT-X"
            ch = run / "08_changes" / "output" / "change_1.json"
            ch.parent.mkdir(parents=True)
            ch.write_text(json.dumps({"bug_id": "1"}), encoding="utf-8")
            closure = {
                "marker": "CLOSURE_DONE",
                "closure_mode": "local_commit_only",
                "commit": "abcdefghijklmn",
                "pushed": False,
                "push_skipped": True,
                "zentao_updated": False,
                "message": "[P][BugID]1[Description]测[Solution]改",
            }
            summary = post_closure_side_effects(run, "1", closure, drain=False)
            report = json.loads(Path(summary["closure_report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["commit"], "abcdefghijklmn")
            self.assertEqual(report["message"], closure["message"])


if __name__ == "__main__":
    unittest.main()
