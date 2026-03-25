from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from localmelo.melo.memory.history import History
from localmelo.melo.schema import StepRecord, TaskRecord, ToolCall, ToolResult


class SqliteHistory(History):
    """Persistent history backed by SQLite.

    Drop-in replacement for the in-memory :class:`History` — same async
    interface, data survives process restarts.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                query   TEXT NOT NULL,
                status  TEXT NOT NULL DEFAULT 'running',
                result  TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS steps (
                step_id          TEXT PRIMARY KEY,
                task_id          TEXT NOT NULL,
                thought          TEXT NOT NULL DEFAULT '',
                tool_call_json   TEXT,
                tool_result_json TEXT,
                timestamp        REAL NOT NULL,
                seq              INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            );
        """
        )
        self._conn.commit()

    # ── public interface (mirrors History) ──

    async def save_task(self, task: TaskRecord) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO tasks (task_id, query, status, result)
               VALUES (?, ?, ?, ?)""",
            (task.task_id, task.query, task.status, task.result),
        )
        self._conn.commit()

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT task_id, query, status, result FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        task = TaskRecord(query=row[1], task_id=row[0], status=row[2], result=row[3])
        task.steps = await self.get_steps(task_id)
        return task

    async def add_step(self, task_id: str, step: StepRecord) -> None:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM steps WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        seq = (row[0] if row else -1) + 1

        tc_json = None
        if step.tool_call:
            tc_json = json.dumps(
                {
                    "tool_name": step.tool_call.tool_name,
                    "arguments": step.tool_call.arguments,
                }
            )

        tr_json = None
        if step.tool_result:
            tr_json = json.dumps(
                {
                    "tool_name": step.tool_result.tool_name,
                    "output": step.tool_result.output,
                    "error": step.tool_result.error,
                    "duration_ms": step.tool_result.duration_ms,
                }
            )

        self._conn.execute(
            """INSERT INTO steps
               (step_id, task_id, thought, tool_call_json, tool_result_json,
                timestamp, seq)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                step.step_id,
                task_id,
                step.thought,
                tc_json,
                tr_json,
                step.timestamp,
                seq,
            ),
        )
        self._conn.commit()

    async def get_steps(self, task_id: str) -> list[StepRecord]:
        rows = self._conn.execute(
            """SELECT step_id, thought, tool_call_json, tool_result_json, timestamp
               FROM steps WHERE task_id = ? ORDER BY seq""",
            (task_id,),
        ).fetchall()

        steps: list[StepRecord] = []
        for row in rows:
            tc = None
            if row[2]:
                d = json.loads(row[2])
                tc = ToolCall(
                    tool_name=d["tool_name"],
                    arguments=d.get("arguments", {}),
                )

            tr = None
            if row[3]:
                d = json.loads(row[3])
                tr = ToolResult(
                    tool_name=d["tool_name"],
                    output=d.get("output", ""),
                    error=d.get("error", ""),
                    duration_ms=d.get("duration_ms", 0.0),
                )

            steps.append(
                StepRecord(
                    step_id=row[0],
                    thought=row[1],
                    tool_call=tc,
                    tool_result=tr,
                    timestamp=row[4],
                )
            )
        return steps

    def close(self) -> None:
        self._conn.close()
