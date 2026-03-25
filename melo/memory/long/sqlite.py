from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from localmelo.melo.memory.long import LongTerm


class SqliteLongTerm(LongTerm):
    """Persistent long-term memory backed by SQLite.

    Drop-in replacement for the in-memory :class:`LongTerm` — same async
    interface, vectors survive process restarts.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS long_term (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                text     TEXT NOT NULL,
                vector   BLOB NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """
        )
        self._conn.commit()

    async def add(
        self,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        self._conn.execute(
            "INSERT INTO long_term (text, vector, metadata) VALUES (?, ?, ?)",
            (text, vec.tobytes(), json.dumps(metadata or {})),
        )
        self._conn.commit()

    async def search(
        self, query_embedding: list[float], top_k: int = 5
    ) -> list[tuple[str, float, dict[str, Any]]]:
        q = np.asarray(query_embedding, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return []
        q = q / q_norm

        rows = self._conn.execute(
            "SELECT text, vector, metadata FROM long_term"
        ).fetchall()
        if not rows:
            return []

        scores: list[tuple[str, float, dict[str, Any]]] = []
        for text, vec_bytes, meta_json in rows:
            vec = np.frombuffer(vec_bytes, dtype=np.float32)
            e_norm = np.linalg.norm(vec)
            if e_norm == 0:
                continue
            sim = float(np.dot(q, vec / e_norm))
            scores.append((text, sim, json.loads(meta_json)))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def close(self) -> None:
        self._conn.close()
