from __future__ import annotations

import re

from ..schema import CheckResult, Message, ToolCall, ToolDef, ToolResult

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
