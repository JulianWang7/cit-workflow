"""Agent adapter 抽象基类 — 定义 orchestrator 与 agent 运行时的接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentResult:
    """单次 agent 会话的执行结果。"""

    success: bool                    # 是否成功完成（非超时/非崩溃）
    output: str                      # agent 的完整文本输出
    marker: str = ""                 # 结束标记（PATCH_DONE / REVIEW_PASS / SUBMIT_DONE 等）
    error: str = ""                  # 失败原因（success=False 时）
    elapsed_s: float = 0.0           # 耗时（秒）
    session_id: str = ""             # ZCode session ID（如可获取）


@dataclass
class SessionConfig:
    """单次 agent 会话的配置。"""

    goal: str                        # 会话目标（--target 或 --prompt 的内容）
    mode: str = "target"             # "target" (多轮 goal) 或 "prompt" (单次执行)
    cwd: str = ""                    # 工作目录
    disallowed_tools: list[str] = field(default_factory=list)  # 工具黑名单
    permission_mode: str = "yolo"    # yolo/build/edit/plan
    timeout_s: int = 600             # 超时秒数
    env: dict[str, str] = field(default_factory=dict)  # 额外环境变量


class AgentAdapter(ABC):
    """Agent 运行时适配器接口。"""

    @abstractmethod
    def run_session(self, config: SessionConfig) -> AgentResult:
        """执行一次 agent 会话，返回结果。

        orchestrator 通过此方法驱动每个步骤的 agent 会话。
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """检查 agent 运行时是否可用（如 CLI 二进制存在、凭证有效）。"""
        ...
