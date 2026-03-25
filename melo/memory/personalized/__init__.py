from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PersonalizedSample:
    """Training-only personalization sample.

    These samples are not queried during normal online inference. They are
    collected during runtime and later consumed by the sleep pipeline.
    """

    input_text: str
    target_text: str = ""
    signal: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class PersonalizedMemory:
    """Append-only store for sleep-time personalization samples."""

    def __init__(self) -> None:
        self._samples: list[PersonalizedSample] = []

    def add(self, sample: PersonalizedSample) -> None:
        self._samples.append(sample)

    def extend(self, samples: list[PersonalizedSample]) -> None:
        self._samples.extend(samples)

    def list_all(self) -> list[PersonalizedSample]:
        return list(self._samples)

    def clear(self) -> None:
        self._samples.clear()


__all__ = [
    "PersonalizedMemory",
    "PersonalizedSample",
]
