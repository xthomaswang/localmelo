"""The personal query adapter: pairs from notes, events and sleep; a trained map;
the lifecycle of its file.

Synthetic notes written straight to a temporary records file, synthetic chat
turns in a temporary session file, and a fake embedding model in eight
dimensions: notes embed as their meaning and a question (it starts with the
Qwen3 instruction) as that meaning with its first axis squashed, so the
model confuses a note with its siblings and a linear map can undo it. No
model, no network and none of the user's files are touched.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from localmelo.melo.contracts.providers import BaseEmbeddingProvider
from localmelo.melo.schema import new_record_id
from localmelo.support.config import Config, MemoryConfig
from localmelo.support.memory import ChatMemory, records
from localmelo.support.memory import adapter as adapting
from localmelo.support.memory.service import EMBED_TIMEOUT_S, _instructed
from localmelo.support.sessions.store import NewSession, SessionStore

HOLDER = "test-host:1:adapter"
MODEL = "qwen3-embedding-test"
D = 8
SQUASH = 0.5
LEVELS = (1.0, 0.5, 0.2)
NAMES = [
    *("gym", "dance", "swim", "run", "yoga", "chess", "piano", "tennis", "hike"),
    *("cook", "paint", "sing", "climb", "surf", "golf", "judo", "skate", "bike"),
    *("row", "fence", "knit", "sew", "chat", "draw", "bake", "brew", "camp"),
    *("dive", "jog", "kayak", "lift", "pray", "read", "ski", "sail", "quilt"),
]
WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
PROBE = "my gym day"
_QUERY = re.compile(r"^Instruct: .*?\nQuery: ", re.DOTALL)


def unit(vector: Any) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float64)
    return array / np.linalg.norm(array)


def hashed(text: str) -> np.ndarray:
    seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return np.random.default_rng(seed).standard_normal(D)


class Space(BaseEmbeddingProvider):
    """A fake embedding model: a text has a meaning (a vector it was told, else a
    hashed one); a question's vector is its meaning seen through ``SKEW``."""

    SKEW = np.diag([SQUASH] + [1.0] * (D - 1))

    def __init__(self, model: str = MODEL) -> None:
        self.model = model
        self.meaning: dict[str, np.ndarray] = {}
        self.calls: list[list[str]] = []
        self.fail = False

    def know(self, text: str, meaning: Any, noise: float = 0.0) -> np.ndarray:
        self.meaning[text] = unit(unit(meaning) + noise * unit(hashed(text)))
        return self.meaning[text]

    def document(self, text: str) -> list[float]:
        return [float(x) for x in self.meaning.get(text, unit(hashed(text)))]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("the embedding server is down")
        out = []
        for text in texts:
            plain = _QUERY.sub("", text)
            meaning = self.meaning.get(plain, unit(hashed(plain)))
            vector = unit(self.SKEW @ meaning) if plain != text else meaning
            out.append([float(x) for x in vector])
        return out

    async def close(self) -> None:
        return None


@dataclass
class World:
    memory: ChatMemory
    chats: SessionStore
    space: Space
    cfg: Config
    ids: dict[str, int] = field(default_factory=dict)  # note text -> id


@pytest.fixture()
async def chats(tmp_path: Path) -> AsyncIterator[SessionStore]:
    store = SessionStore(str(tmp_path / "sessions.db"), writer="gateway")
    await store.open()
    try:
        yield store
    finally:
        await store.aclose()


@pytest.fixture()
async def world(tmp_path: Path, chats: SessionStore) -> AsyncIterator[World]:
    cfg = Config(chat_backend="vllm", embedding_backend="none")
    cfg.memory.adapter = "on"
    cfg.memory.adapter_penalty = 0.01  # d = 8 has little to hold the map back
    memory = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    space = Space()
    memory.embedding = lambda: (space, MODEL)  # type: ignore[method-assign]
    # What a chat turn's before_turn leaves for a build in the background.
    memory._chats = chats
    try:
        yield World(memory, chats, space, cfg)
    finally:
        await memory.aclose()


