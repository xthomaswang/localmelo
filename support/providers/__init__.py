"""Provider registry and factory functions.

Usage
-----
::

    from localmelo.support.providers import create_llm, create_embedding
    from localmelo.support.config import Config

    # Direct creation
    llm = create_llm("openai_compat", base_url="http://localhost:11434/v1", model="llama3.2")
    emb = create_embedding("openai_compat", base_url="http://localhost:8323/v1", model="bge-base")

    # From config
    cfg = ProviderConfig("openai_compat", "https://api.openai.com/v1", "gpt-4o", api_key="sk-...")
    llm = create_llm_from_config(cfg)
"""

from __future__ import annotations

from typing import Any

from localmelo.melo.contracts.providers import BaseEmbeddingProvider, BaseLLMProvider

from .embedding.openai_compat import OpenAICompatEmbedding
from .llm.openai_compat import OpenAICompatLLM

_LLM_REGISTRY: dict[str, type[BaseLLMProvider]] = {}
_EMBEDDING_REGISTRY: dict[str, type[BaseEmbeddingProvider]] = {}


# ── Registration ──


def register_llm(name: str, cls: type[BaseLLMProvider]) -> None:
    """Register an LLM provider class under the given name."""
    _LLM_REGISTRY[name] = cls


def register_embedding(name: str, cls: type[BaseEmbeddingProvider]) -> None:
    """Register an embedding provider class under the given name."""
    _EMBEDDING_REGISTRY[name] = cls


def list_llm_providers() -> list[str]:
    return list(_LLM_REGISTRY)


def list_embedding_providers() -> list[str]:
    return list(_EMBEDDING_REGISTRY)


# ── Factory ──


def create_llm(provider: str, **kwargs: Any) -> BaseLLMProvider:
    """Create an LLM provider by registered name."""
    if provider not in _LLM_REGISTRY:
        raise ValueError(
            f"Unknown LLM provider: {provider!r}. " f"Available: {list(_LLM_REGISTRY)}"
        )
    return _LLM_REGISTRY[provider](**kwargs)


def create_embedding(provider: str, **kwargs: Any) -> BaseEmbeddingProvider:
    """Create an embedding provider by registered name."""
    if provider not in _EMBEDDING_REGISTRY:
        raise ValueError(
            f"Unknown embedding provider: {provider!r}. "
            f"Available: {list(_EMBEDDING_REGISTRY)}"
        )
    return _EMBEDDING_REGISTRY[provider](**kwargs)


def create_llm_from_config(config: Any) -> BaseLLMProvider:
    """Create an LLM provider from a ProviderConfig dataclass."""
    return create_llm(
        config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        timeout=config.timeout,
        **config.extra,
    )


def create_embedding_from_config(config: Any) -> BaseEmbeddingProvider:
    """Create an embedding provider from a ProviderConfig dataclass."""
    return create_embedding(
        config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        timeout=config.timeout,
        **config.extra,
    )


# ── Auto-register built-in providers ──

register_llm("openai_compat", OpenAICompatLLM)
register_embedding("openai_compat", OpenAICompatEmbedding)

__all__ = [
    "BaseLLMProvider",
    "BaseEmbeddingProvider",
    "register_llm",
    "register_embedding",
    "create_llm",
    "create_embedding",
    "create_llm_from_config",
    "create_embedding_from_config",
    "list_llm_providers",
    "list_embedding_providers",
    "OpenAICompatLLM",
    "OpenAICompatEmbedding",
]
