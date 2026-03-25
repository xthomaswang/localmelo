"""Sleep-mode pipeline for offline personalization.

This package groups the components used when a user explicitly enters sleep
mode: preprocessing, training, evaluation, and state tracking.
"""

from localmelo.melo.sleep.evaluation import EvaluationReport, SleepEvaluator
from localmelo.melo.sleep.preprocess import PreprocessArtifacts, SleepPreprocessor
from localmelo.melo.sleep.state import SleepStage, SleepState, SleepStateStore
from localmelo.melo.sleep.training import SleepTrainer, TrainingArtifacts

__all__ = [
    "EvaluationReport",
    "PreprocessArtifacts",
    "SleepEvaluator",
    "SleepPreprocessor",
    "SleepStage",
    "SleepState",
    "SleepStateStore",
    "SleepTrainer",
    "TrainingArtifacts",
]
