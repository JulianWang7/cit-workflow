"""Path helpers for citfix workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PipelinePaths:
    repo_root: Path
    test_bed_root: Path
    kb_pipeline_root: Path


def load_pipeline_config() -> tuple[PipelinePaths, dict]:
    repo = Path(__file__).resolve().parents[2]
    cfg_path = repo / "workflow" / "citfix_pipeline.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    paths_cfg = raw.get("paths") or {}
    paths = PipelinePaths(
        repo_root=Path(paths_cfg.get("repo_root", repo)),
        test_bed_root=Path(paths_cfg["test_bed_root"]),
        kb_pipeline_root=Path(paths_cfg["kb_pipeline_root"]),
    )
    return paths, raw
