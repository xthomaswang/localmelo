"""The developer route of the query adapter: ``POST /api/memory/adapter``.

It answers 404 outside developer mode (``[ui] mode = "developer"``) unless the
console was given ``dev_routes``. In developer mode it builds the adapter from
scratch and answers the report: 409 with memory off, and a ``skipped`` report
when there is no embedding model to read the notes with. Over the console's app
with a scripted model, as ``test_memory_routes`` does.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from localmelo.support.gateway.console import Console
from localmelo.support.memory import adapter as adapting
from localmelo.support.sessions.store import SessionStore

from .test_memory_routes import ScriptedLLM, setup  # noqa: F401  (fixture)

Setup = tuple[Console, httpx.AsyncClient, list[ScriptedLLM]]


async def test_the_route_is_not_there_outside_developer_mode(
    setup: Setup,  # noqa: F811
) -> None:
    console, client, _ = setup
    refused = await client.post("/api/memory/adapter")
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "not_found"
    # A test may open it without developer mode.
    console.options = dataclasses.replace(console.options, dev_routes=True)
    assert (await client.post("/api/memory/adapter")).status_code == 200


async def test_developer_mode_says_when_nothing_could_be_built(
    setup: Setup,  # noqa: F811
) -> None:
    console, client, _ = setup
    console.cfg.ui.mode = "developer"
    skipped = await client.post("/api/memory/adapter")
    assert skipped.status_code == 200
    body = skipped.json()
    assert (body["state"], body["reason"], body["pairs"]) == (
        "skipped",
        "no embedding model is set up",
        0,
    )
    assert "event" not in body


async def test_memory_off_is_a_conflict(setup: Setup) -> None:  # noqa: F811
    console, client, _ = setup
    console.cfg.ui.mode = "developer"
    console.cfg.memory.across_chats = False
    refused = await client.post("/api/memory/adapter")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "memory_off"
    assert "Memory across chats is off" in refused.json()["error"]["message"]


async def test_a_built_report_comes_with_its_event_and_the_chats(
    setup: Setup, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    console, client, _ = setup
    console.cfg.ui.mode = "developer"
    seen: list[SessionStore | None] = []

    async def rebuild(chats: SessionStore | None = None) -> adapting.AdapterReport:
        seen.append(chats)
        return adapting.AdapterReport(
            "built",
            pairs=61,
            by_source={"quote": 40, "used": 18, "sleep": 3},
            held_out_loss=0.2,
            baseline_loss=0.5,
            held_out_mrr=0.6,
            baseline_mrr=0.4,
            steps=25,
            train_hash="abc",
        )

    monkeypatch.setattr(console.memory, "rebuild_adapter", rebuild)
    response = await client.post("/api/memory/adapter")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "built" and body["pairs"] == 61
    assert body["by_source"] == {"quote": 40, "used": 18, "sleep": 3}
    assert (body["baseline_loss"], body["held_out_loss"]) == (0.5, 0.2)
    assert body["event"] == {
        "type": "memory.adapter",
        "data": {"state": "built", "pairs": 61, "loss": 0.2},
    }
    assert seen == [console.store]
