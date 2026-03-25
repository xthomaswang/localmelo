from __future__ import annotations

import os


class WorkspacePolicy:
    """Restricts file operations to an allowed workspace root directory."""

    def __init__(self, allowed_root: str | None = None) -> None:
        self._root = os.path.realpath(allowed_root) if allowed_root else None

    @property
    def root(self) -> str | None:
        return self._root

    def check_path(self, path: str) -> bool:
        """Return True if *path* is inside the allowed root (or no root set)."""
        if self._root is None:
            return True
        real = os.path.realpath(path)
        return real == self._root or real.startswith(self._root + os.sep)
