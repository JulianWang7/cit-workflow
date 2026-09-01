#!/usr/bin/env python3
"""CIT 上下文快照阶段烟测：校验目录与 JSON 门禁字段（不依赖真机/禅道）。

用法:
  python scripts/cit_smoke_context_prepare.py
  python scripts/cit_smoke_context_prepare.py --run-dir runs/CIT-xxx
  python scripts/cit_smoke_context_prepare.py --fixture-dir fixtures/context_prepare
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = ("workspace.json", "context.json", "task_ref.json")


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: root must be object")
    return data


def _require(obj: dict, keys: list[str], label: str, errors: list[str]) -> None:
    for k in keys:
        if k not in obj:
            errors.append(f"{label}: missing field '{k}'")
        elif obj[k] in (None, ""):
            errors.append(f"{label}: empty field '{k}'")


def validate_bundle(bundle_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not bundle_dir.is_dir():
        return [f"bundle dir not found: {bundle_dir}"], warnings

    paths = {name: bundle_dir / name for name in REQUIRED_FILES}
    for name, path in paths.items():
        if not path.is_file():
            errors.append(f"missing file: {name}")

    if errors:
        return errors, warnings

    workspace = _load(paths["workspace.json"])
    task = _load(paths["task_ref.json"])
    context = _load(paths["context.json"])

    _require(workspace, ["schema_version", "server", "code_root"], "workspace.json", errors)
    _require(task, ["schema_version", "bug_id", "title"], "task_ref.json", errors)
    _require(
        context,
        ["schema_version", "run_id", "source", "gates", "validation"],
        "context.json",
        errors,
    )

    source = context.get("source")
    if not isinstance(source, dict):
        errors.append("context.json: source must be object")
    else:
        _require(source, ["project", "code_root"], "context.json.source", errors)

    gates = context.get("gates")
    if not isinstance(gates, dict) or "ready_for_analyze" not in gates:
        errors.append("context.json: gates.ready_for_analyze required")
    elif not isinstance(gates["ready_for_analyze"], bool):
        errors.append("context.json: gates.ready_for_analyze must be bool")

    validation = context.get("validation")
    if not isinstance(validation, dict):
        errors.append("context.json: validation must be object")
    else:
        if "ok" not in validation or "errors" not in validation:
            errors.append("context.json: validation.ok and validation.errors required")
        elif gates.get("ready_for_analyze") is True and validation.get("ok") is not True:
            errors.append("ready_for_analyze=true but validation.ok is not true")

    title = str(task.get("title", ""))
    steps = str(task.get("steps", ""))
    cit_hint = task.get("cit_hint") if isinstance(task.get("cit_hint"), dict) else {}
    text = f"{title}\n{steps}".lower()
    looks_cit = "cit" in text
    if looks_cit and not cit_hint.get("title_has_cit_keyword"):
        warnings.append("title/steps look like CIT but cit_hint.title_has_cit_keyword is not true")
    if looks_cit and not (workspace.get("cit_source_path") or source.get("cit_source_path")):
        warnings.append("CIT-like task without cit_source_path in workspace/context")

    for att in task.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        if att.get("missing") is True or not att.get("local_path"):
            warnings.append(
                f"attachment {att.get('id')} not localized "
                f"(missing={att.get('missing')}, local_path={att.get('local_path')!r})"
            )

    return errors, warnings


def resolve_default_dirs() -> list[Path]:
    dirs: list[Path] = [REPO_ROOT / "fixtures" / "context_prepare"]
    runs = REPO_ROOT / "runs"
    if runs.is_dir():
        for run_dir in sorted(runs.iterdir()):
            out = run_dir / "06_context_snapshot" / "output"
            if out.is_dir():
                dirs.append(out)
    return dirs


def main() -> int:
    parser = argparse.ArgumentParser(description="CIT context-prepare smoke test")
    parser.add_argument("--run-dir", type=Path, help="runs/<run_id> or .../06_context_snapshot/output")
    parser.add_argument("--fixture-dir", type=Path, help="directory containing the three JSON files")
    args = parser.parse_args()

    targets: list[Path] = []
    if args.fixture_dir:
        targets.append(args.fixture_dir)
    if args.run_dir:
        p = args.run_dir
        if (p / "06_context_snapshot" / "output").is_dir():
            targets.append(p / "06_context_snapshot" / "output")
        else:
            targets.append(p)
    if not targets:
        targets = resolve_default_dirs()

    print(f"[cit-smoke] repo={REPO_ROOT}")
    overall_fail = False
    for target in targets:
        errors, warnings = validate_bundle(target)
        status = "PASS" if not errors else "FAIL"
        if errors:
            overall_fail = True
        print(f"[cit-smoke] {status}  {target}")
        for w in warnings:
            print(f"  WARN  {w}")
        for e in errors:
            print(f"  ERROR {e}")

    if overall_fail:
        print("[cit-smoke] FAILED")
        return 1
    print("[cit-smoke] ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
