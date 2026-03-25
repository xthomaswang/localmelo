from __future__ import annotations

import os
from typing import TYPE_CHECKING

from localmelo.melo.agent.chat import Chat
from localmelo.melo.checker import Checker
from localmelo.melo.checker.payloads import (
    ExecutorResultPayload,
    MemoryWritePayload,
    ToolResolutionResult,
)
from localmelo.melo.contracts.providers import BaseEmbeddingProvider, BaseLLMProvider
from localmelo.melo.executor import Executor, register_builtins
from localmelo.melo.executor.models import ExecutionRequest
from localmelo.melo.memory.coordinator import Hippo
from localmelo.melo.schema import (
    MAX_AGENT_STEPS,
    Message,
    StepRecord,
    TaskRecord,
    ToolResult,
)

if TYPE_CHECKING:
    from localmelo.support.config import Config


def _providers_from_config(
    cfg: Config,
) -> tuple[BaseLLMProvider, BaseEmbeddingProvider | None]:
    """Build LLM and embedding providers from a Config object."""
    from localmelo.support.providers.embedding.openai_compat import (
        OpenAICompatEmbedding,
    )
    from localmelo.support.providers.llm.openai_compat import OpenAICompatLLM

    llm: BaseLLMProvider
    embedding: BaseEmbeddingProvider | None

    if cfg.backend == "mlc-llm":
        base_url = f"http://127.0.0.1:{cfg.mlc.chat_port}/v1"
        llm = OpenAICompatLLM(base_url=base_url, model=cfg.mlc.chat_model)
        embedding = OpenAICompatEmbedding(
            base_url=base_url, model=cfg.mlc.embedding_model
        )

    elif cfg.backend == "ollama":
        chat_url = cfg.ollama.chat_url.rstrip("/") + "/v1"
        llm = OpenAICompatLLM(base_url=chat_url, model=cfg.ollama.chat_model)

        if cfg.ollama.embedding_model:
            emb_base = cfg.ollama.embedding_url or cfg.ollama.chat_url
            emb_url = emb_base.rstrip("/") + "/v1"
            embedding = OpenAICompatEmbedding(
                base_url=emb_url, model=cfg.ollama.embedding_model
            )
        else:
            emb_url = f"http://127.0.0.1:{cfg.mlc.chat_port}/v1"
            embedding = OpenAICompatEmbedding(
                base_url=emb_url, model="qwen3-embedding-0.6b"
            )

    elif cfg.backend == "online":
        api_base = {
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "anthropic": "https://api.anthropic.com/v1",
        }
        base_url = api_base.get(cfg.online.provider, "")
        api_key = os.environ.get(cfg.online.api_key_env, "")
        llm = OpenAICompatLLM(
            base_url=base_url, model=cfg.online.chat_model, api_key=api_key
        )

        if cfg.online.local_embedding:
            emb_url = f"http://127.0.0.1:{cfg.mlc.chat_port}/v1"
            embedding = OpenAICompatEmbedding(
                base_url=emb_url, model="qwen3-embedding-0.6b"
            )
        else:
            embedding = None

    else:
        raise ValueError(f"Unknown backend: {cfg.backend!r}")

    return llm, embedding


