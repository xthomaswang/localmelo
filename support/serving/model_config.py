"""Model configuration registry for local MLC-LLM serving.

Each ``ModelEntry`` describes one compiled model: its path, library,
device target, and the port it should be served on.

``ServingConfig`` groups entries by type ("chat" / "embedding") and
provides filtering helpers used by the server launcher.

Path convention
---------------
All model paths are derived from ``localmelo.support.models`` — the same
location used by the compile pipeline and the gateway.  This ensures that
``python -m localmelo.support.serving.server`` finds the same compiled
models that onboarding/gateway produce.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModelEntry:
    """A single compiled model to serve."""

    name: str
    model_dir: str
    model_lib: str
    device: str  # "metal", "cuda", "vulkan", "cpu"
    port: int
    model_type: str = "embedding"  # "chat" | "embedding"


@dataclass
class ServingConfig:
    """Collection of models to serve."""

    models: list[ModelEntry] = field(default_factory=list)
    host: str = "127.0.0.1"

    def filter(
        self,
        model_type: str | None = None,
        names: list[str] | None = None,
    ) -> list[ModelEntry]:
        """Return models matching the given type and/or names."""
        result = self.models
        if model_type:
            result = [m for m in result if m.model_type == model_type]
        if names:
            result = [m for m in result if m.name in names]
        return result


# ── Path resolution ──
#
# The single source of truth for compiled model paths is
# ``localmelo.support.models`` (MODELS_DIR / EMBED_DIR).


def models_base() -> Path:
    """Return the base directory that contains ``chat/`` and ``embedding/`` subdirs.

    This is ``localmelo/support/models/`` — the same directory used by the
    compile pipeline (``support.models.compiled_dir``) and the gateway.
    """
    return Path(os.path.dirname(os.path.dirname(__file__))) / "models"


# ── Default configuration ──


def _detect_device() -> str:
    """Return the default device for the current platform."""
    import sys

    return "metal" if sys.platform == "darwin" else "cuda"


def default_config() -> ServingConfig:
    """Return the default serving config for this machine.

    Paths are derived from ``support.models`` so that compiled models
    produced by onboarding are found by the standalone serving CLI.
    """
    from localmelo.support.models import (
        CHAT_MODELS,
        DEFAULT_EMBEDDING,
        compiled_dir,
        dylib_path,
    )

    device = _detect_device()
    models: list[ModelEntry] = []

    # Qwen3-Embedding (compiled via onboarding)
    models.append(
        ModelEntry(
            name=DEFAULT_EMBEDDING.name,
            model_dir=compiled_dir(DEFAULT_EMBEDDING),
            model_lib=dylib_path(DEFAULT_EMBEDDING),
            device=device,
            port=8324,
            model_type="embedding",
        )
    )

    # Default chat model (smallest — Qwen3-1.7B)
    if CHAT_MODELS:
        chat = CHAT_MODELS[0]
        models.append(
            ModelEntry(
                name=chat.name,
                model_dir=compiled_dir(chat),
                model_lib=dylib_path(chat),
                device=device,
                port=8400,
                model_type="chat",
            )
        )

    return ServingConfig(models=models)
