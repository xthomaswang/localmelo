from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from localmelo.melo.schema import ToolResult


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


class ErrorCategory(str, Enum):
    NONE = "none"
    TOOL_NOT_FOUND = "tool_not_found"
    BLOCKED_BY_CHECKER = "blocked_by_checker"
    TIMEOUT = "timeout"
    RUNTIME_ERROR = "runtime_error"
    PATH_POLICY_VIOLATION = "path_policy_violation"


@dataclass
class ArtifactMeta:
    kind: str  # "file", "stdout", "stderr"
    path: str = ""
    size_bytes: int = 0
    description: str = ""


@dataclass
class LogMeta:
    level: str = "info"  # "info", "warning", "error"
    message: str = ""
    detail: str = ""


@dataclass
class ExecutionRequest:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_ms: float | None = None
    workspace_root: str | None = None


@dataclass
class ExecutionOutcome:
    tool_name: str
    status: ExecutionStatus = ExecutionStatus.SUCCESS
    output: str = ""
    error: str = ""
    error_category: ErrorCategory = ErrorCategory.NONE
    duration_ms: float = 0.0
    artifacts: list[ArtifactMeta] = field(default_factory=list)
    logs: list[LogMeta] = field(default_factory=list)

    def to_tool_result(self) -> ToolResult:
        return ToolResult(
            tool_name=self.tool_name,
            output=self.output,
            error=self.error,
            duration_ms=self.duration_ms,
        )
