"""Tests for formal runs/<PRODUCT>/<run_id> layout."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citfix.engine import ensure_formal_under_product, WorkflowState
from citfix.models import StageRecord, StageStatus
from citfix.paths import PipelinePaths, UNASSIGNED_PROJECT, sanitize_product_dirname


class FormalPathLayoutTests(unittest.TestCase):
    def _paths(self, root: Path) -> PipelinePaths:
        return PipelinePaths(
            repo_root=root,
            test_bed_root=root / "runs_work",
            kb_pipeline_root=root / "docs",
            runs_root=root / "runs",
        )

    def test_formal_run_dir_under_product(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._paths(root)
            p = paths.formal_run_dir("CIT-20260903-81097", "SLB783 - Android14")
            self.assertEqual(
                p,
                root / "runs" / "SLB783 - Android14" / "CIT-20260903-81097",
            )

    def test_migrate_legacy_flat_to_product(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._paths(root)
            rid = "CIT-20260903-81097"
            legacy = paths.runs_root / rid
            legacy.mkdir(parents=True)
            (legacy / "workflow_state.json").write_text(
                json.dumps({"run_id": rid, "bug_id": "81097", "product": "SLB783 - Android14"}),
                encoding="utf-8",
            )
            state = WorkflowState(
                schema_version="1.0",
                pipeline_id="citfix",
                bug_id="81097",
                run_id=rid,
                entry_mode="citfix_direct",
                current_stage="07_analysis",
                workflow_status="blocked",
                updated_at="",
                stages={"00_run_registry": StageRecord(status=StageStatus.COMPLETED)},
            )
            target = ensure_formal_under_product(
                paths, state, legacy, "SLB783 - Android14"
            )
            self.assertEqual(
                target,
                paths.runs_root / "SLB783 - Android14" / rid,
            )
            self.assertTrue((target / "workflow_state.json").is_file())
            self.assertFalse(legacy.exists())
            found = paths.find_formal_run_dir(rid)
            self.assertEqual(found, target)

    def test_sanitize_keeps_spaces(self) -> None:
        self.assertEqual(sanitize_product_dirname("SLB783 - Android14"), "SLB783 - Android14")
        self.assertEqual(sanitize_product_dirname(""), UNASSIGNED_PROJECT)


if __name__ == "__main__":
    unittest.main()
