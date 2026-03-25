from __future__ import annotations

import re

from localmelo.melo.checker.payloads import (
    ExecutorRequest,
    ExecutorResultPayload,
    GatewayIngressPayload,
    MemoryWritePayload,
    SessionTransition,
    ToolResolutionResult,
    ValidationResult,
)
from localmelo.melo.checker.validators import (
    validate_executor_request,
    validate_executor_result,
    validate_gateway_ingress,
    validate_memory_write,
    validate_session_transition,
    validate_tool_resolution,
)
from localmelo.melo.schema import CheckResult, Message, ToolCall, ToolDef, ToolResult

BLOCKED_COMMANDS = [
    r"\brm\s+-rf\s+/",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":(){.*};",
    r"\bshutdown\b",
    r"\breboot\b",
]

MAX_OUTPUT_LEN = 50_000


class Checker:
    def __init__(self) -> None:
        self._blocked = [re.compile(p) for p in BLOCKED_COMMANDS]

    # ── v0.1 API (backward compatible) ──

    # ── Core → Memory boundary ──

    async def pre_plan(self, messages: list[Message]) -> CheckResult:
        total = sum(len(m.content) for m in messages)
        if total > 100_000:
            return CheckResult(allowed=False, reason=f"Prompt too large: {total} chars")
        return CheckResult(allowed=True)

    async def post_plan(self, response: Message) -> CheckResult:
        if response.tool_call and not response.tool_call.tool_name:
            return CheckResult(allowed=False, reason="Empty tool name in response")
        return CheckResult(allowed=True)

    # ── Core → Executor boundary ──

    async def pre_execute(
        self, tool_call: ToolCall, tool_def: ToolDef | None
    ) -> CheckResult:
        if tool_def is None:
            return CheckResult(
                allowed=False, reason=f"Unknown tool: {tool_call.tool_name}"
            )

        # check dangerous shell commands
        if tool_call.tool_name == "shell_exec":
            cmd = tool_call.arguments.get("command", "")
            for pattern in self._blocked:
                if pattern.search(cmd):
                    return CheckResult(
                        allowed=False, reason=f"Blocked command: {cmd[:100]}"
                    )

        return CheckResult(allowed=True)

    # ── Executor → Core boundary ──

    async def post_execute(
        self, tool_call: ToolCall, result: ToolResult
    ) -> CheckResult:
        if len(result.output) > MAX_OUTPUT_LEN:
            truncated = result.output[:MAX_OUTPUT_LEN] + "\n... [truncated]"
            return CheckResult(
                allowed=True,
                reason="Output truncated",
                modified_payload=ToolResult(
                    tool_name=result.tool_name,
                    output=truncated,
                    error=result.error,
                    duration_ms=result.duration_ms,
                ),
            )
        return CheckResult(allowed=True)

    # ── Memory write boundary ──

    async def pre_memory_write(self, text: str) -> CheckResult:
        if len(text) > MAX_OUTPUT_LEN:
            return CheckResult(
                allowed=False, reason=f"Memory write too large: {len(text)} chars"
            )
        return CheckResult(allowed=True)

    # ── v0.2 API: Structured boundary validators ──

    def check_gateway_ingress(self, payload: GatewayIngressPayload) -> ValidationResult:
        return validate_gateway_ingress(payload)

    def check_session_transition(
        self, transition: SessionTransition
    ) -> ValidationResult:
        return validate_session_transition(transition)

    def check_tool_resolution(self, result: ToolResolutionResult) -> ValidationResult:
        return validate_tool_resolution(result)

    def check_executor_request(self, request: ExecutorRequest) -> ValidationResult:
        return validate_executor_request(request)

    def check_executor_result(self, result: ExecutorResultPayload) -> ValidationResult:
        return validate_executor_result(result)

    def check_memory_write(self, payload: MemoryWritePayload) -> ValidationResult:
        return validate_memory_write(payload)
