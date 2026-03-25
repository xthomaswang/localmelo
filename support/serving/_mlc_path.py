"""Shared helper for locating the ``mlc_llm`` package.

Lookup order
------------
1. Already installed / importable  -- use it directly.
2. Package-local vendored copy at ``localmelo/support/3rdparty/mlc-llm/python``.

The old fallback to a repo-external ``../mlc-llm`` sibling directory is
deliberately removed so that a standalone clone of ``localmelo`` works
without any external dependencies in the parent directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Resolved once at import time.
_VENDORED_MLC_PYTHON: str = str(
    Path(__file__).resolve().parent.parent / "3rdparty" / "mlc-llm" / "python"
)


def ensure_mlc_importable() -> None:
    """Ensure ``import mlc_llm`` will succeed in the current process.

    Adds the vendored path to *sys.path* only when the package is not
    already importable.
    """
    try:
        import mlc_llm  # noqa: F401

        return
    except ImportError:
        pass

    if os.path.isdir(_VENDORED_MLC_PYTHON) and _VENDORED_MLC_PYTHON not in sys.path:
        sys.path.insert(0, _VENDORED_MLC_PYTHON)


def mlc_python_path() -> str | None:
    """Return the filesystem path to the ``mlc_llm`` Python package root.

    Returns ``None`` when ``mlc_llm`` is installed as a regular package
    (no extra path manipulation needed).  Otherwise returns the vendored
    ``support/3rdparty/mlc-llm/python`` directory so callers can pass it
    to subprocesses via ``PYTHONPATH``.
    """
    try:
        import mlc_llm  # noqa: F401

        return None  # already importable natively
    except ImportError:
        pass

    if os.path.isdir(_VENDORED_MLC_PYTHON):
        return _VENDORED_MLC_PYTHON
    return None


def subprocess_env() -> dict[str, str]:
    """Return an ``env`` dict suitable for ``subprocess.run / Popen``.

    If the vendored ``mlc_llm`` path needs to be on ``PYTHONPATH``, it is
    prepended.  Otherwise the current environment is returned unchanged.
    """
    extra = mlc_python_path()
    if extra is None:
        return dict(os.environ)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{extra}{os.pathsep}{existing}" if existing else extra
    return env
