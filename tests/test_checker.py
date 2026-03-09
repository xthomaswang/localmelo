import pytest

from localmelo.checker import Checker
from localmelo.schema import Message, ToolCall, ToolDef, ToolResult


@pytest.fixture
def checker() -> Checker:
    return Checker()


class TestChecker:
    @pytest.mark.asyncio
    async def test_pre_plan_ok(self, checker: Checker) -> None:
        msgs = [Message(role="user", content="hello")]
        result = await checker.pre_plan(msgs)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_pre_plan_too_large(self, checker: Checker) -> None:
        msgs = [Message(role="user", content="x" * 200_000)]
        result = await checker.pre_plan(msgs)
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_pre_execute_blocks_dangerous(self, checker: Checker) -> None:
        td = ToolDef(name="shell_exec", description="", parameters={})
        tc = ToolCall(tool_name="shell_exec", arguments={"command": "rm -rf /"})
        result = await checker.pre_execute(tc, td)
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_pre_execute_allows_safe(self, checker: Checker) -> None:
        td = ToolDef(name="shell_exec", description="", parameters={})
        tc = ToolCall(tool_name="shell_exec", arguments={"command": "ls -la"})
        result = await checker.pre_execute(tc, td)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_pre_execute_unknown_tool(self, checker: Checker) -> None:
        tc = ToolCall(tool_name="fake_tool", arguments={})
        result = await checker.pre_execute(tc, None)
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_post_execute_truncates(self, checker: Checker) -> None:
        tc = ToolCall(tool_name="shell_exec", arguments={})
        tr = ToolResult(tool_name="shell_exec", output="x" * 100_000)
        result = await checker.post_execute(tc, tr)
        assert result.allowed
        assert result.modified_payload is not None
        assert len(result.modified_payload.output) < 100_000
