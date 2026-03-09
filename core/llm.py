from __future__ import annotations

import json
from typing import Any

import httpx

from ..schema import (
    CHAT_MODEL,
    EMBEDDING_MODEL,
    LLM_BASE_URL,
    Message,
    ToolCall,
    ToolDef,
)


def _tool_def_to_openai(td: ToolDef) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": td.name,
            "description": td.description,
            "parameters": td.parameters,
        },
    }


class LLMClient:
    def __init__(
        self,
        base_url: str = LLM_BASE_URL,
        chat_model: str = CHAT_MODEL,
        embed_model: str = EMBEDDING_MODEL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if tools:
            payload["tools"] = [_tool_def_to_openai(t) for t in tools]
            payload["tool_choice"] = "auto"

        resp = await self._http.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]

        tool_call = None
        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]
            fn = tc["function"]
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                args = json.loads(args)
            tool_call = ToolCall(tool_name=fn["name"], arguments=args)

        return Message(
            role="assistant",
            content=msg.get("content") or "",
            tool_call=tool_call,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.embed_model, "input": texts}
        resp = await self._http.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]

    async def close(self) -> None:
        await self._http.aclose()
