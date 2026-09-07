"""Tests for 01_req_parse / 02_bug_task_extract auto handlers and batch intake."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from citfix.models import RunContext
from citfix.paths import PipelinePaths, UNASSIGNED_PROJECT
from citfix.project_match import CandidateBug, DiscoverResult, ProductHit
from citfix.stages.req_extract import _stage_01_req_parse, _stage_02_bug_task_extract


def _paths(repo: Path) -> PipelinePaths:
    return PipelinePaths(
        repo_root=repo,
        test_bed_root=repo / "runs_work",
        kb_pipeline_root=repo / "kb",
        runs_root=repo / "runs",
    )


def _fake_discover(alias: str, **_kwargs) -> DiscoverResult:
    prod = ProductHit(product_id="10", name="SLB783 - Android14", code="slb783", score=90)
    accepted = [
        CandidateBug(
            bug_id="100",
            title="【CIT】系统信息显示版本号错误",
            status="active",
            severity="3",
            product_id="10",
            product_name="SLB783 - Android14",
            assigned_to="wanglingqi",
            cit_ok=True,
            verify_mode="auto",
        )
    ]
    rejected = [
        CandidateBug(
            bug_id="101",
            title="【CIT】主副MIC测试异常",
            status="active",
            product_id="10",
            product_name="SLB783 - Android14",
            assigned_to="wanglingqi",
            cit_ok=True,
            verify_mode="human",
            reject_reason="verify_mode=human (project batch requires auto)",
        )
    ]
    return DiscoverResult(
        alias=alias,
        zentao_user="wanglingqi",
        matched_product=prod,
        candidates=accepted + rejected,
        accepted=accepted,
        rejected=rejected,
        errors=[],
    )


class ReqExtractStageTests(unittest.TestCase):
    def test_01_02_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            paths = _paths(repo)
            bid = "CIT-BATCH-TEST-SLB783"
            formal = paths.formal_run_dir(bid, UNASSIGNED_PROJECT)
            inter = (
                paths.projects_root()
                / UNASSIGNED_PROJECT
                / "batches"
                / bid
            )
            pipeline = {
                "stages": [
                    {"id": "01_req_parse", "dir": "01_req_parse"},
                    {"id": "02_bug_task_extract", "dir": "02_bug_task_extract"},
                ]
            }
            ctx = RunContext(
                paths=paths,
                pipeline=pipeline,
                bug_id=bid,
                run_id=bid,
                run_dir=formal,
                intermediate_dir=inter,
                skip_stage_ids=set(),
                entry_mode="citfix_batch_discover",
                force_verify_auto=True,
                project_alias="slb783",
            )
            s01 = pipeline["stages"][0]
            s02 = pipeline["stages"][1]
            with patch(
                "citfix.stages.req_extract.discover_project_cit_bugs",
                side_effect=_fake_discover,
            ):
                r1 = _stage_01_req_parse(ctx, s01, ctx.stage_log_path(s01))
            self.assertIsNone(r1.blocker)
            cand = json.loads(
                (formal / "01_req_parse" / "output" / "candidate_rows.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(cand["marker"], "CANDIDATE_ROWS_READY")
            self.assertEqual(len(cand["rows"]), 2)

            # relocate like batch intake
            product = cand["product_name"]
            formal2 = paths.formal_run_dir(bid, product)
            inter2 = paths.projects_root() / "SLB783 - Android14" / "batches" / bid
            formal2.parent.mkdir(parents=True, exist_ok=True)
            inter2.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.move(str(formal), str(formal2))
            if inter.exists():
                shutil.move(str(inter), str(inter2))
            ctx.run_dir = formal2
            ctx.intermediate_dir = inter2

            r2 = _stage_02_bug_task_extract(ctx, s02, ctx.stage_log_path(s02))
            self.assertIsNone(r2.blocker)
            ids = json.loads(
                (formal2 / "02_bug_task_extract" / "output" / "bug_ids.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(ids["marker"], "BUG_IDS_READY")
            self.assertEqual(ids["bug_ids"], ["100"])
            compat = json.loads(
                (
                    formal2 / "02_bug_task_extract" / "output" / "bug_task_ids.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(compat["bug_ids"], ["100"])


class BatchIntakeTests(unittest.TestCase):
    def test_run_batch_intake_uses_stages(self) -> None:
        from citfix.project_batch import run_batch_intake

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # Point load_pipeline_config at temp repo via patch
            fake_paths = _paths(repo)
            pipeline = {
                "pipeline_id": "citfix",
                "entry_modes": {
                    "citfix_batch_discover": {
                        "skip_stages": ["03_zentao_fetch"],
                    }
                },
                "stages": [
                    {"id": "01_req_parse", "dir": "01_req_parse", "executor": "auto"},
                    {
                        "id": "02_bug_task_extract",
                        "dir": "02_bug_task_extract",
                        "executor": "auto",
                    },
                ],
            }
            with patch(
                "citfix.project_batch.load_pipeline_config",
                return_value=(fake_paths, pipeline),
            ):
                state, batch_dir, formal = run_batch_intake(
                    "slb783",
                    discover_fn=_fake_discover,
                )
            self.assertEqual(state.queue[0].bug_id, "100")
            self.assertTrue(
                (formal / "01_req_parse" / "output" / "candidate_rows.json").is_file()
            )
            self.assertTrue(
                (batch_dir / "02_bug_task_extract" / "output" / "bug_ids.json").is_file()
                or (formal / "02_bug_task_extract" / "output" / "bug_ids.json").is_file()
            )
            self.assertTrue((batch_dir / "batch_state.json").is_file())
            from citfix.run_log import read_run_events

            events = read_run_events(formal)
            names = [e.get("event") for e in events]
            self.assertIn("batch_start", names)
            self.assertIn("batch_01_done", names)
            self.assertIn("batch_ready", names)
            self.assertTrue((formal / "logs" / "run_events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
