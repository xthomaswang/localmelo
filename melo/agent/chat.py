from __future__ import annotations

from localmelo.melo.contracts.providers import BaseLLMProvider
from localmelo.melo.schema import Message, ToolDef

SYSTEM_PROMPT = (
    "You are a task-solving agent. You can use tools to accomplish tasks.\n"
    "When you need to use a tool, respond with a tool call.\n"
    "When the task is complete, respond with your final answer directly.\n"
    "Think step by step. Be concise."
)


class Chat:
    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def plan_step(
        self,
        context: list[Message],
        short: list[Message],
        tools: list[ToolDef],
        query: str,
    ) -> Message:
        messages = [Message(role="system", content=SYSTEM_PROMPT)]
        messages.extend(context)
        messages.extend(short)
        if not any(m.role == "user" for m in short):
            messages.append(Message(role="user", content=query))

        return await self.llm.chat(messages, tools=tools or None)
