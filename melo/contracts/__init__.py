"""Contracts (abstract interfaces) that support/ implementations must satisfy."""

from localmelo.melo.contracts.providers import BaseEmbeddingProvider, BaseLLMProvider

__all__ = [
    "BaseEmbeddingProvider",
    "BaseLLMProvider",
]
