"""IO helpers for citfix stages."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _log(log_path: Path, msg: str) -> None:
    from citfix.run_log import append_stage_log_line

    append_stage_log_line(log_path, msg)


def _mirror_tree(src_dir: Path, dst_dir: Path) -> None:
    """Copy stage tree. No-op when src and dst resolve to the same path."""
    if not src_dir.is_dir():
        return
    try:
        if src_dir.resolve() == dst_dir.resolve():
            return
    except OSError:
        pass
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src_dir)
            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_download_path(msg: str) -> Path | None:
    """Parse ``已下载: <path> (<size>)`` from zentao.download_attachment."""
    m = re.search(r"已下载:\s*(.+?)\s*\(", msg)
    if not m:
        return None
    p = Path(m.group(1).strip().strip("\"'"))
    return p if p.is_file() else None


def _promote_to_formal(src: Path, dst: Path) -> None:
    """Copy intermediate file into formal runs/ when agent wrote only to test-bed."""
    if not src.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime:
        shutil.copy2(src, dst)


def _ensure_repo_on_path(repo: Path) -> None:
    s = str(repo)
    if s not in sys.path:
        sys.path.insert(0, s)

