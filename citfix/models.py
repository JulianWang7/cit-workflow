"""Shared datatypes for citfix workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass
class Blocker:
    step: str
    reason: str
    completed_context: dict[str, Any] = field(default_factory=dict)
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageRecord:
    status: StageStatus
    kb_doc: str = ""
    kb_doc_path: str = ""
    kb_doc_read: bool = False
    started_at: str = ""
    completed_at: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    blocker: Blocker | None = None
    log_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "status": self.status.value,
            "kb_doc": self.kb_doc,
            "kb_doc_path": self.kb_doc_path,
            "kb_doc_read": self.kb_doc_read,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "outputs": self.outputs,
            "log_path": self.log_path,
        }
        if self.blocker:
            d["blocker"] = self.blocker.to_dict()
        return d


@dataclass
class StageResult:
    outputs: dict[str, str] = field(default_factory=dict)
    blocker: Blocker | None = None
    skipped: bool = False


@dataclass
class RunContext:
    paths: Any
    pipeline: dict[str, Any]
    bug_id: str
    run_id: str
    run_dir: Any
    intermediate_dir: Any
    skip_stage_ids: set[str]
    entry_mode: str = "citfix_direct"
    force_verify_auto: bool = False
    device_serial_override: str | None = None
    project_alias: str | None = None

    def stage_root(self, stage_cfg: dict[str, Any]):
        """正式阶段根：``cit-workflow/runs/<PRODUCT>/<run_id>/<NN_stage>/``（output 真源）。"""
        return self.run_dir / stage_cfg["dir"]

    def intermediate_stage_root(self, stage_cfg: dict[str, Any]):
        """中间产物根：``runs_work/projects/<PRODUCT>/runs/<run_id>/<NN_stage>/``。"""
        return self.intermediate_dir / stage_cfg["dir"]

    def stage_log_path(self, stage_cfg: dict[str, Any]):
        p = (
            self.intermediate_stage_root(stage_cfg)
            / "logs"
            / f"{self.run_id}_{stage_cfg['id']}.log"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def run_stage_mirror(self, stage_cfg: dict[str, Any]):
        """Mirror formal stage tree into the intermediate (test-bed) copy."""
        return self.intermediate_stage_root(stage_cfg)
