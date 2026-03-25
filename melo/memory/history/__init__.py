from __future__ import annotations

from localmelo.melo.schema import StepRecord, TaskRecord


class History:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}

    async def save_task(self, task: TaskRecord) -> None:
        self._tasks[task.task_id] = task

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    async def add_step(self, task_id: str, step: StepRecord) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.steps.append(step)

    async def get_steps(self, task_id: str) -> list[StepRecord]:
        task = self._tasks.get(task_id)
        return task.steps if task else []
