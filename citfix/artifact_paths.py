"""Formal run artifact path rules (hard layout).

Canonical formal root-cause path::

    runs/<PRODUCT>/CIT-YYYYMMDD-<bug_id>/07_analysis/output/root_cause_<bug_id>.json

PRODUCT may contain spaces (e.g. ``SLB783 - Android14``). Intermediate
``runs_work/...`` paths are never valid for ``analysis_conclusion_path``.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

# runs/<product>/CIT-YYYYMMDD-<bugid>/07_analysis/output/root_cause_<bugid>.json
_ROOT_CAUSE_RE = re.compile(
    r"^runs/"
    r"(?P<product>[^/]+)/"
    r"CIT-(?P<date>\d{8})-(?P<bug>\d+)/"
    r"07_analysis/output/"
    r"root_cause_(?P<bug2>\d+)\.json$"
)

_STAGE_DIR_RE = re.compile(
    r"^runs/"
    r"(?P<product>[^/]+)/"
    r"CIT-(?P<date>\d{8})-(?P<bug>\d+)/"
    r"(?P<stage>\d{2}[a-z]?_[a-z0-9_]+)/"
)


def to_posix_rel(path: str) -> str:
    s = str(path or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def expected_root_cause_rel(product: str, run_id: str, bug_id: str) -> str:
    """Return repo-relative formal path for this run's root_cause JSON."""
    prod = " ".join(str(product or "").split()).strip() or "_unassigned"
    rid = str(run_id or "").strip()
    bid = str(bug_id or "").strip()
    return f"runs/{prod}/{rid}/07_analysis/output/root_cause_{bid}.json"


def validate_root_cause_rel(
    path: str,
    *,
    bug_id: str | None = None,
    product: str | None = None,
    run_id: str | None = None,
) -> list[str]:
    """Return validation errors (empty = ok)."""
    rel = to_posix_rel(path)
    errors: list[str] = []
    if not rel:
        return ["analysis_conclusion_path empty"]
    if rel.startswith("runs_work/") or "/runs_work/" in rel:
        errors.append("analysis_conclusion_path must be under formal runs/, not runs_work/")
    if "://" in rel or re.match(r"^[A-Za-z]:/", rel):
        errors.append("analysis_conclusion_path must be repo-relative (not absolute/URL)")
    m = _ROOT_CAUSE_RE.match(rel)
    if not m:
        errors.append(
            "analysis_conclusion_path must match "
            "runs/<PRODUCT>/CIT-YYYYMMDD-<bug_id>/07_analysis/output/root_cause_<bug_id>.json"
        )
        return errors
    if m.group("bug") != m.group("bug2"):
        errors.append("run_id bug segment != root_cause_<bug_id>")
    if bug_id and str(bug_id) != m.group("bug"):
        errors.append(f"path bug {m.group('bug')!r} != expected {bug_id!r}")
    if run_id:
        got_rid = f"CIT-{m.group('date')}-{m.group('bug')}"
        if str(run_id).strip() != got_rid:
            errors.append(f"path run_id {got_rid!r} != expected {run_id!r}")
    if product:
        prod = " ".join(str(product).split()).strip()
        if prod and prod != m.group("product"):
            errors.append(f"path product {m.group('product')!r} != {prod!r}")
    return errors


def validate_formal_stage_prefix(path: str, *, bug_id: str | None = None) -> list[str]:
    """Soft check that a path is under runs/<product>/CIT-*/<stage>/."""
    rel = to_posix_rel(path)
    if not rel.startswith("runs/"):
        return [f"path must start with runs/: {rel!r}"]
    if not _STAGE_DIR_RE.match(rel) and not _ROOT_CAUSE_RE.match(rel):
        # allow deeper files under a stage dir
        parts = PurePosixPath(rel).parts
        if len(parts) < 4:
            return [f"formal path too short: {rel!r}"]
        if parts[0] != "runs":
            return [f"not under runs/: {rel!r}"]
        rid = parts[2]
        if not re.match(r"^CIT-\d{8}-\d+$", rid):
            return [f"run_id segment invalid: {rid!r}"]
        if bug_id and not rid.endswith(f"-{bug_id}"):
            return [f"run_id {rid!r} does not end with -{bug_id}"]
    return []


def resolve_under_repo(repo_root: Path, rel: str) -> Path:
    return (repo_root / to_posix_rel(rel)).resolve()
