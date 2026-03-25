"""Agent-loop tests with fake providers — no real network or model calls."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from localmelo.melo.agent import Agent
from localmelo.melo.contracts.providers import BaseEmbeddingProvider, BaseLLMProvider
from localmelo.melo.memory.coordinator import Hippo
from localmelo.melo.schema import Message, ToolCall, ToolDef

# ── Fake providers ──────────────────────────────────────────────────────────


class FakeLLM(BaseLLMProvider):
    """Programmable fake LLM that pops responses from a queue."""

    def __init__(self, responses: list[Message] | None = None) -> None:
        self.responses: deque[Message] = deque(responses or [])
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, msg: Message) -> None:
        self.responses.append(msg)

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.responses:
            return Message(role="assistant", content="(no response queued)")
        return self.responses.popleft()

    async def close(self) -> None:
        pass


class FakeEmbedding(BaseEmbeddingProvider):
    """Returns deterministic 4-dim embeddings."""

    def __init__(self) -> None:
        self.call_count = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        return [[float(i + 1), 0.0, 0.0, 0.0] for i in range(len(texts))]

    async def close(self) -> None:
        pass


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_agent(
    responses: list[Message],
    embedding: BaseEmbeddingProvider | None = None,
) -> Agent:
    """Build an Agent wired to fake providers."""
    llm = FakeLLM(responses)
    return Agent(llm=llm, embedding=embedding)


# ── Tests ───────────────────────────────────────────────────────────────────


class TestFinalAnswer:
    """LLM returns a direct answer (no tool call) on the first turn."""

    @pytest.mark.asyncio
    async def test_returns_content(self) -> None:
        agent = _make_agent(
            [Message(role="assistant", content="42")],
            embedding=FakeEmbedding(),
        )
        result = await agent.run("What is 6*7?")
        assert result == "42"

    @pytest.mark.asyncio
    async def test_task_completed(self) -> None:
        agent = _make_agent(
            [Message(role="assistant", content="done")],
            embedding=FakeEmbedding(),
        )
        await agent.run("hello")
        # The latest task should be completed
        tasks = list(agent.hippo.history._tasks.values())
        assert len(tasks) == 1
        assert tasks[0].status == "completed"


class TestToolCallLoop:
    """LLM requests a tool call, gets the result, then answers."""

    @pytest.mark.asyncio
    async def test_tool_executes_and_feeds_back(self) -> None:
        """Step 1: LLM calls echo tool → Step 2: LLM answers with result."""
        llm = FakeLLM()
        # Turn 1: request tool call
        llm.enqueue(
            Message(
                role="assistant",
                content="Let me run echo",
                tool_call=ToolCall(tool_name="echo", arguments={"text": "hello"}),
            )
        )
        # Turn 2: final answer
        llm.enqueue(Message(role="assistant", content="Echo said: hello"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())

        # Register a trivial echo tool
        echo_def = ToolDef(
            name="echo",
            description="echo text back",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
            semantic_tags=["echo"],
        )
        agent.hippo.register_tool(echo_def)
        agent.executor.register("echo", _echo_tool)

        result = await agent.run("echo hello")
        assert result == "Echo said: hello"

        # Verify the tool result was fed back into the conversation
        assert len(llm.calls) == 2
        second_call_msgs = llm.calls[1]["messages"]
        tool_msgs = [m for m in second_call_msgs if m.role == "tool"]
        assert len(tool_msgs) >= 1
        assert "hello" in tool_msgs[0].content

    @pytest.mark.asyncio
    async def test_step_recorded_in_history(self) -> None:
        """Verify the tool-call step is recorded."""
        llm = FakeLLM()
        llm.enqueue(
            Message(
                role="assistant",
                content="calling echo",
                tool_call=ToolCall(tool_name="echo", arguments={"text": "hi"}),
            )
        )
        llm.enqueue(Message(role="assistant", content="done"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        agent.hippo.register_tool(
            ToolDef(
                name="echo",
                description="echo",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        )
        agent.executor.register("echo", _echo_tool)

        await agent.run("echo hi")

        tasks = list(agent.hippo.history._tasks.values())
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == "completed"
        steps = await agent.hippo.history.get_steps(task.task_id)
        assert len(steps) == 1
        assert steps[0].tool_call is not None
        assert steps[0].tool_call.tool_name == "echo"


class TestBlockedToolCall:
    """Checker blocks a dangerous tool call — agent must fail cleanly."""

    @pytest.mark.asyncio
    async def test_blocked_shell_command(self) -> None:
        """rm -rf / must be blocked by the checker."""
        llm = FakeLLM()
        llm.enqueue(
            Message(
                role="assistant",
                content="deleting",
                tool_call=ToolCall(
                    tool_name="shell_exec",
                    arguments={"command": "rm -rf /"},
                ),
            )
        )
        # After blocked result, LLM provides final answer
        llm.enqueue(Message(role="assistant", content="aborted"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        result = await agent.run("delete everything")

        # The built-in shell_exec is registered, but rm -rf / is blocked.
        # The agent should still produce a result (the second LLM turn).
        assert result == "aborted"

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self) -> None:
        """Calling an unregistered tool must return an error result."""
        llm = FakeLLM()
        llm.enqueue(
            Message(
                role="assistant",
                content="try unknown",
                tool_call=ToolCall(
                    tool_name="nonexistent_tool",
                    arguments={},
                ),
            )
        )
        llm.enqueue(Message(role="assistant", content="fallback"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        result = await agent.run("do something")
        assert result == "fallback"


class TestNoEmbeddingProvider:
    """Agent with embedding=None (online mode without local embedding)."""

    @pytest.mark.asyncio
    async def test_runs_without_long_term_memory(self) -> None:
        agent = _make_agent(
            [Message(role="assistant", content="answer")],
            embedding=None,
        )
        result = await agent.run("question")
        assert result == "answer"

    @pytest.mark.asyncio
    async def test_hippo_retrieve_returns_empty(self) -> None:
        """retrieve_context() returns no long-term results without embedding."""
        hippo = Hippo(embedding=None)
        hippo.short.append(Message(role="user", content="hello"))
        long_ctx = await hippo.retrieve_context("hello")
        assert long_ctx == []


class TestPromptAssembly:
    """Verify short-term window is not injected twice into the prompt."""

    @pytest.mark.asyncio
    async def test_no_duplicate_short_term(self) -> None:
        """Short-term messages must appear exactly once in the LLM call."""
        llm = FakeLLM([Message(role="assistant", content="ok")])
        agent = Agent(llm=llm, embedding=FakeEmbedding())
        await agent.run("unique_query_marker")

        # Inspect the messages sent to the LLM
        assert len(llm.calls) == 1
        sent = llm.calls[0]["messages"]
        user_msgs = [m for m in sent if m.role == "user"]
        matches = [m for m in user_msgs if "unique_query_marker" in m.content]
        assert (
            len(matches) == 1
        ), f"Expected user query exactly once, found {len(matches)}"


class TestCheckedMemoryWrite:
    """Memory writes must be routed through Checker.pre_memory_write."""

    @pytest.mark.asyncio
    async def test_huge_summary_blocked(self) -> None:
        """A step summary exceeding MAX_OUTPUT_LEN must not be memorized."""
        llm = FakeLLM()
        # Tool call that produces a huge output
        llm.enqueue(
            Message(
                role="assistant",
                content="x" * 60_000,  # huge thought
                tool_call=ToolCall(tool_name="echo", arguments={"text": "hi"}),
            )
        )
        llm.enqueue(Message(role="assistant", content="done"))

        agent = Agent(llm=llm, embedding=FakeEmbedding())
        agent.hippo.register_tool(
            ToolDef(
                name="echo",
                description="echo",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        )
        agent.executor.register("echo", _echo_tool)

        result = await agent.run("test")
        assert result == "done"

        # The huge summary should have been blocked by pre_memory_write,
        # so long-term memory should be empty.
        assert len(agent.hippo.long._entries) == 0


class TestRetrievalSeparation:
    """Retrieval results must not include tool resolution artefacts."""

    @pytest.mark.asyncio
    async def test_retrieve_context_returns_only_long_term(self) -> None:
        """retrieve_context must not include short-term messages."""
        emb = FakeEmbedding()
        hippo = Hippo(embedding=emb)

        # Populate short-term
        hippo.short.append(Message(role="user", content="short term msg"))

        # Populate long-term
        await hippo.long.add("long term entry", [1.0, 0.0, 0.0, 0.0])

        long_ctx = await hippo.retrieve_context("anything")

        # Should contain only the long-term entry
        assert len(long_ctx) == 1
        assert "[memory]" in long_ctx[0].content
        assert "long term entry" in long_ctx[0].content

        # Must NOT contain the short-term message
        contents = " ".join(m.content for m in long_ctx)
        assert "short term msg" not in contents

    @pytest.mark.asyncio
    async def test_extract_tool_hints_is_separate(self) -> None:
        """extract_tool_hints is a pure text scan, separate from retrieval."""
        hippo = Hippo(embedding=None)
        hippo.register_tool(
            ToolDef(name="shell_exec", description="run shell", parameters={})
        )

        msgs = [
            Message(role="system", content="[memory] used shell_exec before"),
            Message(role="user", content="run a command"),
        ]
        hints = hippo.extract_tool_hints(msgs)
        assert "shell_exec" in hints


# ── Test helper ─────────────────────────────────────────────────────────────


async def _echo_tool(text: str) -> str:
    return text
