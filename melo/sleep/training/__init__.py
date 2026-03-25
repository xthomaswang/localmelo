from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrainingArtifacts:
    """Metadata emitted by the sleep-time training stage."""

    adapter_version: str = "draft"
    checkpoint_path: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class SleepTrainer:
    """Minimal sleep-time trainer skeleton.

    The concrete implementation is expected to run LoRA/QLoRA style adapter
    training using the datasets produced by ``sleep.preprocess``.
    """

    def train(
        self,
        artifacts: Any,
    ) -> TrainingArtifacts:
        sample_count = len(getattr(artifacts, "training_samples", []))
        return TrainingArtifacts(
            metadata={"training_sample_count": sample_count},
        )


__all__ = [
    "SleepTrainer",
    "TrainingArtifacts",
]