async def put(
    w: World,
    text: str,
    *,
    topic: str = "about",
    quote: str = "",
    source: str = "user",
    admitted_by: str = "user_statement",
    action: str = "NEW",
    target: int | None = None,
    extra: dict[str, Any] | None = None,
    vector: list[float] | None = None,
) -> int:
    """A note written as it is, with the vector the fake model gives its text."""
    provenance = {
        "session_id": "s1",
        "turn": 1,
        "topic": topic,
        "quote": quote,
        **(extra or {}),
    }
    entry_id, _ = await w.memory.stores()[0].apply(
        text=text,
        kind=records.TOPIC_KINDS[topic],
        source=source,
        admitted_by=admitted_by,
        provenance=provenance,
        task_id="",
        session_id="s1",
        created_at=time.time(),
        embedding=vector if vector is not None else w.space.document(text),
        action=action,
        target_id=target,
        reason="seeded",
        source_ids=(),
        keep_text=False,
        candidate_id=new_record_id(),
        record={"topic": topic, "quote": quote, "embed_model": MODEL},
    )
    assert entry_id is not None
    w.ids[text] = entry_id
    return entry_id


async def turn(w: World, typed: str, used: list[int]) -> None:
    """A finished chat turn of *typed* whose ``turn.memory`` event lists the *used*
    notes (and a chat turn, which is never a pair)."""
    began = await w.chats.begin_turn(
        None, typed, holder=HOLDER, new_session=NewSession()
    )
    items: list[dict[str, Any]] = [
        {"kind": "record", "id": i, "text": "x", "session_id": "s1", "turn": 1}
        for i in used
    ]
    items.append({"kind": "chat", "id": 3, "text": "x", "session_id": "s1", "turn": 1})
    data = {
        "turn": began.turn,
        "turn_id": began.id,
        "used": items,
        "remembered": [],
        "refused": "",
        "recall": {},
        "unsaved_claim": False,
    }
    await w.chats.append_events(
        began.session_id,
        began.turn,
        [{"type": "turn.memory", "data": data}],
        holder=HOLDER,
    )
    await w.chats.finish_turn(
        began.session_id,
        began.turn,
        holder=HOLDER,
        status="completed",
        cause="run",
        result=None,
        answer="Noted.",
    )


def text_of(index: int) -> str:
    return f"Your {NAMES[index]} day is {WEEKDAYS[index % 7]}."


def quote_of(index: int) -> str:
    return f"I hit the {NAMES[index]} on {WEEKDAYS[index % 7]}s"


def typed_of(index: int, n: int) -> str:
    return f"when is my {NAMES[index]} day? try {n}"


def older_of(index: int) -> str:
    return f"{NAMES[index]} is on {WEEKDAYS[index % 7]}, I think"


