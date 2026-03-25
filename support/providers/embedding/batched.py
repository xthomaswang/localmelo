from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from localmelo.melo.contracts.providers import BaseEmbeddingProvider


@dataclass
class BatchStats:
    """Accumulates batching statistics."""

    total_requests: int = 0
    total_texts: int = 0
    total_batches: int = 0
    requests_per_batch: list[int] = field(default_factory=list)
    texts_per_batch: list[int] = field(default_factory=list)

    @property
    def reduction_ratio(self) -> float:
        if self.total_batches == 0:
            return 1.0
        return self.total_requests / self.total_batches


@dataclass
class _PendingRequest:
    texts: list[str]
    future: asyncio.Future[list[list[float]]]


class BatchedEmbedding(BaseEmbeddingProvider):
    """Decorator that adds request batching to any embedding provider.

    Collects concurrent ``embed()`` calls within a time window and merges
    them into a single call to the underlying provider, then distributes
    results back to each caller.

    This is useful when multiple agent tasks (or multiple LLM instances)
    share an embedding endpoint — the coordinator reduces round-trips.

    Parameters
    ----------
    provider : BaseEmbeddingProvider
        The underlying embedding provider to batch calls to.
    batch_window_ms : float
        Time in milliseconds to wait for more requests before flushing
        (default 5ms).

    Example
    -------
    ::

        base = OpenAICompatEmbedding(base_url="...", model="bge-base")
        batched = BatchedEmbedding(base, batch_window_ms=10)

        # concurrent calls get merged:
        emb1, emb2 = await asyncio.gather(
            batched.embed(["hello"]),
            batched.embed(["world"]),
        )
    """

    def __init__(
        self,
        provider: BaseEmbeddingProvider,
        batch_window_ms: float = 5.0,
    ) -> None:
        self._provider = provider
        self._window_s = batch_window_ms / 1000
        self._pending: list[_PendingRequest] = []
        self._lock = asyncio.Lock()
        self._timer_running = False
        self.stats = BatchStats()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[list[float]]] = loop.create_future()

        async with self._lock:
            self._pending.append(_PendingRequest(texts, future))
            self.stats.total_requests += 1
            self.stats.total_texts += len(texts)

            if not self._timer_running:
                self._timer_running = True
                asyncio.create_task(self._batch_timer())

        return await future

    async def _batch_timer(self) -> None:
        await asyncio.sleep(self._window_s)
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                self._timer_running = False
                return
            batch = self._pending.copy()
            self._pending.clear()
            self._timer_running = False

        # Merge all texts, tracking slice boundaries
        all_texts: list[str] = []
        slices: list[tuple[int, int]] = []
        for req in batch:
            slices.append((len(all_texts), len(req.texts)))
            all_texts.extend(req.texts)

        self.stats.total_batches += 1
        self.stats.requests_per_batch.append(len(batch))
        self.stats.texts_per_batch.append(len(all_texts))

        try:
            embeddings = await self._provider.embed(all_texts)
            for (start, count), req in zip(slices, batch, strict=False):
                if not req.future.done():
                    req.future.set_result(embeddings[start : start + count])
        except Exception as e:
            for req in batch:
                if not req.future.done():
                    req.future.set_exception(e)

    async def close(self) -> None:
        await self._flush()
        await self._provider.close()
