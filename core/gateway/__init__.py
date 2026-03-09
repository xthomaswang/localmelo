from __future__ import annotations

import re

from ...memory.history import History
from ...memory.long import LongTerm
from ...memory.short import ShortTerm
from ...memory.tools import ToolRegistry
from ...schema import (
    LONG_TERM_TOP_K,
    SHORT_TERM_MAX,
    TOOL_SEARCH_TOP_K,
    Message,
    StepRecord,
    TaskRecord,
    ToolDef,
)
from ..llm import LLMClient


class Gateway:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.history = History()
        self.short = ShortTerm(max_len=SHORT_TERM_MAX)
        self.long = LongTerm()
        self.tools = ToolRegistry()

    # ── Tool management ──

    def register_tool(self, tool: ToolDef) -> None:
        self.tools.register(tool)

    def get_tool(self, name: str) -> ToolDef | None:
        return self.tools.get(name)

    def list_tools(self) -> list[ToolDef]:
        return self.tools.list_all()

    # ── Task management ──

    async def save_task(self, task: TaskRecord) -> None:
        await self.history.save_task(task)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self.history.get_task(task_id)

    # ── Step recording ──

    async def store_step(self, task_id: str, step: StepRecord) -> None:
        await self.history.add_step(task_id, step)

        summary = step.thought
        if step.tool_call:
            summary += f" [called {step.tool_call.tool_name}]"
        if step.tool_result:
            output = step.tool_result.output[:200]
            summary += f" -> {output}"

        self.short.append(Message(role="assistant", content=summary))

        embedding = await self.llm.embed([summary])
        await self.long.add(
            text=summary, embedding=embedding[0], metadata={"step_id": step.step_id}
        )

    # ── Level 1: Context Retrieval (embedding over short + long) ──

    async def retrieve_context(self, query: str) -> tuple[list[Message], list[str]]:
        short = self.short.get_window()

        query_emb = await self.llm.embed([query])
        long_results = await self.long.search(query_emb[0], top_k=LONG_TERM_TOP_K)
        long_msgs = [
            Message(role="system", content=f"[memory] {text}")
            for text, _score, _meta in long_results
        ]

        context = long_msgs + short

        # Extract tool hints from retrieved context
        tool_hints: list[str] = []
        all_tool_names = {t.name for t in self.tools.list_all()}
        for msg in context:
            for name in all_tool_names:
                if name in msg.content:
                    tool_hints.append(name)
            for match in re.findall(r"\[called (\w+)\]", msg.content):
                if match not in tool_hints:
                    tool_hints.append(match)

        return context, tool_hints

    # ── Level 2: Tool Resolution (BM25 over registry) ──

    def resolve_tools(
        self, query: str, hints: list[str] | None = None
    ) -> list[ToolDef]:
        results: dict[str, ToolDef] = {}

        for name in hints or []:
            td = self.tools.get(name)
            if td:
                results[td.name] = td

        for td in self.tools.search(query, top_k=TOOL_SEARCH_TOP_K):
            if td.name not in results:
                results[td.name] = td

        return list(results.values())