async def notes(w: World, count: int = 36) -> None:
    """*count* notes, each with a quote worded another way: the notes of one group of
    three share a base meaning and differ on the axis the fake model squashes."""
    rng = np.random.default_rng(11)
    bases = [unit(rng.standard_normal(D - 1)) for _ in range(12)]
    for index in range(count):
        meaning = np.concatenate([[LEVELS[index % 3]], bases[index // 3]])
        w.space.know(text_of(index), meaning)
        w.space.know(quote_of(index), meaning, 0.1)
        await put(w, text_of(index), quote=quote_of(index))
    w.space.know(PROBE, w.space.meaning[text_of(0)])


async def extras(w: World) -> dict[str, int]:
    """Notes and changes around the 36: some are trained on, some never."""
    ids: dict[str, int] = {}
    # Never: People, Decisions, a note brought in from a file, a secret, a quote
    # that only rewords its note, and a note the user reworded after a recall.
    ids["people"] = await put(
        w, "Your manager is Tom Briggs.", topic="people", quote="Tom is my boss"
    )
    ids["decision"] = await put(
        w, "You chose Postgres.", topic="decisions", quote="we went with pg"
    )
    ids["import"] = await put(
        w,
        "You work on robots.",
        quote="my job is robots",
        admitted_by="import",
        extra={"source": "import", "batch_id": "b1"},
    )
    ids["secret"] = await put(
        w, "Your wifi password is hunter2.", quote="the wifi password is hunter2 ok"
    )
    ids["trivial"] = await put(
        w,
        "You prefer short answers.",
        topic="preferences",
        quote="I prefer short answers.",
    )
    ids["edited"] = await put(
        w,
        "You like tea.",
        quote="something I said long ago about drinks",
        extra={"edited": True, "edited_at": time.time() + 1_000},
    )
    # A change of the user's own: the older value is a stale negative of the new note.
    ids["lisbon"] = await put(w, "You live in Lisbon.")
    ids["utrecht"] = await put(
        w,
        "You live in Utrecht.",
        quote="I moved to Utrecht last spring",
        action="UPDATE",
        target=ids["lisbon"],
    )
    # Sleep: two merges (O1), a newer value (O2) and a change that was undone.
    store = w.memory.stores()[0]
    for n, index in enumerate((9, 10)):
        older = await put(w, older_of(index))
        keeper = w.ids[text_of(index)]
        await store.set_status(
            [older],
            "superseded",
            keeper=keeper,
            admitted_by="sleep",
            reason=f"merged into {keeper} by sleep run{n}",
        )
        ids[f"older{n}"] = older
    ids["garden_old"] = await put(w, "Your garden day is Monday.")
    ids["garden"] = await put(
        w,
        "Your garden day is Tuesday.",
        target=ids["garden_old"],
        source="sleep",
        admitted_by="sleep",
        action="UPDATE",
    )
    ids["bad"] = await put(
        w,
        "Your garden day is Friday.",
        target=ids["garden"],
        source="sleep",
        admitted_by="sleep",
        action="UPDATE",
    )
    await store.set_status(
        [ids["bad"]],
        "superseded",
        keeper=None,
        admitted_by="sleep",
        reason="undone: sleep run9",
    )
    await store.set_status(
        [ids["garden"]],
        "active",
        keeper=None,
        admitted_by="sleep",
        reason="sleep run9 undone",
    )
    # Recall events: two typed turns for each of ten notes, and one for notes never
    # trained on.
    for index in range(3, 13):
        for n in (1, 2):
            w.space.know(typed_of(index, n), w.space.meaning[text_of(index)], 0.1)
            await turn(w, typed_of(index, n), [w.ids[text_of(index)]])
    never = [ids["people"], ids["import"], ids["edited"]]
    await turn(w, "ask about people and imports", never)
    return ids


async def training(w: World) -> adapting.TrainingSet:
    source = await adapting.collect(w.memory.stores()[0])
    used = await adapting.used_turns(w.chats)
    return adapting.build(replace(source, used=used), MODEL, D)


async def rebuild(w: World) -> adapting.AdapterReport:
    return await w.memory.rebuild_adapter(w.chats)


def file_of(w: World) -> str:
    return adapting.path_of(w.memory.directory)


def dummy(**fields: Any) -> adapting.Adapter:
    """An adapter file's content with a map of the identity."""
    values: dict[str, Any] = {
        "W": np.eye(D),
        "embed_model": MODEL,
        "dim": D,
        "train_hash": "h",
        "built_at": 1.0,
        "pairs": 99,
        "held_out_loss": 0.1,
    }
    return adapting.Adapter(**{**values, **fields})


async def question(w: World, text: str) -> list[float]:
    vectors, _, _ = await w.memory._embed([text], EMBED_TIMEOUT_S, query=True)
    assert vectors is not None
    return vectors[0]


def cosines(vector: list[float], space: Space, *texts: str) -> list[float]:
    return [float(unit(vector) @ space.meaning[t]) for t in texts]


def plain_question(w: World, text: str) -> np.ndarray:
    """The vector of a question as the fake model gives it, with no map."""
    return unit(w.space.SKEW @ w.space.meaning[text])


# ── Pairs ──


async def test_the_pairs_of_each_source_are_counted(world: World) -> None:
    await notes(world)
    ids = await extras(world)
    found = await training(world)
    assert found.counts() == {"quote": 37, "used": 20, "sleep": 3}
    assert len(found.pairs) == 60
    by = {
        s: {(p.query, p.note_id) for p in found.pairs if p.source == s}
        for s in found.counts()
    }
    # The wording sleep merged away or replaced, paired with the note that kept its place.
    assert by["sleep"] == {
        ("Your garden day is Monday.", ids["garden"]),
        (older_of(9), world.ids[text_of(9)]),
        (older_of(10), world.ids[text_of(10)]),
    }
    # The user's own words, and the typed text of a turn that recalled the note.
    assert (quote_of(0), world.ids[text_of(0)]) in by["quote"]
    assert ("I moved to Utrecht last spring", ids["utrecht"]) in by["quote"]
    assert (typed_of(3, 1), world.ids[text_of(3)]) in by["used"]
    assert (typed_of(12, 2), world.ids[text_of(12)]) in by["used"]
    # Sorted, so a rebuild sees the same order.
    keys = [(p.note_id, p.query.lower(), p.source) for p in found.pairs]
    assert keys == sorted(keys)


async def test_people_decisions_imports_secrets_and_trivial_notes_are_never_trained_on(
    world: World,
) -> None:
    await notes(world)
    ids = await extras(world)
    found = await training(world)
    positive = {p.note_id for p in found.pairs}
    negative = {i for p in found.pairs for i in p.negatives}
    # None of them is the right answer of a pair.
    for never in ("people", "decision", "import", "secret", "trivial", "edited"):
        assert ids[never] not in positive, never
    # People, Decisions, imports and secrets are not even a wrong answer.
    for never in ("people", "decision", "import", "secret"):
        assert ids[never] not in set(found.notes) | negative, never
    queries = [p.query for p in found.pairs]
    assert "Tom is my boss" not in queries
    assert not any("hunter2" in q for q in queries)
    # The typed text of the turn that recalled them is no pair either.
    assert "ask about people and imports" not in queries


async def test_hard_negatives_are_siblings_older_values_and_undone_changes(
    world: World,
) -> None:
    await notes(world)
    ids = await extras(world)
    found = await training(world)

    def negatives(note_id: int, source: str) -> tuple[int, ...]:
        (pair,) = [
            p for p in found.pairs if p.note_id == note_id and p.source == source
        ]
        return pair.negatives

    gym, swim = world.ids[text_of(0)], world.ids[text_of(2)]
    assert swim in negatives(gym, "quote")  # the confusable sibling
    assert ids["lisbon"] in negatives(ids["utrecht"], "quote")  # the older value
    garden = negatives(ids["garden"], "sleep")
    assert ids["bad"] in garden  # a note of an undone change
    assert ids["garden_old"] not in garden  # it is this pair's own query
    for note_id, source in ((gym, "quote"), (ids["garden"], "sleep")):
        assert len(negatives(note_id, source)) <= adapting.MAX_NEGATIVES
        assert note_id not in negatives(note_id, source)


async def test_a_note_with_a_vector_of_another_size_is_left_out(world: World) -> None:
    await notes(world)
    await put(world, "You like jazz.", quote="jazz is what I put on", vector=[1.0] * 4)
    found = await training(world)
    assert world.ids["You like jazz."] not in found.notes
    assert found.counts()["quote"] == 36


async def test_fewer_than_30_pairs_builds_no_adapter_and_removes_an_old_one(
    world: World,
) -> None:
    await notes(world, count=24)  # 24 quote pairs
    report = await rebuild(world)
    assert (report.state, report.pairs) == ("skipped", 24)
    assert "at least 30 pairs" in report.reason
    assert not os.path.exists(file_of(world))
    adapting.save(file_of(world), dummy())
    report = await rebuild(world)
    assert report.state == "removed" and not os.path.exists(file_of(world))
    assert report.to_json()["event"] == {
        "type": "memory.adapter",
        "data": {"state": "removed", "pairs": 24, "loss": None},
    }


# ── The map ──


async def test_a_trained_map_moves_my_gym_day_from_the_swim_note_to_the_gym_note(
    world: World,
) -> None:
    await notes(world)
    gym, swim = text_of(0), text_of(2)
    plain_gym, plain_swim = cosines(
        await question(world, PROBE), world.space, gym, swim
    )
    assert plain_swim > plain_gym  # before: the model confuses them
    report = await rebuild(world)
    assert report.state == "built", report
    assert report.held_out_loss is not None and report.baseline_loss is not None
    assert report.held_out_loss < report.baseline_loss
    after = await question(world, PROBE)
    mapped_gym, mapped_swim = cosines(after, world.space, gym, swim)
    assert mapped_gym > mapped_swim  # after: the gym note is the nearer
    assert float(np.linalg.norm(after)) == pytest.approx(1.0)
    status = world.memory.adapter_status()
    assert status is not None and status["active"] and status["pairs"] == 36
    assert report.to_json()["event"]["data"]["state"] == "built"


async def test_the_file_is_private_atomic_and_read_back(world: World) -> None:
    await notes(world)
    report = await rebuild(world)
    path = file_of(world)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not [f for f in os.listdir(world.memory.directory) if f.endswith(".tmp")]
    saved = adapting.load(path)
    assert saved is not None
    assert (saved.embed_model, saved.dim, saved.pairs) == (MODEL, D, 36)
    assert saved.train_hash == report.train_hash and saved.W.dtype == np.float32
    assert saved.W.shape == (D, D)
    # A write that fails leaves the old file and no temporary file behind.
    with pytest.raises(AttributeError):
        adapting.save(path, dummy(W=None))
    kept = adapting.load(path)
    assert kept is not None and np.array_equal(kept.W, saved.W)
    assert not [f for f in os.listdir(world.memory.directory) if f.endswith(".tmp")]
    Path(path).write_bytes(b"not a numpy file")
    assert adapting.load(path) is None


async def test_the_same_notes_give_the_same_map(world: World) -> None:
    await notes(world)
    await rebuild(world)
    first = adapting.load(file_of(world))
    assert first is not None
    os.unlink(file_of(world))
    await rebuild(world)
    second = adapting.load(file_of(world))
    assert second is not None
    assert np.array_equal(first.W, second.W)
    assert first.train_hash == second.train_hash
    assert first.held_out_loss == second.held_out_loss
    assert (await training(world)).digest(MODEL, D) == first.train_hash


async def test_an_unchanged_training_set_is_not_built_again(world: World) -> None:
    await notes(world)
    assert (await rebuild(world)).state == "built"
    asked = len(world.space.calls)
    again = await rebuild(world)
    assert again.state == "unchanged" and len(world.space.calls) == asked
    world.space.know("jazz is what I put on", unit(hashed("jazz")), 0.1)
    await put(world, "You like jazz.", quote="jazz is what I put on")
    assert (await rebuild(world)).state == "built"  # a new pair, a new map


def toy(count: int = 40) -> tuple[Any, ...]:
    """Notes in eight dimensions and queries that are them with the first axis shrunk."""
    rng = np.random.default_rng(0)
    notes_ = rng.standard_normal((count, D)).astype(np.float32)
    notes_ /= np.linalg.norm(notes_, axis=1, keepdims=True)
    queries = notes_ @ np.diag([0.3] + [1.0] * (D - 1)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    negatives = [[(i + 1) % count, (i + 2) % count] for i in range(count)]
    return queries, notes_, np.arange(count), negatives


def test_training_holds_out_whole_notes_keeps_the_best_step_and_is_deterministic() -> (
    None
):
    queries, notes_, positive, negatives = toy()
    trained = adapting.train(queries, notes_, positive, negatives, penalty=0.01)
    assert trained is not None
    assert trained.held_out_loss < trained.baseline_loss
    # A map is only kept when it does not rank the held-out notes worse than I.
    assert trained.held_out_mrr >= trained.baseline_mrr
    assert 1 <= trained.steps <= adapting.MAX_STEPS
    again = adapting.train(queries, notes_, positive, negatives, penalty=0.01)
    assert again is not None and np.array_equal(trained.W, again.W)
    # A heavy pull towards the identity gives a map nearer to it, or none.
    held = adapting.train(queries, notes_, positive, negatives, penalty=100.0)
    identity = np.eye(D)
    assert held is None or np.linalg.norm(held.W - identity) < np.linalg.norm(
        trained.W - identity
    )


def test_too_few_notes_to_hold_any_out_gives_no_map() -> None:
    queries, notes_, positive, negatives = toy(adapting.MIN_NOTES - 1)
    assert adapting.train(queries, notes_, positive, negatives) is None


# ── Where it applies ──


async def test_a_map_for_another_model_or_size_is_ignored(world: World) -> None:
    await notes(world)
    await rebuild(world)
    plain = plain_question(world, PROBE)
    assert not np.allclose(await question(world, PROBE), plain)
    saved = adapting.load(file_of(world))
    assert saved is not None
    # Another model id: ignored, and back with the model.
    world.memory.embedding = lambda: (world.space, "qwen3-embedding-other")  # type: ignore[method-assign]
    assert np.allclose(await question(world, PROBE), plain)
    status = world.memory.adapter_status()
    assert status is not None and status["active"] is False
    world.memory.embedding = lambda: (world.space, MODEL)  # type: ignore[method-assign]
    assert not np.allclose(await question(world, PROBE), plain)
    # Another size: a map of 4 dimensions never meets a vector of 8.
    adapting.save(file_of(world), dummy(W=np.eye(4), dim=4))
    assert np.allclose(await question(world, PROBE), plain)
    assert saved.matches(MODEL, D) and not saved.matches(MODEL, D + 1)
    assert not saved.matches("", D) and saved.apply([1.0, 0.0]) is None


async def test_only_the_question_is_mapped_never_a_document_or_a_reindex(
    world: World,
) -> None:
    await notes(world)
    await rebuild(world)
    documents = [text_of(0), text_of(2)]
    vectors, _, _ = await world.memory._embed(documents, 1.0, query=False)
    assert vectors == [world.space.document(t) for t in documents]
    # A batch with a question first: its vector only, the rest as the model gave them.
    batch, _, _ = await world.memory._embed([PROBE, *documents], 1.0, query=True)
    plain = await world.space.embed([_instructed(MODEL, PROBE), *documents])
    assert batch is not None and batch[1:] == plain[1:] and batch[0] != plain[0]
    # Giving a note a vector again is a document call: it never meets the map.
    note = world.ids[text_of(1)]
    await world.memory.stores()[0].set_vector(note, [0.0] * D, "stale")
    await world.memory.reindex(None)
    (entry,) = [e for e in await world.memory.records() if e.id == note]
    assert list(entry.vector or ()) == pytest.approx(
        world.space.document(text_of(1)), abs=1e-6
    )


async def test_it_is_off_by_default_and_then_nothing_changes(world: World) -> None:
    assert MemoryConfig().adapter == "off" and MemoryConfig().adapter_penalty == 1.0
    await notes(world)
    questions = [PROBE, "when is my yoga day?", "where do I live?"]

    async def run() -> tuple[list[list[str]], list[list[str]]]:
        world.space.calls.clear()
        order = [[i.key for i in await world.memory.search(q)] for q in questions]
        return world.space.calls[:], order

    world.cfg.memory.adapter = "off"
    plain_calls, plain_order = await run()
    # The bytes of a request: the instruction and the question, nothing else.
    assert plain_calls[0] == [_instructed(MODEL, PROBE)]
    assert plain_calls[0][0].startswith("Instruct: ") and plain_calls[0][0].endswith(
        PROBE
    )
    plain_vectors = [await question(world, q) for q in questions]
    # A map that is built, and would change every question, changes nothing while off.
    scramble, _ = np.linalg.qr(np.random.default_rng(5).standard_normal((D, D)))
    adapting.save(file_of(world), dummy(W=scramble))
    assert await run() == (plain_calls, plain_order)
    assert [await question(world, q) for q in questions] == plain_vectors
    assert world.memory.adapter_status() == {
        "on": False,
        "pairs": 99,
        "built_at": 1.0,
        "held_out_loss": 0.1,
        "active": False,
        "train_hash": "h",
    }
    # On with no file: the same again. A request is never different, only a vector is.
    os.unlink(file_of(world))
    world.cfg.memory.adapter = "on"
    assert await run() == (plain_calls, plain_order)
    adapting.save(file_of(world), dummy(W=scramble))
    mapped_calls, _ = await run()
    assert mapped_calls == plain_calls
    assert await question(world, PROBE) != plain_vectors[0]
    # Off, a forget builds nothing.
    world.cfg.memory.adapter = "off"
    os.unlink(file_of(world))
    await world.memory.forget_all()
    await world.memory.drain_adapter()
    assert not os.path.exists(file_of(world))
    assert world.memory.adapter_status() is None


def test_the_setting_is_checked() -> None:
    cfg = Config(chat_backend="vllm", embedding_backend="none")
    assert not [e for e in cfg.validate() if "[memory]" in e]
    cfg.memory.adapter = "maybe"
    errors = cfg.validate()
    assert any("[memory] adapter 'maybe' is not recognised" in e for e in errors)
    cfg.memory = MemoryConfig(adapter="on", adapter_penalty=-1.0)
    errors = cfg.validate()
    assert any("adapter_penalty must be a number of at least 0" in e for e in errors)


# ── Forget, edit, undo ──


async def built(world: World) -> None:
    await notes(world)
    await extras(world)
    assert (await rebuild(world)).state == "built"
    assert os.path.exists(file_of(world))


async def test_forgetting_a_note_in_the_set_removes_the_file_in_the_same_call(
    world: World,
) -> None:
    await built(world)
    before = adapting.load(file_of(world))
    assert before is not None and before.pairs == 60
    gym = world.ids[text_of(0)]
    assert gym in (await training(world)).notes
    assert await world.memory.delete_record(gym)
    assert not os.path.exists(file_of(world))  # gone when the call returns
    await world.memory.drain_adapter()  # the rebuild, from scratch
    after = adapting.load(file_of(world))
    assert after is not None
    found = await training(world)
    assert gym not in found.notes and not any(p.note_id == gym for p in found.pairs)
    assert after.train_hash == found.digest(MODEL, D) != before.train_hash
    assert after.pairs == 59  # without its quote pair


async def test_editing_a_note_in_the_set_removes_the_file_and_the_rebuild_reads_the_new_wording(
    world: World,
) -> None:
    await built(world)
    before = adapting.load(file_of(world))
    assert before is not None
    note = world.ids[text_of(4)]
    edited = await world.memory.edit_record(note, "Your yoga day is Sunday.")
    assert edited is not None and not os.path.exists(file_of(world))
    await world.memory.drain_adapter()
    after = adapting.load(file_of(world))
    assert after is not None and after.train_hash != before.train_hash
    # It was reworded after its recalls: its quote (now its wording) and the two turns
    # that recalled the old wording are no pairs.
    assert after.pairs == 57


@pytest.mark.parametrize(
    "act",
    [
        "edit",
        "forget",
        "undo a note",
        "forget everything",
        "forget a chat's notes",
        "forget a chat",
        "undo an import",
        "undo a sleep run",
    ],
)
async def test_every_forget_edit_and_undo_removes_the_file_before_it_returns(
    world: World, act: str
) -> None:
    await notes(world, count=3)
    memory, note = world.memory, world.ids[text_of(1)]
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "edit": lambda: memory.edit_record(note, "Your dance day is Friday."),
        "forget": lambda: memory.delete_record(note),
        "undo a note": lambda: memory.delete_record(note, undo=True),
        "forget everything": memory.forget_all,
        "forget a chat's notes": lambda: memory.forget_chat_notes("s1"),
        "forget a chat": lambda: memory.forget_session("s1"),
        "undo an import": lambda: memory.undo_import("b1"),
        "undo a sleep run": lambda: memory.undo_sleep("run9"),
    }
    adapting.save(file_of(world), dummy())
    await calls[act]()
    assert not os.path.exists(file_of(world))
    assert memory.adapter_status() == {
        "on": True,
        "pairs": 0,
        "built_at": None,
        "held_out_loss": None,
    }
    await memory.drain_adapter()  # a build was scheduled; 3 notes are too few for a map
    assert not os.path.exists(file_of(world))


async def test_a_build_that_a_forget_overtakes_is_not_saved(world: World) -> None:
    await notes(world)
    world.cfg.memory.adapter = "off"  # no automatic build to race with
    original = world.space.embed

    async def forgetting(texts: list[str]) -> list[list[float]]:
        out = await original(texts)
        if len(world.space.calls) == 1:
            await world.memory.delete_record(world.ids[text_of(5)])
        return out

    world.space.embed = forgetting  # type: ignore[method-assign]
    report = await rebuild(world)
    assert report.state == "skipped" and "changed while" in report.reason
    assert not os.path.exists(file_of(world))


async def test_a_finished_sleep_run_builds_a_new_adapter(world: World) -> None:
    await notes(world)
    world.cfg.memory.sleep = "rules"
    await world.memory.sleep()
    await world.memory.drain_adapter()
    assert adapting.load(file_of(world)) is not None


# ── When a build waits or cannot ──


async def test_a_build_waits_for_the_lock_and_for_a_quiet_user(
    world: World, tmp_path: Path
) -> None:
    await notes(world)
    lock = tmp_path / "busy.lock"
    lock.write_text("held")
    world.cfg.memory.sleep_lock = str(lock)
    report = await rebuild(world)
    assert (report.state, report.reason) == ("skipped", "sleep lock is held")
    assert world.space.calls == [] and not os.path.exists(file_of(world))
    lock.unlink()
    original = world.space.embed

    async def busy(texts: list[str]) -> list[list[float]]:
        out = await original(texts)
        if len(world.space.calls) == 1:
            await turn(world, "hello there", [])  # the user is back
        return out

    world.space.embed = busy  # type: ignore[method-assign]
    report = await rebuild(world)
    assert (report.state, report.reason) == ("skipped", "user is active")
    assert not os.path.exists(file_of(world))


async def test_no_embedding_model_a_failing_one_or_memory_off_builds_nothing(
    world: World,
) -> None:
    await notes(world)
    world.memory.embedding = lambda: (None, "")  # type: ignore[method-assign]
    assert (await rebuild(world)).reason == "no embedding model is set up"
    world.memory.embedding = lambda: (world.space, MODEL)  # type: ignore[method-assign]
    world.space.fail = True
    report = await rebuild(world)
    assert report.state == "skipped" and "could not embed" in report.reason
    assert world.memory.last_error
    world.space.fail = False
    world.cfg.memory.across_chats = False
    assert (await rebuild(world)).reason == "memory across chats is off"
    assert not os.path.exists(file_of(world))
