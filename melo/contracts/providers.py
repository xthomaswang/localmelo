from __future__ import annotations

from abc import ABC, abstractmethod

from localmelo.melo.schema import Message, ToolDef


class BaseLLMProvider(ABC):
    """Abstract interface for chat/completion model backends.

    Any backend that can handle chat messages and (optionally) tool calls
    should subclass this.  One implementation (OpenAICompatLLM) covers all
    backends that expose the OpenAI ``/chat/completions`` format: MLC LLM,
    Ollama, vLLM, LMStudio, OpenAI, Groq, Together AI, etc.
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        """Send messages and return the assistant response."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying resources (HTTP connections, etc.)."""


class BaseEmbeddingProvider(ABC):
    """Abstract interface for embedding model backends.

    Any backend that can turn text into dense vectors should subclass this.
    """

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying resources."""
