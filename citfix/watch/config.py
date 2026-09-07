"""Load watch.yaml configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass
class WatchConfig:
    schema_version: str = "1.0"
    workspace: Path = field(default_factory=_repo_root)
    cursor_bin: str = "cursor-agent"
    cursor_args: list[str] = field(default_factory=lambda: ["--workspace"])
    nudge_dry_run: bool = True
    use_thread_resume: bool = True
    scan_runs_seconds: int = 60
    scan_batches_seconds: int = 120
    scan_retry_seconds: int = 30
    stall_timeout_seconds: dict[str, int] = field(
        default_factory=lambda: {"default": 1200}
    )
    blocked_nudge_after_seconds: int = 600
    run_wall_clock_max_seconds: int = 86400
    nudge_max: int = 3
    infra_max: int = 3
    product_max: int = 3
    nudge_backoff_seconds: list[int] = field(default_factory=lambda: [120, 300, 900])
    infra_backoff_seconds: list[int] = field(default_factory=lambda: [5, 20, 60])
    batch_item_running_timeout_seconds: int = 3600
    escalate_file_notify: bool = True
    # Prefer formal runs/<PRODUCT>/<run_id>/watch when scanning/writing
    prefer_formal_watch: bool = True
    # After LOG-12 cutover: do not scan runs_work for active runs (formal only)
    scan_intermediate_runs: bool = False
    # Optional: nudge when run_events.jsonl has no new row (default off — observe first)
    event_heartbeat_enabled: bool = False
    event_heartbeat_timeout_seconds: int = 1800

    def stall_timeout_for(self, stage_id: str) -> int:
        table = self.stall_timeout_seconds or {}
        if stage_id in table:
            return int(table[stage_id])
        # prefix match e.g. 09_*
        for key, val in table.items():
            if key != "default" and stage_id.startswith(str(key).split("_")[0]):
                return int(val)
        return int(table.get("default", 1200))

    def nudge_backoff(self, nudge_count: int) -> int:
        seq = self.nudge_backoff_seconds or [120]
        idx = min(max(nudge_count, 0), len(seq) - 1)
        return int(seq[idx])


def load_watch_config(path: Path | None = None) -> WatchConfig:
    """Load YAML config; missing file → defaults. Prefer watch.yaml then .example."""
    repo = _repo_root()
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend(
        [
            repo / "config" / "citfix" / "watch.yaml",
            repo / "config" / "citfix" / "watch.yaml.example",
        ]
    )
    raw: dict[str, Any] = {}
    for p in candidates:
        if p.is_file():
            try:
                import yaml

                loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    raw = loaded
                    break
            except Exception:
                continue

    retry = raw.get("retry") if isinstance(raw.get("retry"), dict) else {}
    batch = raw.get("batch") if isinstance(raw.get("batch"), dict) else {}
    ws = str(raw.get("workspace") or "").strip()
    workspace = Path(ws) if ws else repo

    stall = raw.get("stall_timeout_seconds")
    if not isinstance(stall, dict):
        stall = {"default": 1200}

    return WatchConfig(
        schema_version=str(raw.get("schema_version") or "1.0"),
        workspace=workspace,
        cursor_bin=str(raw.get("cursor_bin") or "cursor-agent"),
        cursor_args=list(raw.get("cursor_args") or ["--workspace"]),
        nudge_dry_run=bool(raw.get("nudge_dry_run", True)),
        use_thread_resume=bool(raw.get("use_thread_resume", True)),
        scan_runs_seconds=int(raw.get("scan_runs_seconds") or 60),
        scan_batches_seconds=int(raw.get("scan_batches_seconds") or 120),
        scan_retry_seconds=int(raw.get("scan_retry_seconds") or 30),
        stall_timeout_seconds={str(k): int(v) for k, v in stall.items()},
        blocked_nudge_after_seconds=int(raw.get("blocked_nudge_after_seconds") or 600),
        run_wall_clock_max_seconds=int(raw.get("run_wall_clock_max_seconds") or 86400),
        nudge_max=int(retry.get("nudge_max") or 3),
        infra_max=int(retry.get("infra_max") or 3),
        product_max=int(retry.get("product_max") or 3),
        nudge_backoff_seconds=[int(x) for x in (retry.get("nudge_backoff_seconds") or [120, 300, 900])],
        infra_backoff_seconds=[int(x) for x in (retry.get("infra_backoff_seconds") or [5, 20, 60])],
        batch_item_running_timeout_seconds=int(
            batch.get("item_running_timeout_seconds") or 3600
        ),
        escalate_file_notify=bool(raw.get("escalate_file_notify", True)),
        prefer_formal_watch=bool(raw.get("prefer_formal_watch", True)),
        scan_intermediate_runs=bool(raw.get("scan_intermediate_runs", False)),
        event_heartbeat_enabled=bool(raw.get("event_heartbeat_enabled", False)),
        event_heartbeat_timeout_seconds=int(
            raw.get("event_heartbeat_timeout_seconds") or 1800
        ),
    )
