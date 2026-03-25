"""Executor: structured tool execution with safety boundaries."""

from localmelo.melo.executor.builtins import BUILTINS
from localmelo.melo.executor.executor import Executor

# Re-export for backward compatibility
from localmelo.melo.executor.models import (
    ArtifactMeta,
    ErrorCategory,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionStatus,
    LogMeta,
)
from localmelo.melo.executor.policy import WorkspacePolicy


def register_builtins(executor: Executor, hippo: object) -> None:
    """Register all built-in tools with the executor and hippo."""
    for tool_def, fn in BUILTINS:
        hippo.register_tool(tool_def)  # type: ignore[attr-defined]
        executor.register(tool_def.name, fn)


__all__ = [
    "ArtifactMeta",
    "BUILTINS",
    "ErrorCategory",
    "ExecutionOutcome",
    "ExecutionRequest",
    "ExecutionStatus",
    "Executor",
    "LogMeta",
    "WorkspacePolicy",
    "register_builtins",
]
