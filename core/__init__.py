from __future__ import annotations

from ..checker import Checker
from ..executor import Executor, register_builtins
from ..schema import MAX_AGENT_STEPS, Message, StepRecord, TaskRecord
from .chat import Chat
from .gateway import Gateway
from .llm import LLMClient


class Agent:
    def __init__(
        self,
        base_url: str | None = None,
        chat_model: str | None = None,
        embed_model: str | None = None,
    ) -> None:
        from ..schema import CHAT_MODEL, EMBEDDING_MODEL, LLM_BASE_URL

        self.llm = LLMClient(
            base_url=base_url or LLM_BASE_URL,
            chat_model=chat_model or CHAT_MODEL,
            embed_model=embed_model or EMBEDDING_MODEL,
        )
        self.gateway = Gateway(self.llm)
        self.checker = Checker()
        self.executor = Executor(self.gateway, self.checker)
        self.chat = Chat(self.llm)

        register_builtins(self.executor, self.gateway)

    async def run(self, query: str) -> str:
        task = TaskRecord(query=query)
        await self.gateway.save_task(task)

        self.gateway.short.append(Message(role="user", content=query))

        for _ in range(MAX_AGENT_STEPS):
            # Level 1: retrieve context + tool hints
            context, tool_hints = await self.gateway.retrieve_context(query)

            # Level 2: resolve tools from hints + query
            tools = self.gateway.resolve_tools(query, hints=tool_hints)

            # pre-check
            all_msgs = context + self.gateway.short.get_window()
            check = await self.checker.pre_plan(all_msgs)
            if not check.allowed:
                task.status = "failed"
                task.result = f"Plan check failed: {check.reason}"
                break

            # plan
            response = await self.chat.plan_step(
                context=context,
                short=self.gateway.short.get_window(),
                tools=tools,
                query=query,
            )

            # post-check
            check = await self.checker.post_plan(response)
            if not check.allowed:
                task.status = "failed"
                task.result = f"Response check failed: {check.reason}"
                break

            # no tool call → final answer
            if response.tool_call is None:
                task.status = "completed"
                task.result = response.content
                break

            # execute tool
            result = await self.executor.execute(response.tool_call)

            # record step
            step = StepRecord(
                thought=response.content,
                tool_call=response.tool_call,
                tool_result=result,
            )
            await self.gateway.store_step(task.task_id, step)

            # feed tool result back into short memory
            output = result.error if result.error else result.output
            self.gateway.short.append(
                Message(role="tool", content=f"[{result.tool_name}] {output}")
            )
        else:
            task.status = "failed"
            task.result = "Max steps reached"

        await self.gateway.save_task(task)
        return task.result

    async def close(self) -> None:
        await self.llm.close()
