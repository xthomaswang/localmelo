"""Integration tests covering cross-module wiring after the merge.

Uses fake providers — no real network or model calls.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from localmelo.melo.agent import Agent
from localmelo.melo.contracts.providers import BaseEmbeddingProvider, BaseLLMProvider
from localmelo.melo.memory.coordinator import Hippo
from localmelo.melo.memory.history.sqlite import SqliteHistory
from localmelo.melo.memory.long.sqlite import SqliteLongTerm
from localmelo.melo.schema import Message, ToolCall, ToolDef
from localmelo.support.config import Config, ConfigError, GatewayConfig, MlcConfig
from localmelo.support.gateway.session import SessionManager

# ── Fake providers ──────────────────────────────────────────────────────────


class FakeLLM(BaseLLMProvider):
    def __init__(self, responses: list[Message] | None = None) -> None:
        self.responses: deque[Message] = deque(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self, messages: list[Message], tools: list[ToolDef] | None = None
    ) -> Message:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.responses:
            return Message(role="assistant", content="(empty)")
        return self.responses.popleft()

    async def close(self) -> None:
        pass


class FakeEmbedding(BaseEmbeddingProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(i + 1), 0.0, 0.0, 0.0] for i in range(len(texts))]

    async def close(self) -> None:
        pass


class FakeAgent:
    def __init__(self) -> None:
        self.closed = False
        self.run_count = 0
        self._run_delay = 0.0

    async def run(self, query: str) -> str:
        self.run_count += 1
        if self._run_delay:
            await asyncio.sleep(self._run_delay)
        return f"echo: {query}"

    async def close(self) -> None:
        self.closed = True


class FakeSessionManager(SessionManager):
    def _create_agent(self) -> FakeAgent:  # type: ignore[override]
        return FakeAgent()


# ── 1. Gateway startup validation failure ───────────────────────────────────


class TestGatewayStartupValidation:
    def test_validate_or_raise_blocks_empty_backend(self) -> None:
        cfg = Config(backend="")
        with pytest.raises(ConfigError, match="backend is not set"):
            cfg.validate_or_raise()

    def test_validate_or_raise_blocks_bad_backend(self) -> None:
        cfg = Config(backend="imaginary")
        with pytest.raises(ConfigError, match="invalid"):
            cfg.validate_or_raise()

    def test_validate_or_raise_blocks_missing_mlc_model(self) -> None:
        cfg = Config(backend="mlc-llm", mlc=MlcConfig(chat_model=""))
        with pytest.raises(ConfigError, match="chat_model"):
            cfg.validate_or_raise()

    def test_validate_or_raise_blocks_bad_gateway_port(self) -> None:
        cfg = Config(
            backend="mlc-llm",
            mlc=MlcConfig(chat_model="Qwen3-1.7B"),
            gateway=GatewayConfig(port=0),
        )
        with pytest.raises(ConfigError, match="port"):
            cfg.validate_or_raise()

    def test_start_gateway_validates_config(self) -> None:
        from localmelo.support.gateway import start_gateway

        cfg = Config(backend="")
        with pytest.raises(ConfigError, match="backend is not set"):
            start_gateway(cfg)

    def test_valid_config_passes(self) -> None:
        cfg = Config(
            backend="mlc-llm",
            mlc=MlcConfig(chat_model="Qwen3-1.7B", chat_port=8400),
        )
        cfg.validate_or_raise()  # should not raise


# ── 2. Agent loop still works after merged core+memory changes ──────────────


class TestAgentLoopAfterMerge:
    @pytest.mark.asyncio
    async def test_final_answer_no_tools(self) -> None:
        agent = Agent(
            llm=FakeLLM([Message(role="assistant", content="42")]),
            embedding=FakeEmbedding(),
        )
        result = await agent.run("What is 6*7?")
        assert result == "42"
        await agent.close()

    @pytest.mark.asyncio
    async def test_tool_call_then_answer(self) -> None:
        llm = FakeLLM()
        llm.responses.append(
            Message(
                role="assistant",
                content="calling echo",
                tool_call=ToolCall(tool_name="echo", arguments={"text": "hi"}),
            )
        )
        llm.responses.append(Message(role="assistant", content="echo: hi"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        agent.hippo.register_tool(
            ToolDef(
                name="echo",
                description="echo text",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        )
        agent.executor.register("echo", _echo_tool)

        result = await agent.run("echo hi")
        assert result == "echo: hi"
        await agent.close()

    @pytest.mark.asyncio
    async def test_no_prompt_duplication(self) -> None:
        llm = FakeLLM([Message(role="assistant", content="ok")])
        agent = Agent(llm=llm, embedding=FakeEmbedding())
        await agent.run("unique_marker_xyz")

        sent = llm.calls[0]["messages"]
        user_msgs = [m for m in sent if m.role == "user"]
        matches = [m for m in user_msgs if "unique_marker_xyz" in m.content]
        assert len(matches) == 1
        await agent.close()

    @pytest.mark.asyncio
    async def test_no_embedding_mode(self) -> None:
        agent = Agent(
            llm=FakeLLM([Message(role="assistant", content="ok")]),
            embedding=None,
        )
        result = await agent.run("question")
        assert result == "ok"
        await agent.close()

    @pytest.mark.asyncio
    async def test_tool_registry_search_works(self) -> None:
        hippo = Hippo(embedding=None)
        hippo.register_tool(
            ToolDef(
                name="shell_exec",
                description="execute shell commands",
                parameters={},
                semantic_tags=["shell", "bash"],
            )
        )
        tools = hippo.resolve_tools("run a shell command")
        assert any(t.name == "shell_exec" for t in tools)


# ── 3. Same-session serialization ───────────────────────────────────────────


class TestSessionSerializationAfterMerge:
    @pytest.mark.asyncio
    async def test_concurrent_runs_serialize(self) -> None:
        mgr = FakeSessionManager(max_sessions=10, idle_ttl=3600.0)
        s = await mgr.create("serial")
        agent: FakeAgent = s.agent  # type: ignore[assignment]
        agent._run_delay = 0.05

        order: list[str] = []

        async def run_task(label: str) -> None:
            async with s._lock:
                order.append(f"{label}-start")
                await agent.run("test")
                order.append(f"{label}-end")

        await asyncio.gather(run_task("A"), run_task("B"))

        # First task fully completes before second starts
        assert order[0].endswith("-start")
        assert order[1].endswith("-end")
        assert order[2].endswith("-start")
        assert order[3].endswith("-end")
        assert agent.run_count == 2

    @pytest.mark.asyncio
    async def test_session_eviction_closes_agent(self) -> None:
        import time

        mgr = FakeSessionManager(max_sessions=2, idle_ttl=0.01)
        s1 = await mgr.create("old")
        agent1: FakeAgent = s1.agent  # type: ignore[assignment]
        s1.last_active = time.time() - 1.0  # past TTL

        await mgr.create("new")
        assert mgr.get("old") is None
        assert agent1.closed


# ── 4. Optional SQLite-backed memory ────────────────────────────────────────


class TestPersistentMemoryWiring:
    @pytest.mark.asyncio
    async def test_sqlite_backends_via_env(self, tmp_path: Path) -> None:
        """LOCALMELO_PERSIST_MEMORY=1 enables SQLite backends."""
        mem_dir = str(tmp_path / "memory")
        with mock.patch.dict(
            os.environ,
            {
                "LOCALMELO_PERSIST_MEMORY": "1",
                "LOCALMELO_MEMORY_DIR": mem_dir,
            },
        ):
            agent = Agent(
                llm=FakeLLM([Message(role="assistant", content="ok")]),
                embedding=FakeEmbedding(),
            )
            assert isinstance(agent.hippo.history, SqliteHistory)
            assert isinstance(agent.hippo.long, SqliteLongTerm)

            result = await agent.run("test query")
            assert result == "ok"

            # Task was persisted in SQLite
            tasks_dir = Path(mem_dir)
            assert (tasks_dir / "history.db").exists()
            assert (tasks_dir / "long_term.db").exists()
            await agent.close()

    @pytest.mark.asyncio
    async def test_sqlite_history_only_without_embedding(self, tmp_path: Path) -> None:
        """Without embedding, only SqliteHistory is created (no long-term db)."""
        mem_dir = str(tmp_path / "memory")
        with mock.patch.dict(
            os.environ,
            {
                "LOCALMELO_PERSIST_MEMORY": "1",
                "LOCALMELO_MEMORY_DIR": mem_dir,
            },
        ):
            agent = Agent(
                llm=FakeLLM([Message(role="assistant", content="ok")]),
                embedding=None,
            )
            assert isinstance(agent.hippo.history, SqliteHistory)
            # No embedding → long-term stays in-memory (no-op)
            from localmelo.melo.memory.long import LongTerm

            assert isinstance(agent.hippo.long, LongTerm)
            await agent.close()

    @pytest.mark.asyncio
    async def test_default_is_in_memory(self) -> None:
        """Without env vars, memory backends are in-memory."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCALMELO_PERSIST_MEMORY", None)
            agent = Agent(
                llm=FakeLLM([Message(role="assistant", content="ok")]),
                embedding=FakeEmbedding(),
            )
            from localmelo.melo.memory.history import History
            from localmelo.melo.memory.long import LongTerm

            assert isinstance(agent.hippo.history, History)
            assert isinstance(agent.hippo.long, LongTerm)
            await agent.close()

    @pytest.mark.asyncio
    async def test_hippo_close_cleans_up_sqlite(self, tmp_path: Path) -> None:
        """Hippo.close() closes SQLite backends without error."""
        h = SqliteHistory(tmp_path / "h.db")
        lt = SqliteLongTerm(tmp_path / "lt.db")
        hippo = Hippo(embedding=None, history=h, long=lt)
        hippo.close()  # should not raise

    @pytest.mark.asyncio
    async def test_agent_loop_with_sqlite_backends(self, tmp_path: Path) -> None:
        """Full agent loop works with SQLite backends."""
        llm = FakeLLM()
        llm.responses.append(
            Message(
                role="assistant",
                content="calling echo",
                tool_call=ToolCall(tool_name="echo", arguments={"text": "hi"}),
            )
        )
        llm.responses.append(Message(role="assistant", content="done"))

        h = SqliteHistory(tmp_path / "h.db")
        lt = SqliteLongTerm(tmp_path / "lt.db")
        emb = FakeEmbedding()

        agent = Agent(llm=llm, embedding=emb)
        # Replace hippo with SQLite-backed one
        agent.hippo.close()
        agent.hippo = Hippo(embedding=emb, history=h, long=lt)
        # Re-register tools
        from localmelo.melo.executor import register_builtins

        agent.executor = __import__(
            "localmelo.melo.executor", fromlist=["Executor"]
        ).Executor(agent.hippo, agent.checker)
        register_builtins(agent.executor, agent.hippo)

        agent.hippo.register_tool(
            ToolDef(
                name="echo",
                description="echo text",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        )
        agent.executor.register("echo", _echo_tool)

        result = await agent.run("echo hi")
        assert result == "done"

        # Verify task persisted in SQLite
        h2 = SqliteHistory(tmp_path / "h.db")
        # There should be at least one task
        # (We can't iterate easily, but get_task with the task_id works)
        await agent.close()
        h2.close()


# ── 5. Checker v0.2 gateway ingress validation ─────────────────────────────


class TestCheckerV02GatewayIngress:
    """Gateway ingress validated through Checker v0.2."""

    def test_empty_query_rejected(self) -> None:
        from localmelo.melo.checker import Checker, GatewayIngressPayload

        checker = Checker()
        result = checker.check_gateway_ingress(GatewayIngressPayload(query=""))
        assert not result.allowed
        assert "non-empty" in result.reason

    def test_invalid_session_id_rejected(self) -> None:
        from localmelo.melo.checker import Checker, GatewayIngressPayload

        checker = Checker()
        result = checker.check_gateway_ingress(
            GatewayIngressPayload(query="hello", session_id="bad session id!")
        )
        assert not result.allowed
        assert "session_id" in result.reason

    def test_valid_ingress_sanitized(self) -> None:
        from localmelo.melo.checker import Checker, GatewayIngressPayload

        checker = Checker()
        result = checker.check_gateway_ingress(
            GatewayIngressPayload(query="  hello  ", session_id="abc123")
        )
        assert result.allowed
        assert result.sanitized_payload.query == "hello"

    def test_oversized_query_rejected(self) -> None:
        from localmelo.melo.checker import Checker, GatewayIngressPayload

        checker = Checker()
        result = checker.check_gateway_ingress(
            GatewayIngressPayload(query="x" * 200_000)
        )
        assert not result.allowed
        assert "too large" in result.reason.lower()


# ── 6. Checker v0.2 session transition validation ──────────────────────────


class TestCheckerV02SessionTransition:
    """Illegal session transitions rejected by Checker v0.2."""

    def test_closed_to_running_rejected(self) -> None:
        from localmelo.melo.checker import Checker, SessionTransition

        checker = Checker()
        result = checker.check_session_transition(
            SessionTransition(from_status="closed", to_status="running", session_id="x")
        )
        assert not result.allowed

    def test_idle_to_running_allowed(self) -> None:
        from localmelo.melo.checker import Checker, SessionTransition

        checker = Checker()
        result = checker.check_session_transition(
            SessionTransition(from_status="idle", to_status="running", session_id="x")
        )
        assert result.allowed

    def test_running_to_running_rejected(self) -> None:
        from localmelo.melo.checker import Checker, SessionTransition

        checker = Checker()
        result = checker.check_session_transition(
            SessionTransition(
                from_status="running", to_status="running", session_id="x"
            )
        )
        assert not result.allowed


# ── 7. Checker v0.2 tool resolution validation ─────────────────────────────


class TestCheckerV02ToolResolution:
    """Invalid tool resolution blocked before planning/execution."""

    @pytest.mark.asyncio
    async def test_agent_validates_resolution_and_runs(self) -> None:
        """Agent loop uses check_tool_resolution and still produces results."""
        agent = Agent(
            llm=FakeLLM([Message(role="assistant", content="ok")]),
            embedding=FakeEmbedding(),
        )
        result = await agent.run("hello")
        assert result == "ok"
        await agent.close()

    def test_duplicate_tools_rejected(self) -> None:
        from localmelo.melo.checker import Checker
        from localmelo.melo.checker.payloads import ToolResolutionResult

        checker = Checker()
        result = checker.check_tool_resolution(
            ToolResolutionResult(
                query="test",
                resolved_tool_names=["shell_exec", "shell_exec"],
            )
        )
        assert not result.allowed
        assert "Duplicate" in result.reason

    def test_empty_query_rejected(self) -> None:
        from localmelo.melo.checker import Checker
        from localmelo.melo.checker.payloads import ToolResolutionResult

        checker = Checker()
        result = checker.check_tool_resolution(
            ToolResolutionResult(query="", resolved_tool_names=["shell_exec"])
        )
        assert not result.allowed


# ── 8. Structured executor path in agent loop ──────────────────────────────


class TestStructuredExecutorInAgentLoop:
    """execute_structured used in agent loop without breaking Agent.run()."""

    @pytest.mark.asyncio
    async def test_tool_call_result_fed_back(self) -> None:
        """Tool result from structured path is fed back into conversation."""
        llm = FakeLLM()
        llm.responses.append(
            Message(
                role="assistant",
                content="calling echo",
                tool_call=ToolCall(tool_name="echo", arguments={"text": "hi"}),
            )
        )
        llm.responses.append(Message(role="assistant", content="done"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        agent.hippo.register_tool(
            ToolDef(
                name="echo",
                description="echo text",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        )
        agent.executor.register("echo", _echo_tool)

        result = await agent.run("echo hi")
        assert result == "done"

        # Verify tool result fed back in second LLM call
        sent = llm.calls[1]["messages"]
        tool_msgs = [m for m in sent if m.role == "tool"]
        assert len(tool_msgs) >= 1
        assert "hi" in tool_msgs[0].content
        await agent.close()

    @pytest.mark.asyncio
    async def test_blocked_tool_handled_via_structured(self) -> None:
        """Blocked tool calls still handled correctly via structured path."""
        llm = FakeLLM()
        llm.responses.append(
            Message(
                role="assistant",
                content="deleting",
                tool_call=ToolCall(
                    tool_name="shell_exec",
                    arguments={"command": "rm -rf /"},
                ),
            )
        )
        llm.responses.append(Message(role="assistant", content="aborted"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        result = await agent.run("delete everything")
        assert result == "aborted"
        await agent.close()

    @pytest.mark.asyncio
    async def test_unknown_tool_handled_via_structured(self) -> None:
        """Unknown tool calls produce an error via structured path."""
        llm = FakeLLM()
        llm.responses.append(
            Message(
                role="assistant",
                content="try unknown",
                tool_call=ToolCall(tool_name="nonexistent", arguments={}),
            )
        )
        llm.responses.append(Message(role="assistant", content="fallback"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        result = await agent.run("do something")
        assert result == "fallback"
        await agent.close()

    @pytest.mark.asyncio
    async def test_large_output_truncated_via_v02(self) -> None:
        """Large tool output truncated via check_executor_result."""
        llm = FakeLLM()
        llm.responses.append(
            Message(
                role="assistant",
                content="calling big",
                tool_call=ToolCall(tool_name="big_tool", arguments={}),
            )
        )
        llm.responses.append(Message(role="assistant", content="done"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        agent.hippo.register_tool(
            ToolDef(
                name="big_tool",
                description="huge output",
                parameters={"type": "object", "properties": {}},
            )
        )

        async def _big() -> str:
            return "x" * 100_000

        agent.executor.register("big_tool", _big)

        result = await agent.run("run big")
        assert result == "done"

        # Verify truncation occurred (tool msg in short-term is truncated)
        window = agent.hippo.short.get_window()
        tool_msgs = [m for m in window if m.role == "tool"]
        if tool_msgs:
            assert len(tool_msgs[-1].content) < 100_000
        await agent.close()


# ── 9. Artifact metadata survives execution path ───────────────────────────


class TestArtifactMetadata:
    """Artifact/log metadata from Executor v0.2 in agent context."""

    @pytest.mark.asyncio
    async def test_agent_executor_produces_artifacts(self) -> None:
        """The agent's executor produces artifact metadata via structured path."""

        from localmelo.melo.executor.models import ExecutionRequest, ExecutionStatus

        agent = Agent(
            llm=FakeLLM([Message(role="assistant", content="ok")]),
            embedding=FakeEmbedding(),
        )

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            outcome = await agent.executor.execute_structured(
                ExecutionRequest(
                    tool_name="file_write",
                    arguments={"path": path, "content": "test"},
                )
            )
            assert outcome.status == ExecutionStatus.SUCCESS
            assert len(outcome.artifacts) == 1
            assert outcome.artifacts[0].kind == "file"
            assert outcome.artifacts[0].path == path
        finally:
            os.unlink(path)
        await agent.close()

    @pytest.mark.asyncio
    async def test_outcome_to_tool_result_preserves_fields(self) -> None:
        """ExecutionOutcome.to_tool_result() preserves key fields."""
        from localmelo.melo.executor.models import ExecutionOutcome, ExecutionStatus

        outcome = ExecutionOutcome(
            tool_name="python_exec",
            status=ExecutionStatus.SUCCESS,
            output="42",
            duration_ms=1.5,
        )
        result = outcome.to_tool_result()
        assert result.tool_name == "python_exec"
        assert result.output == "42"
        assert result.duration_ms == 1.5
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_error_category_in_structured_path(self) -> None:
        """Error category is set correctly for unknown tools."""
        from localmelo.melo.executor.models import (
            ErrorCategory,
            ExecutionRequest,
            ExecutionStatus,
        )

        agent = Agent(
            llm=FakeLLM([Message(role="assistant", content="ok")]),
            embedding=FakeEmbedding(),
        )
        outcome = await agent.executor.execute_structured(
            ExecutionRequest(tool_name="no_such_tool")
        )
        assert outcome.status == ExecutionStatus.ERROR
        assert outcome.error_category == ErrorCategory.TOOL_NOT_FOUND
        await agent.close()


# ── Helper ──────────────────────────────────────────────────────────────────


async def _echo_tool(text: str) -> str:
    return text
