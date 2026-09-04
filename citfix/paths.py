"""Path helpers for citfix workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Reserved project folder before product is known from zentao.
UNASSIGNED_PROJECT = "_unassigned"


def sanitize_product_dirname(product: str) -> str:
    """Keep zentao product name as directory name; strip only path-illegal chars.

    Spaces are preserved (EXP-CIT-005). Characters invalid on Windows are replaced.
    """
    name = (product or "").strip() or UNASSIGNED_PROJECT
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.rstrip(" .")
    return name or UNASSIGNED_PROJECT


def resolve_repo_path(repo: Path, value: str | None, default: Path) -> Path:
    """Resolve a pipeline path entry against repo root when relative."""
    if value is None or str(value).strip() in ("", "."):
        return default.resolve()
    p = Path(value)
    if not p.is_absolute():
        return (repo / p).resolve()
    return p.resolve()


def to_repo_relative(repo_root: Path, path: Path | str) -> str:
    """Return a portable repo-relative POSIX path when ``path`` is under repo_root.

    Absolute paths outside the repo (e.g. compile-server ``/home2/...``) are
    returned unchanged as strings.
    """
    raw = Path(path)
    try:
        return raw.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except Exception:
        return str(path)


def resolve_under_repo(repo_root: Path, value: str | Path) -> Path:
    """Resolve ``value`` to an absolute Path; relative values are under repo_root."""
    p = Path(value)
    if p.is_absolute():
        return p
    return (Path(repo_root) / p).resolve()


@dataclass
class PipelinePaths:
    repo_root: Path
    test_bed_root: Path
    kb_pipeline_root: Path
    runs_root: Path

    def formal_run_dir(self, run_id: str, product: str | None = None) -> Path:
        """交付真源：``cit-workflow/runs/<PRODUCT>/<run_id>/``.

        Product unknown → ``runs/_unassigned/<run_id>/``.
        """
        proj = sanitize_product_dirname(product or UNASSIGNED_PROJECT)
        return self.runs_root / proj / run_id

    def find_formal_run_dir(self, run_id: str) -> Path | None:
        """Locate an existing formal run under any project (incl. legacy flat layout)."""
        candidates: list[Path] = [
            self.formal_run_dir(run_id, UNASSIGNED_PROJECT),
            self.runs_root / run_id,  # legacy: runs/<run_id>/
        ]
        if self.runs_root.is_dir():
            for proj in self.runs_root.iterdir():
                if not proj.is_dir():
                    continue
                # skip files / README-like; only dirs that look like product containers
                if proj.name.startswith("."):
                    continue
                if proj.name == run_id:
                    continue
                candidates.append(proj / run_id)
        for c in candidates:
            if c.is_dir() and (c / "workflow_state.json").is_file():
                return c
        for c in candidates:
            if c.is_dir():
                return c
        return None

    def projects_root(self) -> Path:
        return self.test_bed_root / "projects"

    def intermediate_run_dir(self, run_id: str, product: str | None = None) -> Path:
        """中间产物：runs_work/projects/<PRODUCT>/runs/<run_id>/"""
        proj = sanitize_product_dirname(product or UNASSIGNED_PROJECT)
        return self.projects_root() / proj / "runs" / run_id

    def find_intermediate_run_dir(self, run_id: str) -> Path | None:
        """Locate an existing intermediate run under any project folder."""
        candidates = [
            self.intermediate_run_dir(run_id, UNASSIGNED_PROJECT),
            self.test_bed_root / "00_runs" / run_id,  # legacy flat layout
        ]
        root = self.projects_root()
        if root.is_dir():
            for proj in root.iterdir():
                if proj.is_dir():
                    candidates.append(proj / "runs" / run_id)
        for c in candidates:
            if c.is_dir() and (c / "workflow_state.json").is_file():
                return c
        for c in candidates:
            if c.is_dir():
                return c
        return None


def load_pipeline_config() -> tuple[PipelinePaths, dict]:
    # citfix/paths.py → parents[1] = repo root
    import os

    repo = Path(__file__).resolve().parents[1]
    cfg_path = repo / "contracts" / "citfix_pipeline.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    paths_cfg = raw.get("paths") or {}
    repo_root = resolve_repo_path(repo, paths_cfg.get("repo_root"), repo)
    runs_root = resolve_repo_path(
        repo_root, paths_cfg.get("runs_root"), repo_root / "runs"
    )
    test_bed_root = resolve_repo_path(
        repo_root, paths_cfg.get("test_bed_root"), repo_root / "runs_work"
    )
    # Prefer env override for shared KB outside the clone.
    kb_cfg = os.environ.get("CIT_KB_PIPELINE_ROOT") or paths_cfg.get("kb_pipeline_root")
    kb_pipeline_root = resolve_repo_path(
        repo_root,
        kb_cfg,
        repo_root / "docs",
    )
    paths = PipelinePaths(
        repo_root=repo_root,
        test_bed_root=test_bed_root,
        kb_pipeline_root=kb_pipeline_root,
        runs_root=runs_root,
    )
    return paths, raw


def read_product_from_run(formal_or_inter: Path) -> str | None:
    """Best-effort product from tasks.json or context.json under a run dir."""
    for rel in (
        "04_result_normalize/output/tasks.json",
        "06_context_snapshot/output/context.json",
        "06_context_snapshot/output/task_ref.json",
        "workflow_state.json",
    ):
        p = formal_or_inter / rel
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rel == "workflow_state.json":
            prod = data.get("product")
            if prod and str(prod) != UNASSIGNED_PROJECT:
                return str(prod)
            continue
        if "tasks" in data and isinstance(data["tasks"], list) and data["tasks"]:
            prod = data["tasks"][0].get("product")
            if prod:
                return str(prod)
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        prod = data.get("product") or source.get("project")
        if prod:
            return str(prod)
    return None
