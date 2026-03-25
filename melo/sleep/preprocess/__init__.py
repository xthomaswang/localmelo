from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from localmelo.melo.memory.personalized import PersonalizedSample


@dataclass
class PreprocessArtifacts:
    """Prepared datasets produced before sleep-time training."""

    dataset_version: str = "draft"
    training_samples: list[dict[str, Any]] = field(default_factory=list)
    evaluation_samples: list[dict[str, Any]] = field(default_factory=list)
    synthetic_questions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class SleepPreprocessor:
    """Build sleep-time datasets from personalized memory.

    This is intentionally a minimal no-op skeleton. Concrete logic can later
    add cleaning, deduplication, scoring, synthetic question generation, and
    train/eval splitting.
    """

    def build(
        self,
        personalized_samples: list[PersonalizedSample],
    ) -> PreprocessArtifacts:
        training_samples = [
            {
                "input_text": sample.input_text,
                "target_text": sample.target_text,
                "signal": sample.signal,
                "metadata": dict(sample.metadata),
            }
            for sample in personalized_samples
        ]
        return PreprocessArtifacts(
            training_samples=training_samples,
            metadata={"source_sample_count": len(personalized_samples)},
        )


__all__ = [
    "PreprocessArtifacts",
    "SleepPreprocessor",
]
