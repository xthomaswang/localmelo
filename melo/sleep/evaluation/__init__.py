from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationReport:
    """Result of evaluating a sleep-trained personalized model."""

    passed: bool = False
    personalized_score: float = 0.0
    regression_score: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class SleepEvaluator:
    """Minimal evaluator skeleton for sleep-time personalization."""

    def evaluate(
        self,
        artifacts: Any,
        training: Any,
    ) -> EvaluationReport:
        eval_count = len(getattr(artifacts, "evaluation_samples", []))
        metrics = dict(getattr(training, "metrics", {}))
        return EvaluationReport(
            metrics=metrics,
            metadata={"evaluation_sample_count": eval_count},
        )


__all__ = [
    "EvaluationReport",
    "SleepEvaluator",
]
