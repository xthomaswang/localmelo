"""A task handover never carries the user's views.

A view (topic ``views``: the user's stance on a contested question) is recalled
by the chat when it matters. The notes a chat hands the task it starts leave it
out, whether the turn recalled it, searched for it or kept it with a "Start this
as a task?" card, as they leave out People and Decisions notes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from localmelo.support.gateway.handover import (
    LEFT_OUT_TOPICS,
    Handover,
    kept_lines,
    picked_lines,
)
from localmelo.support.memory import ChatMemory, records
from localmelo.support.memory.recall import Item

VIEW = "You believe remote work beats the office."
VIEW_QUOTE = "I believe remote work beats the office."


@pytest.fixture()
async def memory(tmp_path: Path) -> AsyncIterator[ChatMemory]:
    memory = ChatMemory(lambda: None, directory=str(tmp_path / "memory"))
    try:
        yield memory
    finally:
        await memory.aclose()


async def note(memory: ChatMemory, text: str, quote: str, topic: str) -> str:
    """One note of *topic*; its key (``r:<id>``)."""
    source: dict[str, Any] = {"session_id": "s1", "turn": 1, "chat": "Main chat"}
    cand = records.Candidate(text, quote, topic, "user_statement")
    written = await records.write(
        memory.stores()[0], [cand], [None], source=source, embed_model=""
    )
    return f"r:{written[0].id}"


async def item_of(memory: ChatMemory, key: str) -> Item:
    (text,) = [e.text for e in await memory.records() if f"r:{e.id}" == key]
    for item in await memory.search(text, records_k=8, docs_k=0):
        if item.key == key:
            return item
    raise AssertionError(f"{key} is not found")


def test_views_join_people_and_decisions_among_the_notes_a_task_never_gets() -> None:
    assert set(LEFT_OUT_TOPICS) == {"people", "decisions", "views"}


async def test_the_lines_a_chat_picked_leave_a_view_out(memory: ChatMemory) -> None:
    about = await note(memory, "Your name is Juno.", "My name is Juno.", "about")
    view = await note(memory, VIEW, VIEW_QUOTE, "views")
    items = {key: await item_of(memory, key) for key in (about, view)}
    handover = Handover(
        searched=(items[view], items[about]), used=(items[view], items[about])
    )
    lines, notes, *_ = await picked_lines(memory, handover)
    assert [line.split(" (")[0] for line in lines.splitlines()] == [
        "- the user said “My name is Juno.”"
    ]
    assert "remote" not in lines and notes == {about}


async def test_the_lines_a_card_kept_are_checked_again_and_leave_a_view_out(
    memory: ChatMemory,
) -> None:
    about = await note(memory, "Your name is Juno.", "My name is Juno.", "about")
    view = await note(memory, VIEW, VIEW_QUOTE, "views")
    handover = Handover(
        kept=(
            ("the user said “My name is Juno.”", about),
            (f"the user said “{VIEW_QUOTE}”", view),
            ("Main chat, 09:10 · the user: Plan the week", ""),
        )
    )
    picked = await kept_lines(memory, handover)
    assert picked.notes == {about}
    assert [text for text, _ in picked.entries] == [
        "the user said “My name is Juno.”",
        "Main chat, 09:10 · the user: Plan the week",
    ]
    assert "remote" not in picked.lines
