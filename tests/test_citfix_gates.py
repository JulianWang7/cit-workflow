"""Unit tests for citfix gate validation and human-case registry."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citfix.human_cases import high_risk_hit, resolve_verification
from citfix.stages import _validate_agent_output


REPO = Path(__file__).resolve().parents[1]


class HumanCaseRegistryTests(unittest.TestCase):
    def test_slb783_mic_maps_human(self) -> None:
        ver = resolve_verification(
            REPO,
            "SLB783 - Android14",
            "【CIT】主副MIC测试85db就能通过",
            "CIT-主副MIC测试 88db",
        )
        self.assertEqual(ver["mode"], "human")
        self.assertEqual(ver["classification_source"], "registry")
        self.assertFalse(ver["needs_proposal"])

    def test_high_risk_without_registry_needs_proposal(self) -> None:
        ver = resolve_verification(
            REPO,
            "UnknownProductXYZ",
            "【CIT】喇叭音质主观评测异常",
            "人工听感",
        )
        self.assertEqual(ver["mode"], "human")
        self.assertTrue(ver["needs_proposal"])
        self.assertTrue(high_risk_hit("喇叭音质", ""))

    def test_system_info_defaults_auto(self) -> None:
        ver = resolve_verification(
            REPO,
            "UnknownProductXYZ",
            "【CIT】系统信息显示版本号错误",
            "adb getprop 即可",
        )
        self.assertEqual(ver["mode"], "auto")


class ValidateAgentOutputTests(unittest.TestCase):
    def _write(self, tmp: Path, name: str, data: dict) -> Path:
        p = tmp / name
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def test_07_requires_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "rc.json", {"schema_version": "1.0", "root_cause": "x"})
            errs = _validate_agent_output("07_analysis", p)
            self.assertTrue(any("ROOT_CAUSE_FOUND" in e for e in errs))

    def test_12_verify_pass_rejects_null_auto(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write(
                Path(td),
                "r.json",
                {
                    "marker": "VERIFY_PASS",
                    "auto_assertions": {"cfg": {"passed": True}, "ui": {"passed": None}},
                },
            )
            errs = _validate_agent_output("12_cit_test", p)
            self.assertTrue(errs)

    def test_12_verify_pass_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write(
                Path(td),
                "r.json",
                {
                    "marker": "VERIFY_PASS",
                    "auto_assertions": {"cfg": {"passed": True}},
                    "human_assertions": {"ui": {"passed": None}},
                },
            )
            errs = _validate_agent_output("12_cit_test", p)
            self.assertEqual(errs, [])

    def test_13_pending_human_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write(
                Path(td),
                "h.json",
                {"gate_status": "pending_human", "verification_mode": "human"},
            )
            errs = _validate_agent_output(
                "13_human_gate",
                p,
                context={"routing": {"verify_mode": "human"}},
            )
            self.assertTrue(any("pending_human" in e for e in errs))

    def test_13_auto_bypassed_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = self._write(
                Path(td),
                "h.json",
                {"gate_status": "auto_bypassed", "verification_mode": "auto"},
            )
            errs = _validate_agent_output(
                "13_human_gate",
                p,
                context={"routing": {"verify_mode": "auto"}},
            )
            self.assertEqual(errs, [])

    def test_14_pending_marker_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            (run / "13_human_gate" / "output").mkdir(parents=True)
            (run / "12_cit_test" / "output").mkdir(parents=True)
            self._write(
                run / "13_human_gate" / "output",
                "human_gate_1.json",
                {"gate_status": "auto_bypassed", "verification_mode": "auto"},
            )
            self._write(
                run / "12_cit_test" / "output",
                "result_1.json",
                {"marker": "VERIFY_PASS", "auto_assertions": {"a": {"passed": True}}},
            )
            p = self._write(
                root,
                "c.json",
                {"marker": "CLOSURE_PENDING", "message": "x", "closure_mode": "full"},
            )
            errs = _validate_agent_output(
                "14_closure",
                p,
                context={"routing": {"verify_mode": "auto"}},
                run_dir=run,
                bug_id="1",
            )
            self.assertTrue(any("CLOSURE_PENDING" in e for e in errs))

    def test_14_evidence_only_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            (run / "13_human_gate" / "output").mkdir(parents=True)
            (run / "12_cit_test" / "output").mkdir(parents=True)
            self._write(
                run / "13_human_gate" / "output",
                "human_gate_1.json",
                {"gate_status": "approved", "verification_mode": "human", "approved_by": "t"},
            )
            self._write(
                run / "12_cit_test" / "output",
                "result_1.json",
                {"marker": "VERIFY_PASS", "auto_assertions": {"a": {"passed": True}}},
            )
            p = self._write(
                root,
                "c.json",
                {
                    "schema_version": "1.0",
                    "bug_id": "1",
                    "run_id": "r",
                    "marker": "CLOSURE_DONE",
                    "closure_mode": "evidence_only",
                    "message": "dry-run",
                    "commit_skipped": True,
                    "push_skipped": True,
                    "zentao_skipped": True,
                    "skip_reason": "stage14 dry-run",
                    "pushed": False,
                    "zentao_updated": False,
                    "human_gate_status": "approved",
                },
            )
            errs = _validate_agent_output(
                "14_closure",
                p,
                context={"routing": {"verify_mode": "human"}},
                run_dir=run,
                bug_id="1",
            )
            self.assertEqual(errs, [])

    def test_14_full_requires_push_zentao(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            (run / "13_human_gate" / "output").mkdir(parents=True)
            (run / "12_cit_test" / "output").mkdir(parents=True)
            self._write(
                run / "13_human_gate" / "output",
                "human_gate_1.json",
                {"gate_status": "auto_bypassed", "verification_mode": "auto"},
            )
            self._write(
                run / "12_cit_test" / "output",
                "result_1.json",
                {"marker": "VERIFY_PASS"},
            )
            p = self._write(
                root,
                "c.json",
                {
                    "marker": "CLOSURE_DONE",
                    "closure_mode": "full",
                    "message": "msg",
                    "commit": "abcd123",
                    "pushed": False,
                    "zentao_updated": False,
                },
            )
            errs = _validate_agent_output(
                "14_closure",
                p,
                context={"routing": {"verify_mode": "auto"}},
                run_dir=run,
                bug_id="1",
            )
            self.assertTrue(any("pushed=true" in e for e in errs))
            self.assertTrue(any("zentao_updated=true" in e for e in errs))

    def test_14_default_local_commit_only_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            (run / "13_human_gate" / "output").mkdir(parents=True)
            (run / "12_cit_test" / "output").mkdir(parents=True)
            self._write(
                run / "13_human_gate" / "output",
                "human_gate_1.json",
                {"gate_status": "auto_bypassed", "verification_mode": "auto"},
            )
            self._write(
                run / "12_cit_test" / "output",
                "result_1.json",
                {"marker": "VERIFY_PASS"},
            )
            p = self._write(
                root,
                "c.json",
                {
                    "marker": "CLOSURE_DONE",
                    "closure_mode": "local_commit_only",
                    "message": "[P][BugID]1[Description]x[Solution]y",
                    "commit": "abcd1234",
                    "pushed": False,
                    "push_skipped": True,
                    "zentao_skipped": True,
                    "zentao_updated": False,
                    "skip_reason": "default_no_push: human push",
                    "human_gate_status": "auto_bypassed",
                },
            )
            errs = _validate_agent_output(
                "14_closure",
                p,
                context={"routing": {"verify_mode": "auto"}},
                run_dir=run,
                bug_id="1",
            )
            self.assertEqual(errs, [])

if __name__ == "__main__":
    unittest.main()
