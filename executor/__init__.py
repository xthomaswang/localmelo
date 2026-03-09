from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable, Coroutine
from typing import Any

from ..checker import Checker
from ..core.gateway import Gateway
from ..schema import ToolCall, ToolDef, ToolResult


class Executor:
    def __init__(self, gateway: Gateway, checker: Checker) -> None:
        self.gateway = gateway
        self.checker = checker
        self._callables: dict[str, Callable[..., Coroutine[Any, Any, str]]] = {}

    def register(self, name: str, fn: Callable[..., Coroutine[Any, Any, str]]) -> None:
        self._callables[name] = fn

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        tool_def = self.gateway.get_tool(tool_call.tool_name)

        # pre-check
        check = await self.checker.pre_execute(tool_call, tool_def)
        if not check.allowed:
            return ToolResult(
                tool_name=tool_call.tool_name, error=f"Blocked: {check.reason}"
            )

        fn = self._callables.get(tool_call.tool_name)
        if fn is None:
            return ToolResult(
                tool_name=tool_call.tool_name,
                error=f"No callable registered for: {tool_call.tool_name}",
            )

        start = time.perf_counter()
        try:
            output = await fn(**tool_call.arguments)
        except Exception as e:
            return ToolResult(
                tool_name=tool_call.tool_name,
                error=str(e),
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        duration = (time.perf_counter() - start) * 1000

        result = ToolResult(
            tool_name=tool_call.tool_name, output=output, duration_ms=duration
        )

        # post-check
        check = await self.checker.post_execute(tool_call, result)
        if check.modified_payload:
            result = check.modified_payload

        return result


# ── Built-in tools ──

BUILTINS: list[tuple[ToolDef, Callable[..., Coroutine[Any, Any, str]]]] = []


def _builtin(
    tool_def: ToolDef,
) -> Callable[
    [Callable[..., Coroutine[Any, Any, str]]], Callable[..., Coroutine[Any, Any, str]]
]:
    def decorator(
        fn: Callable[..., Coroutine[Any, Any, str]],
    ) -> Callable[..., Coroutine[Any, Any, str]]:
        BUILTINS.append((tool_def, fn))
        return fn

    return decorator


@_builtin(
    ToolDef(
        name="shell_exec",
        description="Execute a shell command and return its output",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"}
            },
            "required": ["command"],
        },
        semantic_tags=["shell", "bash", "command", "terminal", "exec", "run"],
    )
)
async def _shell_exec(command: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    return f"{out}\n{err}".strip() if err else out.strip()


@_builtin(
    ToolDef(
        name="file_read",
        description="Read the contents of a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
            "required": ["path"],
        },
        semantic_tags=["file", "read", "open", "content", "text"],
    )
)
async def _file_read(path: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: open(path).read())


@_builtin(
    ToolDef(
        name="file_write",
        description="Write content to a file (creates or overwrites)",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        semantic_tags=["file", "write", "save", "create", "output"],
    )
)
async def _file_write(path: str, content: str) -> str:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: open(path, "w").write(content))
    return f"Written {len(content)} bytes to {path}"


@_builtin(
    ToolDef(
        name="python_exec",
        description="Execute Python code and return the result",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python code"}},
            "required": ["code"],
        },
        semantic_tags=["python", "code", "execute", "eval", "script", "compute"],
    )
)
async def _python_exec(code: str) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    ns: dict[str, Any] = {}
    with contextlib.redirect_stdout(buf):
        exec(code, ns)
    return buf.getvalue().strip()


def register_builtins(executor: Executor, gateway: Gateway) -> None:
    for tool_def, fn in BUILTINS:
        gateway.register_tool(tool_def)
        executor.register(tool_def.name, fn)
