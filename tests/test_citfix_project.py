"""Unit tests for /citfix project parsing, alias match, and batch discover filters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from citfix.cli_parse import parse_citfix_text, parse_citfix_tokens
from citfix.project_match import (
    discover_project_cit_bugs,
    match_products,
    pick_best_product,
    score_product_alias,
)


class CliParseTests(unittest.TestCase):
    def test_bug_id(self) -> None:
        cmd = parse_citfix_text("/citfix 81097 --resume")
        self.assertEqual(cmd.kind, "bug")
        self.assertEqual(cmd.bug_id, "81097")
        self.assertTrue(cmd.resume)

    def test_project_case_insensitive(self) -> None:
        for text in (
            "/citfix project slb783",
            "/citfix Project SLB783",
            "/citfix ProJeCT slb783",
            "citfix PROJECT slb783",
        ):
            cmd = parse_citfix_text(text)
            self.assertEqual(cmd.kind, "project", text)
            self.assertEqual(cmd.project_alias.lower(), "slb783", text)

    def test_project_device_reserved(self) -> None:
        cmd = parse_citfix_tokens(["project", "slb783", "--device", "SN123"])
        self.assertEqual(cmd.device_serial, "SN123")
        cmd2 = parse_citfix_text("/citfix project mc5711 --device=ABC")
        self.assertEqual(cmd2.device_serial, "ABC")

    def test_device_invalid_on_bug(self) -> None:
        with self.assertRaises(ValueError):
            parse_citfix_text("/citfix 81097 --device SN")

    def test_project_fixture_flag(self) -> None:
        cmd = parse_citfix_text(
            "/citfix project slb783 --fixture fixtures/project_discover --dry-run"
        )
        self.assertEqual(cmd.kind, "project")
        self.assertEqual(cmd.fixture, "fixtures/project_discover")
        self.assertTrue(cmd.dry_run)
        with self.assertRaises(ValueError):
            parse_citfix_text("/citfix 81097 --fixture x")


class ProductMatchTests(unittest.TestCase):
    def test_slb783_keyword(self) -> None:
        self.assertGreaterEqual(score_product_alias("slb783", "SLB783 - Android14"), 70)
        self.assertGreaterEqual(score_product_alias("SLB783", "SLB783 - Android14"), 70)

    def test_pick_best(self) -> None:
        products = [
            {"id": 1, "name": "Other", "code": ""},
            {"id": 2, "name": "SLB783 - Android14", "code": "slb783"},
            {"id": 3, "name": "MC5711", "code": ""},
        ]
        hit = pick_best_product("slb783", products)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(str(hit.product_id), "2")
        self.assertEqual(len(match_products("mc5711", products)), 1)


class DiscoverFilterTests(unittest.TestCase):
    def test_filters_assignee_product_cit_auto(self) -> None:
        products = [{"id": "10", "name": "SLB783 - Android14", "code": "slb783"}]
        bugs = [
            {
                "id": "100",
                "title": "【CIT】系统信息显示版本号错误",
                "steps": "adb getprop",
                "status": "active",
                "assignedTo": "wanglingqi",
                "product": "10",
                "product_name": "SLB783 - Android14",
                "severity": "3",
            },
            {
                "id": "101",
                "title": "【CIT】主副MIC测试异常",
                "steps": "MIC loopback 88db",
                "status": "active",
                "assignedTo": "wanglingqi",
                "product": "10",
                "product_name": "SLB783 - Android14",
                "severity": "2",
            },
            {
                "id": "102",
                "title": "【CIT】别的项目",
                "steps": "x",
                "status": "active",
                "assignedTo": "wanglingqi",
                "product": "99",
                "product_name": "MC5711",
                "severity": "3",
            },
            {
                "id": "103",
                "title": "显示问题无关键字",
                "steps": "no keyword here",
                "status": "active",
                "assignedTo": "wanglingqi",
                "product": "10",
                "product_name": "SLB783 - Android14",
            },
            {
                "id": "104",
                "title": "【CIT】指派别人",
                "steps": "x",
                "status": "active",
                "assignedTo": "other",
                "product": "10",
                "product_name": "SLB783 - Android14",
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # empty registry → MIC high-risk → human (rejected); system info → auto
            result = discover_project_cit_bugs(
                "slb783",
                repo_root=repo,
                my_bugs_fn=lambda: bugs,
                list_products_fn=lambda limit=100: products,
                configured_user_fn=lambda: "wanglingqi",
            )
        self.assertEqual(result.errors, [])
        ids = [c.bug_id for c in result.accepted]
        self.assertEqual(ids, ["100"])
        reasons = {r.bug_id: r.reject_reason for r in result.rejected}
        self.assertIn("verify_mode=", reasons["101"])
        self.assertIn("product", reasons["102"])
        self.assertEqual(reasons["103"], "cit_title_gate")
        self.assertIn("assignedTo", reasons["104"])


if __name__ == "__main__":
    unittest.main()
