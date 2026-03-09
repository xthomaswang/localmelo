from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Entry:
    text: str
    vector: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)


class LongTerm:
    def __init__(self) -> None:
        self._entries: list[Entry] = []

    async def add(
        self,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        self._entries.append(Entry(text=text, vector=vec, metadata=metadata or {}))

    async def search(
        self, query_embedding: list[float], top_k: int = 5
    ) -> list[tuple[str, float, dict[str, Any]]]:
        if not self._entries:
            return []

        q = np.asarray(query_embedding, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return []
        q = q / q_norm

        scores: list[tuple[int, float]] = []
        for i, entry in enumerate(self._entries):
            e_norm = np.linalg.norm(entry.vector)
            if e_norm == 0:
                continue
            sim = float(np.dot(q, entry.vector / e_norm))
            scores.append((i, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [
            (self._entries[i].text, sim, self._entries[i].metadata)
            for i, sim in scores[:top_k]
        ]
