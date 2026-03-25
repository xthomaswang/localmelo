from __future__ import annotations

import json
from typing import Any

import httpx

from localmelo.melo.contracts.providers import BaseLLMProvider
from localmelo.melo.schema import Message, ToolCall, ToolDef


def _tool_def_to_openai(td: ToolDef) -> dict[str, Any]:
    """Convert internal ToolDef to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": td.name,
            "description": td.description,
            "parameters": td.parameters,
        },
    }


class OpenAICompatLLM(BaseLLMProvider):
    """OpenAI-compatible chat/completion provider.

    Works with any backend that exposes ``POST /chat/completions``
    in the OpenAI format:

    - **Local**: MLC LLM, Ollama, vLLM, LMStudio, llama.cpp server
    - **Online**: OpenAI, Groq, Together AI, Fireworks, DeepSeek, Mistral

    Parameters
    ----------
    base_url : str
        Server base URL **including** the ``/v1`` prefix,
        e.g. ``"http://localhost:11434/v1"`` or ``"https://api.openai.com/v1"``.
    model : str
        Model identifier sent in the ``"model"`` field.
    api_key : str | None
        Bearer token for authenticated endpoints.
    timeout : float
        HTTP timeout in seconds (default 120).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        **kwargs: Any,
    ) -> None:
        self.model = model
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        payload: dict[str, Any] = {
            "model": self.model,
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

    async def close(self) -> None:
        await self._http.aclose()