class Agent:
    """Task-solving agent with tool use and memory.

    Three construction modes:

    1. **Config-based** (gateway / full app)::

        from localmelo.support.config import load
        agent = Agent(config=load())

    2. **Direct provider injection** (testing / custom setups)::

        agent = Agent(llm=my_llm_provider, embedding=my_emb_provider)

    3. **Legacy raw strings** (backward compat)::

        agent = Agent(base_url="http://...", chat_model="...", embed_model="...")
    """

    def __init__(
        self,
        # Config-based construction
        config: Config | None = None,
        # Legacy raw-string construction (backward compat)
        base_url: str | None = None,
        chat_model: str | None = None,
        embed_model: str | None = None,
        *,
        # Direct provider injection
        llm: BaseLLMProvider | None = None,
        embedding: BaseEmbeddingProvider | None = None,
    ) -> None:
        if llm is not None:
            self._llm = llm
            self._embedding = embedding

        elif config is not None:
            self._llm, self._embedding = _providers_from_config(config)

        else:
            from localmelo.melo.schema import CHAT_MODEL, EMBEDDING_MODEL, LLM_BASE_URL
            from localmelo.support.providers.embedding.openai_compat import (
                OpenAICompatEmbedding,
            )
            from localmelo.support.providers.llm.openai_compat import OpenAICompatLLM

            self._llm = OpenAICompatLLM(
                base_url=base_url or LLM_BASE_URL,
                model=chat_model or CHAT_MODEL,
            )
            self._embedding = OpenAICompatEmbedding(
                base_url=base_url or LLM_BASE_URL,
                model=embed_model or EMBEDDING_MODEL,
            )

        # Optional persistent memory backends (env-based opt-in).
        # LOCALMELO_PERSIST_MEMORY=1 enables SQLite-backed history and long-term.
        # LOCALMELO_MEMORY_DIR overrides the default storage directory.
        history_backend = None
        long_backend = None
        if os.environ.get("LOCALMELO_PERSIST_MEMORY"):
            from localmelo.melo.memory.history.sqlite import SqliteHistory
            from localmelo.melo.memory.long.sqlite import SqliteLongTerm

            mem_dir = os.environ.get(
                "LOCALMELO_MEMORY_DIR",
                os.path.expanduser("~/.cache/localmelo/memory"),
            )
            os.makedirs(mem_dir, exist_ok=True)
            history_backend = SqliteHistory(os.path.join(mem_dir, "history.db"))
            if self._embedding:
                long_backend = SqliteLongTerm(os.path.join(mem_dir, "long_term.db"))

        self.hippo = Hippo(
            embedding=self._embedding,
            history=history_backend,
            long=long_backend,
        )
        self.checker = Checker()
        self.executor = Executor(self.hippo, self.checker)
        self.chat = Chat(self._llm)

        register_builtins(self.executor, self.hippo)

    async def run(self, query: str) -> str:
        task = TaskRecord(query=query)
        await self.hippo.save_task(task)

        self.hippo.short.append(Message(role="user", content=query))

        for _ in range(MAX_AGENT_STEPS):
            # ── Stage 1: Retrieval ──
            # Long-term memory search (embedding similarity).
            # Short-term window is fetched separately — never merged
            # into retrieval results — to avoid injecting it twice
            # when building the prompt.
            long_context = await self.hippo.retrieve_context(query)
            short_window = self.hippo.short.get_window()

            # ── Stage 2: Tool resolution ──
            # Extract tool hints from the combined context, then
            # resolve full ToolDefs via BM25.  No tools are executed
            # at this stage.
            tool_hints = self.hippo.extract_tool_hints(long_context + short_window)
            tools = self.hippo.resolve_tools(query, hints=tool_hints)

            # ── Tool resolution check (v0.2) ──
            resolution_check = self.checker.check_tool_resolution(
                ToolResolutionResult(
                    query=query,
                    hints=tool_hints,
                    resolved_tool_names=[t.name for t in tools],
                )
            )
            if not resolution_check.allowed:
                task.status = "failed"
                task.result = f"Tool resolution failed: {resolution_check.reason}"
                break

            # ── Pre-plan check ──
            all_msgs = long_context + short_window
            check = await self.checker.pre_plan(all_msgs)
            if not check.allowed:
                task.status = "failed"
                task.result = f"Plan check failed: {check.reason}"
                break

            # ── Stage 3: Plan ──
            # context and short are disjoint — no duplication.
            response = await self.chat.plan_step(
                context=long_context,
                short=short_window,
                tools=tools,
                query=query,
            )

            # ── Post-plan check ──
            check = await self.checker.post_plan(response)
            if not check.allowed:
                task.status = "failed"
                task.result = f"Response check failed: {check.reason}"
                break

            # No tool call → final answer
            if response.tool_call is None:
                task.status = "completed"
                task.result = response.content
                break

            # ── Stage 4: Execute (structured, v0.2) ──
            exec_request = ExecutionRequest(
                tool_name=response.tool_call.tool_name,
                arguments=dict(response.tool_call.arguments),
            )
            outcome = await self.executor.execute_structured(exec_request)

            # Validate executor result (v0.2)
            exec_result_check = self.checker.check_executor_result(
                ExecutorResultPayload(
                    tool_name=outcome.tool_name,
                    output=outcome.output,
                    error=outcome.error,
                    duration_ms=outcome.duration_ms,
                )
            )
            if exec_result_check.sanitized_payload is not None:
                sp = exec_result_check.sanitized_payload
                result = ToolResult(
                    tool_name=sp.tool_name,
                    output=sp.output,
                    error=sp.error,
                    duration_ms=sp.duration_ms,
                )
            else:
                result = outcome.to_tool_result()

            # ── Stage 5: Store & memorize (checked via v0.2) ──
            step = StepRecord(
                thought=response.content,
                tool_call=response.tool_call,
                tool_result=result,
            )
            summary = await self.hippo.store_step(task.task_id, step)

            # Checked write (v0.2): step summary → short + long memory
            mem_check = self.checker.check_memory_write(
                MemoryWritePayload(
                    text=summary,
                    role="assistant",
                    metadata={"step_id": step.step_id},
                )
            )
            if mem_check.allowed:
                await self.hippo.memorize(summary, metadata={"step_id": step.step_id})

            # Checked write (v0.2): tool result → short memory
            output = result.error if result.error else result.output
            tool_msg = f"[{result.tool_name}] {output}"
            tool_mem_check = self.checker.check_memory_write(
                MemoryWritePayload(text=tool_msg, role="tool")
            )
            if tool_mem_check.allowed:
                self.hippo.short.append(Message(role="tool", content=tool_msg))
        else:
            task.status = "failed"
            task.result = "Max steps reached"

        await self.hippo.save_task(task)
        return task.result

    async def close(self) -> None:
        self.hippo.close()
        await self._llm.close()
        if self._embedding:
            await self._embedding.close()
