"""Agent 适配器：ZCode（M1/M2）/ Cursor / Codex（M3+ stub）。"""

from bugflow.cli.adapters.base import AgentAdapter
from bugflow.cli.adapters.zcode_adapter import ZCodeAdapter

__all__ = ["AgentAdapter", "ZCodeAdapter"]
