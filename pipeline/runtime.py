"""Unified Agent Runtime interface.

The pipeline owns task preparation, official scoring and reporting; a runtime
owns only "how the agent works" (one-shot model call vs. the frozen multi-turn
executor with tools). Keeping this boundary small is what lets different runtimes
be compared without touching scoring.

A runtime must never call the official scorer. It returns a candidate artifact
(and its sha256) and the runner in ``run.py`` submits it through
``pipeline/scoring.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol


class RunStatus:
    COMPLETED = "completed"            # agent finished and produced a candidate
    FAILED = "failed"                  # runtime error / invalid handoff
    TRUNCATED = "truncated"            # model output limit hit
    TIMEOUT = "timeout"                # wall-clock budget exhausted
    NO_CANDIDATE = "no_candidate"      # finished without a usable artifact
    INFRA_ERROR = "infra_error"        # environment/Slurm/node failure


@dataclass(frozen=True)
class TaskContext:
    """Everything a runtime is allowed to know about one task."""

    task_id: str
    task_dir: Path                 # official workspace from make_task
    data_root: Path
    mode: str                      # 'smoke' | 'solve'
    description: str
    readme: str
    sources: dict[str, str] = field(default_factory=dict)
    input_manifest: dict[str, str] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """JSON payload handed to a one-shot prompt."""
        if self.mode == "solve":
            return {"task_id": self.task_id, "description": self.description,
                    "readme": self.readme, "sources": self.sources}
        return {"description.txt": self.description, "README.md": self.readme}


@dataclass(frozen=True)
class RuntimeConfig:
    """Budget and permissions of one agent run (never a scoring decision)."""

    runtime: str = "oneshot"
    model: str = "deepseek-flash"
    agent_timeout: int = 300
    max_steps: int = 1000
    max_generated_tokens: int = 512_000
    wall_seconds: int = 14_400
    tools: tuple[str, ...] = ()
    output_dir: Path | None = None
    allow_paid_api: bool = False


@dataclass
class AgentRunResult:
    task_id: str
    run_status: str
    runtime: str
    final_candidate_path: str | None = None
    candidate_sha256: str | None = None
    candidate_bytes: bytes | None = None
    trajectory_path: str | None = None
    llm_requests: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    walltime_sec: float | None = None
    estimated_cost_usd: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def attach_candidate(self, data: bytes) -> None:
        self.candidate_bytes = data
        self.candidate_sha256 = sha256(data).hexdigest()


class AgentRunner(Protocol):
    name: str

    def run(self, context: TaskContext, config: RuntimeConfig) -> AgentRunResult:
        ...


def get_runner(name: str, **kwargs) -> AgentRunner:
    """Factory used by ``pipeline.run``; imports lazily to keep CLI startup cheap."""
    if name == "oneshot":
        from .runtimes.oneshot import OneShotRunner

        return OneShotRunner(**kwargs)
    if name == "frozen":
        from .runtimes.frozen_baseline import FrozenBaselineRunner

        return FrozenBaselineRunner(**kwargs)
    if name == "frozen-nofeedback":
        from .runtimes.frozen import FrozenExecutorAdapter

        return FrozenExecutorAdapter(**kwargs)
    raise ValueError(f"unknown agent runtime: {name}")
