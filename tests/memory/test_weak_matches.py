"""A few weak matches under the list, marked: ``[memory] weak_matches``.

With ``0`` (the default) the block, the search result and the recall data are as they
were. With a number from 1 to 5, the list form's recall block (not the search tool)
also holds, after the list or alone when the gate failed, the best that many items
under the vector bar and at least 0.40 close, under one line that says they are weaker
matches and are to be opened only if the message needs them. Nothing under 0.40
comes, nothing the list holds comes again, and when the room runs out the weak lines
go first. ``render_list_block`` renders the same block from plain entries, for
evaluation outside a chat. Synthetic turns, notes and fixed vectors, no model.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest

from localmelo.melo.contracts.providers import BaseEmbeddingProvider
from localmelo.support.config import MEMORY_WEAK_MATCHES, Config
from localmelo.support.memory import ChatMemory
from localmelo.support.memory.index import Document, document_chunks
from localmelo.support.memory.recall import (
    AUTO_RECORDS,
    BLOCK_HEADER,
    GATHER_COSINE,
    ITEM_LIST_GUIDE,
    LIST_GUIDE,
    LIST_MAX,
    WEAK_GUIDE,
    Corpus,
    Item,
    block,
    block_with_weak,
    day,
    pick_weak,
    render_list_block,
    weak_scores,
)
from localmelo.support.memory.service import (
    CHAT_TURN,
    SEARCH_HINT,
    MemoryTools,
    Recall,
    RecallSettings,
    WeakPick,
)
from localmelo.support.sessions.store import NewSession, SessionStore
from localmelo.tests.memory.chat_memory_helpers import teach
from localmelo.tests.memory.test_gather_recall import (
    AS_ALWAYS,
    GATHER,
    NOTHING_FOUND,
    PLAIN,
    STRONG,
    TURNS,
    WORDS,
    line_of,
    listed,
    memory_of,
)
from localmelo.tests.memory.test_hippo_recall import put
from localmelo.tests.memory.test_recall_bars import (
    MODEL,
    NOW,
    QUERY_VECTOR,
    FixedEmbedding,
    doc_item,
    vector_at,
)
from localmelo.tests.memory.test_recall_list import (
    DAY,
    HOLDER,
    add_turn,
    ask,
    config,
    ids_of,
    lines_of,
)
from localmelo.tests.memory.test_relative_floor import (
    MIDDLE,
    WEAK,
    bar_of,
    memory_with,
    shown,
)
from localmelo.tests.sessions.helpers import Clock

THREE = [(1, "0.52"), (2, "0.45"), (3, "0.42")]  # the weak matches of TURNS


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
async def store(tmp_path: Path, clock: Clock) -> AsyncIterator[SessionStore]:
    store = SessionStore(str(tmp_path / "sessions.db"), writer="gateway", clock=clock)
    await store.open()
    try:
        yield store
    finally:
        CHAT_TURN.set(None)
        await store.aclose()


def with_weak(normal: list[tuple[int, str]], weak: list[tuple[int, str]]) -> str:
    """The block text (after its header) of TURNS' rows: the *normal* list under its
    guide, if any, then the *weak* rows under :data:`WEAK_GUIDE`."""
    parts = [LIST_GUIDE, *(line_of(n, c) for n, c in normal)] if normal else []
    return "\n".join([*parts, WEAK_GUIDE, *(line_of(n, c) for n, c in weak)])


# ── The setting ──


def test_the_constants_and_the_defaults() -> None:
    assert MEMORY_WEAK_MATCHES == (0, 5)
    assert GATHER_COSINE == 0.40
    assert RecallSettings().weak == 0
    assert WEAK_GUIDE == (
        "Weaker matches, maybe not about this message. Open one only if the message "
        "needs it; otherwise do not mention them."
    )


async def test_the_setting_is_read_each_turn_and_a_bad_one_is_zero(
    tmp_path: Path,
) -> None:
    cfg = config()
    memory = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    try:
        assert memory.recall_settings().weak == 0
        for value in (1, 3, 5):
            cfg.memory.weak_matches = value
            assert memory.recall_settings().weak == value
        for bad in (-1, 6, 2.5, True, "3", None):
            cfg.memory.weak_matches = bad  # type: ignore[assignment]
            assert memory.recall_settings().weak == 0, bad
    finally:
        await memory.aclose()


# ── Zero changes nothing ──

DATA = {
    "retriever",
    "fallback",
    "embed_ms",
    "search_ms",
    "select_ms",
    "select_tokens",
    "candidates",
    "used",
    "block_chars",
    "query_from",
}


@pytest.mark.parametrize("setting", [None, 0])
@pytest.mark.parametrize("form", ["list", "excerpt"])
async def test_zero_leaves_the_block_the_search_result_and_the_recall_data_as_they_were(
    tmp_path: Path, store: SessionStore, form: str, setting: int | None
) -> None:
    extra: dict[str, object] = {} if setting is None else {"weak_matches": setting}
    memory, _, _ = await memory_of(tmp_path, store, recall_form=form, **extra)
    try:
        for question in (GATHER, PLAIN):
            found = await ask(memory, store, question)
            data = found.event()["recall"]  # type: ignore[index]
            assert set(data) == DATA
            assert found.recall is not None and found.recall.weak is None
            assert data["candidates"] == 1 and data["used"] == 1
            reply = await MemoryTools(memory).search(query=WORDS)
            if form == "list":
                assert found.block == AS_ALWAYS + listed(STRONG)
                assert reply == listed(STRONG)
            else:
                said = (
                    "the user said “My cardiologist moved my appointment.”; melo "
                    "replied “Moved to Friday.” (chat “My cardiologist moved my "
                    f"appointment.”, {day(time.time() - 3 * DAY)})"
                )
                assert found.block == f"{AS_ALWAYS}- {said}"
                assert reply == f"1. {said}"
            assert data["block_chars"] == len(found.block)
    finally:
        await memory.aclose()


def test_block_is_block_with_weak_without_weak_matches() -> None:
    items = [
        Item("d:1", "User: a\nmelo: b", "chat “A”, Oct 30", "s1", 1, NOW),
        Item("m:2", "text", "chat “B”, Oct 30", "s2", 1, NOW, title="A title"),
    ]
    extra = Item("d:3", "User: c\nmelo: d", "chat “C”, Oct 29", "s3", 1, NOW)
    for listing in (True, False):
        text, used = block(items, [], 4000, listing=listing)
        again, same, weak = block_with_weak(items, [], 4000, listing=listing)
        assert (text, used) == (again, same) and weak == []
        # An item the list holds is never shown again as a weak match.
        twice = block_with_weak(items, [], 4000, listing=listing, weak=items[:1])
        assert twice == (text, used, [])
        # A weak match is shown only in the list form.
        more = block_with_weak(items, [], 4000, listing=listing, weak=[extra])
        assert (more[0] == text) is (not listing)
        assert (more[2] == []) is (not listing)


# ── With a number: the gate failed ──


async def test_three_weak_matches_stand_alone_when_the_gate_failed(
    tmp_path: Path, store: SessionStore
) -> None:
    corpus: tuple[list[float], list[float]] = (
        [0.50, 0.46, 0.42, 0.38],
        [],
    )  # no item at the bar, 0.55
    memory, cfg = await memory_with(
        tmp_path, store, corpus, weak_matches=3, recall_form="list"
    )
    try:
        found = await ask(memory, store, "zebra yodel")
        rows = [
            f"[t{n + 1}] {day(time.time() - (n + 1) * 3 * DAY)} · chat “Entry {n}.” · "
            f"“Entry {n}.” · {cosine}"
            for n, cosine in enumerate(["0.50", "0.46", "0.42"])
        ]
        assert found.block == f"\n\n{BLOCK_HEADER}\n" + "\n".join([WEAK_GUIDE, *rows])
        assert LIST_GUIDE not in found.block  # the list is empty
        assert shown(found.block) == ["0.50", "0.46", "0.42"]  # all 0.40 or more
        assert found.used == ()
        assert found.recall is not None
        assert found.recall.used == 0 and found.recall.weak == 3
        assert found.recall.block_chars == len(found.block)
        data = found.event()["recall"]  # type: ignore[index]
        assert data["weak"] == 3 and data["used"] == 0
        assert set(data) == DATA | {"weak"}
        assert found.event()["used"] == []  # type: ignore[index]
        # The same memory with 0: nothing at all, as always.
        assert isinstance(cfg, Config)
        cfg.memory.weak_matches = 0
        none = await ask(memory, store, "zebra yodel")
        assert none.block == "" and none.recall is not None and none.recall.weak is None
        assert set(none.event()["recall"]) == DATA  # type: ignore[index]
    finally:
        await memory.aclose()


async def test_the_best_weak_matches_come_first_up_to_the_setting(
    tmp_path: Path, store: SessionStore
) -> None:
    # Four are 0.40 or more; 0.38 and 0.20 are never weak matches, the setting aside.
    corpus: tuple[list[float], list[float]] = ([0.50, 0.46, 0.42, 0.41, 0.38, 0.20], [])
    for count, expected in (
        (1, ["0.50"]),
        (2, ["0.50", "0.46"]),
        (5, ["0.50", "0.46", "0.42", "0.41"]),
    ):
        memory, _ = await memory_with(
            tmp_path / str(count),
            store,
            corpus,
            weak_matches=count,
            recall_form="list",
        )
        try:
            found = await ask(memory, store, "zebra yodel")
            assert shown(found.block) == expected, count
            assert found.recall is not None and found.recall.weak == len(expected)
        finally:
            await memory.aclose()


# ── With a number: a strong match and weaker ones ──


async def test_weak_matches_follow_the_list_under_their_own_guide(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, ids, cfg = await memory_of(
        tmp_path, store, recall_form="list", weak_matches=3
    )
    try:
        for question in (GATHER, PLAIN):  # no gather setting: both read the same
            found = await ask(memory, store, question)
            assert found.block == AS_ALWAYS + with_weak([(0, "0.70")], THREE)
            assert ids_of(found.block) == ids[:4]  # the 0.30 sofa is under 0.40
            assert found.recall is not None
            assert found.recall.used == 1 and found.recall.weak == 3
            assert found.recall.candidates == 1
            assert [item.key for item in found.used] == [f"d:{ids[0]}"]
            assert found.recall.block_chars == len(found.block)
        # The cap is the setting, and the normal list is the same.
        cfg.memory.weak_matches = 2
        two = await ask(memory, store, PLAIN)
        assert two.block == AS_ALWAYS + with_weak([(0, "0.70")], THREE[:2])
        assert two.recall is not None and two.recall.weak == 2
    finally:
        await memory.aclose()


async def test_the_search_tool_and_the_excerpt_form_are_unchanged(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _, _ = await memory_of(tmp_path, store, recall_form="list", weak_matches=3)
    excerpt, _, _ = await memory_of(
        tmp_path / "e", store, recall_form="excerpt", weak_matches=3
    )
    plain, _, _ = await memory_of(tmp_path / "p", store, recall_form="excerpt")
    try:
        assert CHAT_TURN.get() is None
        assert await MemoryTools(memory).search(query=WORDS) == listed(STRONG)
        found = await ask(excerpt, store, PLAIN)
        assert found.block == (await ask(plain, store, PLAIN)).block
        assert "Weaker matches" not in found.block
        assert found.recall is not None and found.recall.weak == 0  # no list: none
        assert found.event()["recall"]["weak"] == 0  # type: ignore[index]
        assert await MemoryTools(excerpt).search(query=WORDS) == (
            await MemoryTools(plain).search(query=WORDS)
        )
    finally:
        await memory.aclose()
        await excerpt.aclose()
        await plain.aclose()


async def test_nothing_under_the_gather_bar_of_040_and_nothing_the_list_holds_comes(
    tmp_path: Path, store: SessionStore
) -> None:
    # gather asks the list to look wider; weak matches take the rest of the way: the
    # list holds 0.70 and 0.52, the weak matches are the two under 0.50, and neither
    # the 0.52 nor the 0.30 sofa is among them.
    memory, ids, cfg = await memory_of(
        tmp_path,
        store,
        recall_form="list",
        gather="wide",
        gather_min_cosine=0.50,
        weak_matches=3,
    )
    try:
        found = await ask(memory, store, GATHER)
        assert found.block == AS_ALWAYS + with_weak(
            [(0, "0.70"), (1, "0.52")], THREE[1:]
        )
        assert ids_of(found.block) == ids[:4]
        assert len(set(ids_of(found.block))) == 4  # none twice
        assert found.recall is not None
        assert found.recall.floor == 0.50 and found.recall.weak == 2
        # With the gather bar at 0.40 the list takes all four: there is no weak match.
        cfg.memory.gather_min_cosine = 0.40
        wide = await ask(memory, store, GATHER)
        assert wide.block == AS_ALWAYS + listed(
            STRONG, (1, "0.52"), (2, "0.45"), (3, "0.42")
        )
        assert "Weaker matches" not in wide.block
        assert wide.recall is not None and wide.recall.weak == 0
    finally:
        await memory.aclose()


async def test_a_turn_found_by_words_is_listed_once_not_again_as_weak(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, ids, _ = await memory_of(
        tmp_path, store, recall_form="list", weak_matches=5
    )
    try:
        # Its words are the question's, so words list it; its cosine, 0.46, is also a
        # weak match's, and it is not one: it is in the list already.
        _, (own,) = await add_turn(
            memory,
            store,
            "zebra yodel clinic visit",
            "Noted.",
            at=time.time() - 40 * DAY,
            cosine=0.46,
        )
        found = await ask(memory, store, "zebra yodel clinic visit")
        listed_ids = ids_of(found.block)
        assert listed_ids.count(own) == 1 and len(set(listed_ids)) == len(listed_ids)
        head, _, tail = found.block.partition(WEAK_GUIDE)
        assert f"[t{own}]" in head and f"[t{own}]" not in tail
        assert tail.count("\n[t") == 3 and found.recall is not None
        assert found.recall.weak == 3  # the three of the five turns under 0.55
    finally:
        await memory.aclose()


# ── Room ──


async def test_when_the_room_runs_out_the_weak_lines_go_first(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, ids, cfg = await memory_of(
        tmp_path, store, recall_form="list", weak_matches=3, recall_chars=200
    )
    try:
        # A list is not bound by the room: it is whole, with no weak line at all.
        found = await ask(memory, store, PLAIN)
        assert found.block == AS_ALWAYS + listed(STRONG)
        assert found.recall is not None and found.recall.weak == 0
        base = len(found.block) - 2  # the block's own text, with no "\n\n"
        # Room for the guide and one line: one weak match, the best.
        one = len(WEAK_GUIDE) + 1 + len(line_of(1, "0.52")) + 1
        cfg.memory.recall_chars = base + one
        fits = await ask(memory, store, PLAIN)
        assert fits.block == AS_ALWAYS + with_weak([(0, "0.70")], THREE[:1])
        assert fits.recall is not None and fits.recall.weak == 1
        # One character short of it: none, and no guide line either.
        cfg.memory.recall_chars = base + one - 1
        short = await ask(memory, store, PLAIN)
        assert short.block == found.block and short.recall is not None
        assert short.recall.weak == 0
        # Room for two of them: two, never the third.
        two = one + len(line_of(2, "0.45")) + 1
        cfg.memory.recall_chars = base + two + len(line_of(3, "0.42")) - 1
        both = await ask(memory, store, PLAIN)
        assert both.block == AS_ALWAYS + with_weak([(0, "0.70")], THREE[:2])
    finally:
        await memory.aclose()


async def test_weak_lines_standing_alone_keep_to_the_room_too(
    tmp_path: Path, store: SessionStore
) -> None:
    corpus: tuple[list[float], list[float]] = ([0.50, 0.46], [])
    memory, cfg = await memory_with(
        tmp_path, store, corpus, weak_matches=2, recall_form="list", recall_chars=200
    )
    try:
        # The header, the guide and one line are more than the smallest room: no
        # guide line without a line under it, and no block.
        found = await ask(memory, store, "zebra yodel")
        assert found.block == "" and found.recall is not None
        assert found.recall.weak == 0 and found.recall.block_chars == 0
        assert isinstance(cfg, Config)
        cfg.memory.recall_chars = 4000
        wider = await ask(memory, store, "zebra yodel")
        assert wider.recall is not None and wider.recall.weak == 2
        assert wider.block.startswith(f"\n\n{BLOCK_HEADER}\n{WEAK_GUIDE}\n[t")
    finally:
        await memory.aclose()


# ── The lines open ──


async def test_a_weak_line_opens_with_open_memory(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, ids, _ = await memory_of(
        tmp_path, store, recall_form="list", weak_matches=3
    )
    try:
        found = await ask(memory, store, PLAIN)
        weak = found.block.partition(WEAK_GUIDE)[2]
        ident = lines_of(weak)[0][1 : lines_of(weak)[0].index("]")]
        assert ident == f"t{ids[1]}"
        reply = await MemoryTools(memory).open_memory(ids=[ident])
        assert reply.startswith(f"[{ident}] ")
        assert reply.endswith(
            "the user said “I got fitted for hearing aids.”; melo replied "
            "“Good to hear it went well.”"
        )
    finally:
        await memory.aclose()


# ── The relative bar ──


async def test_under_a_relative_bar_the_weak_matches_are_what_is_under_it(
    tmp_path: Path, store: SessionStore
) -> None:
    bar = bar_of(MIDDLE)
    assert 0.40 < bar < 0.55
    answers, rest = MIDDLE
    memory, _ = await memory_with(
        tmp_path,
        store,
        MIDDLE,
        weak_matches=5,
        recall_form="list",
        recall_floor="relative",
    )
    try:
        found = await ask(memory, store, "zebra yodel")
        everything = [*answers, *rest]
        strong = sorted((c for c in everything if c >= bar), reverse=True)
        weak = sorted((c for c in everything if 0.40 <= c < bar), reverse=True)
        head, _, tail = found.block.partition(WEAK_GUIDE)
        assert shown(head) == [f"{c:.2f}" for c in strong]
        assert shown(tail) == [f"{c:.2f}" for c in weak[:5]]
        assert weak and found.recall is not None and found.recall.weak == len(weak)
        assert found.recall.floor == pytest.approx(bar)
        assert found.recall.floor_rule == "relative"
    finally:
        await memory.aclose()


async def test_when_the_relative_gate_fails_the_weak_matches_are_under_the_fixed_bar(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(
        tmp_path,
        store,
        WEAK,  # the best, 0.50, is under 0.55: nothing joins the vector list
        weak_matches=3,
        recall_form="list",
        recall_floor="relative",
    )
    try:
        found = await ask(memory, store, "zebra yodel")
        assert found.used == () and LIST_GUIDE not in found.block
        assert shown(found.block) == ["0.50", "0.46", "0.42"]
        assert found.block.split("\n")[3] == WEAK_GUIDE
        assert found.recall is not None
        assert found.recall.floor == 0.55 and found.recall.weak == 3
    finally:
        await memory.aclose()


# ── What is left out of the weak matches ──


def scored_of(*cosines: float) -> list[tuple[float, Item]]:
    return [(c, doc_item(f"d:{n}", f"memo {n}", c)) for n, c in enumerate(cosines)]


def test_pick_weak_takes_the_best_in_the_range_best_first() -> None:
    scored = scored_of(0.30, 0.41, 0.55, 0.46, 0.40, 0.399, 0.70, 0.50)
    weak = pick_weak(scored, floor=0.55, limit=3)
    assert [(i.key, i.similarity) for i in weak] == [
        ("d:7", 0.50),
        ("d:3", 0.46),
        ("d:1", 0.41),
    ]
    # Both ends: 0.40 counts, the floor itself does not, and under 0.40 never.
    every = pick_weak(scored, floor=0.55, limit=9)
    assert [i.similarity for i in every] == [0.50, 0.46, 0.41, 0.40]
    assert pick_weak(scored, floor=0.40, limit=9) == []
    assert pick_weak(scored, floor=0.30, limit=9) == []
    assert pick_weak(scored, floor=0.55, limit=0) == []
    assert pick_weak([], floor=0.55, limit=3) == []
    # Each is a copy; the corpus items are not touched.
    assert all(item.similarity is None for _, item in scored)


def test_pick_weak_never_repeats_what_the_list_holds() -> None:
    scored = scored_of(0.50, 0.48, 0.46, 0.44)
    held = [scored[1][1]]
    assert [i.key for i in pick_weak(scored, floor=0.55, limit=9, listed=held)] == [
        "d:0",
        "d:2",
        "d:3",
    ]
    # Another chunk of a turn the list lists is that turn again; one chunk of a turn
    # at most is a weak match, the best.
    chunk = lambda key, cosine, turn, n: Item(  # noqa: E731
        key, "text", "chat", "s1", turn, NOW, chunk=n, similarity=cosine
    )
    same = [
        (0.50, chunk("d:10", 0.50, 1, 0)),
        (0.45, chunk("d:11", 0.45, 2, 0)),
        (0.44, chunk("d:12", 0.44, 2, 1)),
    ]
    got = pick_weak(same, floor=0.55, limit=9, listed=[chunk("d:9", 0.7, 1, 2)])
    assert [i.key for i in got] == ["d:11"]
    # A turn a listed note came from is not repeated either, as pick leaves it out.
    note = Item("r:1", "You like tea.", "chat", "s1", 2, NOW)
    got = pick_weak(same, floor=0.55, limit=9, listed=[note])
    assert [i.key for i in got] == ["d:10"]


def corpus_with(*items: Item) -> Corpus:
    corpus = Corpus()
    for item in items:
        corpus.add(item)
    return corpus


def test_weak_scores_are_the_items_ranked_could_list_by_meaning() -> None:
    plain = doc_item("d:1", "plain", 0.50)
    other_model = doc_item("d:2", "other model", 0.50)
    other_model.embed_model = "another"
    no_vector = doc_item("d:3", "no vector")
    wrong_shape = doc_item("d:4", "wrong shape", 0.50)
    wrong_shape.vector = np.asarray([1.0, 0.0], np.float32)
    own_chat = doc_item("d:5", "own chat", 0.50)
    own_chat.session_id, own_chat.turn = "mine", 4
    older = doc_item("d:6", "older", 0.50)
    older.session_id, older.turn = "s", 7
    note = doc_item("r:7", "a note", 0.50)
    replaced_item = doc_item("m:8", "replaced item", 0.50)
    replaced_item.replaced_by = "m:9"
    corpus = corpus_with(
        plain, other_model, no_vector, wrong_shape, own_chat, older, note, replaced_item
    )
    args = (corpus, QUERY_VECTOR, MODEL)
    got = {item.key for _, item in weak_scores(*args)}
    assert got == {"d:1", "d:5", "d:6", "r:7"}  # model, shape, vector, replaced out
    # The chat's own turns that its model still shows are in view: left out.
    assert "d:5" not in {
        i.key for _, i in weak_scores(*args, exclude_session="mine", keep_from_turn=3)
    }
    assert "d:5" in {  # a turn it no longer shows is recalled like another chat's
        i.key for _, i in weak_scores(*args, exclude_session="mine", keep_from_turn=5)
    }
    # Without notes, no note; a turn a newer one replaced is left out.
    assert "r:7" not in {i.key for _, i in weak_scores(*args, notes=False)}
    newer = doc_item("d:20", "newer")
    gone = {i.key for _, i in weak_scores(*args, replaced={("s", 7): newer})}
    assert "d:6" not in gone and "d:1" in gone


async def test_memory_items_are_weak_matches_too_unless_a_newer_one_replaced_them(
    tmp_path: Path, store: SessionStore
) -> None:
    cfg = config(
        recall_source="items", recall_form="list", retriever="embedding", weak_matches=3
    )
    memory = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    provider = FixedEmbedding()
    memory.embedding = lambda: (provider, MODEL)  # type: ignore[method-assign]
    try:
        when = time.time() - 3 * DAY
        sid, _ = await add_turn(
            memory, store, "Where?", "Here.", at=when, title="Plans"
        )
        strong = await put(memory, sid, "Strong item", "Text one.", cosine=0.70)
        near = await put(memory, sid, "Near item", "Text two.", cosine=0.50)
        old = await put(memory, sid, "Old item", "Text three.", cosine=0.45)
        low = await put(memory, sid, "Lower item", "Text four.", cosine=0.42)
        far = await put(memory, sid, "Far item", "Text five.", cosine=0.30)
        found = await ask(memory, store, "zebra yodel")
        text = found.block[2:].split("\n")
        assert text[:2] == [BLOCK_HEADER, ITEM_LIST_GUIDE]
        assert text[2].startswith(f"[m{strong}] ") and text[3] == WEAK_GUIDE
        assert [line.split(" ")[0] for line in text[4:]] == [
            f"[m{near}]",
            f"[m{old}]",
            f"[m{low}]",
        ]
        assert str(far) not in "".join(line[:6] for line in text[4:])
        # The weak line is an item that opens, as a list line does.
        opened = await MemoryTools(memory).open_memory(ids=[f"m{near}"])
        assert "Near item" in opened and "Text two." in opened
        # A newer item replaced the old one: it is no weak match, the next one is.
        _, index = memory.stores()
        await index.mark_replaced(old, low)
        again = await ask(memory, store, "zebra yodel")
        weak = again.block.partition(WEAK_GUIDE)[2].split("\n")[1:]
        assert [line.split(" ")[0] for line in weak] == [f"[m{near}]", f"[m{low}]"]
        assert again.recall is not None and again.recall.weak == 2
    finally:
        await memory.aclose()


def test_a_weak_pick_holds_the_limit_and_nothing_found_yet() -> None:
    pick = WeakPick(3)
    assert pick.limit == 3 and pick.found == []
    assert Recall().weak is None and "weak" not in Recall().to_json()
    assert Recall(weak=0).to_json()["weak"] == 0


# ── render_list_block ──


def turn_text(typed: str, answer: str) -> str:
    return document_chunks(typed, answer)[0]


def test_render_list_block_of_nothing_is_nothing() -> None:
    assert render_list_block([]) == ""
    assert render_list_block([], weak_matches=3) == ""
    # Nothing at the bar and no weak matches asked for, or none close enough: nothing.
    entries = [("item", 0.50, "A"), ("turn", 0.45, turn_text("B", "C"))]
    assert render_list_block(entries) == ""
    assert render_list_block([("item", 0.39, "A")], weak_matches=3) == ""
    assert render_list_block(entries, floor=0.50, weak_matches=0).count("\n") >= 1


def test_render_list_block_of_a_gate_failed_question_with_three_weak_lines() -> None:
    entries = [
        ("item", 0.50, "Started physical therapy for my knee"),
        ("turn", 0.47, turn_text("Which running shoes for flat feet?", "Stability.")),
        ("item", 0.44, "Booked a dentist visit in March"),
        ("item", 0.41, "Moved my appointment to Friday"),
        ("item", 0.38, "The sofa arrived on Tuesday"),
    ]
    text = render_list_block(entries, weak_matches=3)
    today = [day(time.time() - n * DAY) for n in range(5)]
    assert text == "\n".join(
        [
            BLOCK_HEADER,
            WEAK_GUIDE,
            f"[m1] {today[0]} · Main chat · “Started physical therapy for my knee” · 0.50",
            f"[t1] {today[1]} · Main chat · “Which running shoes for flat feet?” · 0.47",
            f"[m2] {today[2]} · Main chat · “Booked a dentist visit in March” · 0.44",
        ]
    )
    # With none asked for, and with a bar low enough, the same entries read as a list.
    assert render_list_block(entries) == ""
    low = render_list_block(entries, floor=0.45).split("\n")
    assert low[:2] == [BLOCK_HEADER, ITEM_LIST_GUIDE] and len(low) == 4
    assert render_list_block(entries, floor=0.45, items=False).split("\n")[1] == (
        LIST_GUIDE
    )


def test_render_list_block_orders_by_cosine_caps_the_list_and_marks_kinds() -> None:
    entries = [
        ("note", 0.91, "I take my medicine at 8."),
        ("note", 0.90, "I swim on Fridays."),
        ("note", 0.89, "I like tea."),
        ("note", 0.88, "I dislike coffee."),  # a fourth note: over the notes' cap
        ("item", 0.80, "First"),
        ("item", 0.85, "Second"),
        ("turn", 0.45, turn_text("Weak turn", "Reply.")),
        ("note", 0.44, "I like zebras."),
        ("item", 0.20, "Far"),
    ]
    text = render_list_block(entries, weak_matches=5, items=False)
    lines = text.split("\n")
    assert lines[0] == BLOCK_HEADER
    notes = [line for line in lines if line.startswith("- ")]
    assert len(notes) == AUTO_RECORDS + 1  # three listed, one weak
    assert notes[-1].endswith(" · 0.44") and "I like zebras." in notes[-1]
    assert lines.index(LIST_GUIDE) > lines.index(notes[AUTO_RECORDS - 1])
    first = lines.index(LIST_GUIDE)
    assert [line.split(" · ")[2] for line in lines[first + 1 : first + 3]] == [
        "“Second”",
        "“First”",
    ]
    weak = lines[lines.index(WEAK_GUIDE) + 1 :]
    assert [line.rsplit(" · ", 1)[1] for line in weak] == ["0.45", "0.44"]
    # The ids are the entries' places among their kind, from 1.
    assert any(line.startswith("[t1] ") for line in weak)
    assert [line[:4] for line in lines if re.match(r"\[m\d", line)] == ["[m2]", "[m1]"]
    assert "Far" not in text and "I dislike coffee" not in text  # under 0.40; capped


def test_render_list_block_refuses_a_kind_it_does_not_know() -> None:
    with pytest.raises(ValueError, match="unknown kind 'chat'"):
        render_list_block([("chat", 0.9, "x")])


def test_render_list_block_keeps_the_weak_lines_inside_the_room() -> None:
    entries = [
        ("item", 0.70, "Strong"),
        ("item", 0.50, "Weak one"),
        ("item", 0.49, "Weak two"),
        ("item", 0.48, "Weak three"),
    ]
    day_of = [day(time.time() - n * DAY) for n in range(4)]
    base = render_list_block(entries, floor=0.55)
    line = f"[m2] {day_of[1]} · Main chat · “Weak one” · 0.50"
    one = len(base) + len(WEAK_GUIDE) + 1 + len(line) + 1
    assert render_list_block(entries, weak_matches=3, chars=one).endswith(line)
    assert render_list_block(entries, weak_matches=3, chars=one - 1) == base
    two = one + len(f"[m3] {day_of[2]} · Main chat · “Weak two” · 0.49") + 1
    got = render_list_block(entries, weak_matches=3, chars=two)
    assert got.count("“Weak ") == 2 and "“Weak three" not in got
    # The list is whole even when it does not fit: only the weak matches are cut.
    tiny = render_list_block(entries, weak_matches=3, chars=10)
    assert tiny == base and LIST_MAX > 3


# The question holds "zebra": its vector is [1, 0, 0]; the note is found by the
# stored vector the table gives it; the turns have theirs at the cosines given.
NOTE_WORDS = "I take my glucose readings every morning."
QUESTION = "Did I go to a zebra yodel clinic?"


class ByTable(BaseEmbeddingProvider):
    """The question is ``[1, 0, 0]``; a text with a key of the table is at that
    cosine to it; any other text is orthogonal."""

    model = MODEL

    def __init__(self, table: dict[str, float]) -> None:
        self.table = table

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            if "zebra" in text:
                out.append([1.0, 0.0, 0.0])
                continue
            cosine = next((c for k, c in self.table.items() if k in text), None)
            out.append([0.0, 0.0, 1.0] if cosine is None else vector_at(cosine))
        return out

    async def close(self) -> None:
        return None


async def front_turn(store: SessionStore, title: str):  # type: ignore[no-untyped-def]
    return await store.begin_turn(
        None,
        title,
        holder=HOLDER,
        title=title,
        new_session=NewSession(kind="front"),
    )


async def live_memory(
    tmp_path: Path,
    store: SessionStore,
    rows: list[tuple[str, float, str, str]],
    **memory: object,
) -> ChatMemory:
    """A memory holding *rows* (kind, cosine, typed, answer) in a "Main chat" each,
    entry *n* from *n* - 1 days ago, as ``render_list_block`` dates its entries."""
    memory.setdefault("retriever", "embedding")
    memory.setdefault("recall_form", "list")
    cfg = config(**memory)
    chat = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    provider = ByTable(
        {"glucose readings": next((c for k, c, _, _ in rows if k == "note"), 0.0)}
    )
    chat.embedding = lambda: (provider, MODEL)  # type: ignore[method-assign]
    for n, (kind, cosine, typed, answer) in enumerate(rows):
        if kind == "note":
            assert n == 0  # a note is dated now: the first entry is
            await teach(chat, store, await front_turn(store, "Notes"), typed)
            continue
        session_id = (await front_turn(store, typed)).session_id
        _, index = chat.stores()
        await index.add_many(
            [
                Document(
                    session_id=session_id,
                    turn=1,
                    surface="gateway",
                    kind="front",
                    title=typed,
                    text=text,
                    created_at=time.time() - n * DAY,
                    vector=tuple(vector_at(cosine)),
                    embed_model=MODEL,
                    chunk=m,
                )
                for m, text in enumerate(document_chunks(typed, answer))
            ]
        )
    return chat


def entries_of(rows: list[tuple[str, float, str, str]]) -> list[tuple[str, float, str]]:
    return [
        (
            kind,
            cosine,
            typed if kind == "note" else turn_text(typed, answer),
        )
        for kind, cosine, typed, answer in rows
    ]


SCENARIOS = {
    # A note at the bar, and the turns weaker: the gate passed on the note alone.
    "note listed": [
        ("note", 0.60, NOTE_WORDS, ""),
        ("turn", 0.52, "Fitted for hearing aids?", "Good."),
        ("turn", 0.45, "Bought a glucose meter?", "Handy."),
        ("turn", 0.42, "The ENT said the ear is fine.", "Glad."),
        ("turn", 0.30, "The sofa arrived.", "Enjoy."),
    ],
    # A note that is weak, a strong turn, a weaker turn and one that is too far.
    "note weak": [
        ("note", 0.47, NOTE_WORDS, ""),
        ("turn", 0.70, "Cardiologist moved my visit.", "To Friday."),
        ("turn", 0.52, "Fitted for hearing aids?", "Good."),
        ("turn", 0.30, "The sofa arrived.", "Enjoy."),
    ],
    # The gate failed: every line is weak.
    "gate failed": [
        ("turn", 0.50, "Fitted for hearing aids?", "Good."),
        ("turn", 0.46, "Bought a glucose meter?", "Handy."),
        ("turn", 0.42, "The ENT said the ear is fine.", "Glad."),
        ("turn", 0.39, "The sofa arrived.", "Enjoy."),
    ],
}


@pytest.mark.parametrize("weak", [0, 1, 3, 5])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_render_list_block_is_the_block_a_turn_shows_for_the_same_corpus(
    tmp_path: Path, store: SessionStore, name: str, weak: int
) -> None:
    rows = SCENARIOS[name]
    memory = await live_memory(tmp_path, store, rows, weak_matches=weak)
    try:
        found = await ask(memory, store, QUESTION)
        built = render_list_block(
            entries_of(rows), weak_matches=weak, items=False, chars=4000
        )
        assert found.block == ("\n\n" + built if built else "")
        # What the weak lines of each corpus are: all that fit under the setting.
        candidates = {"note listed": 3, "note weak": 2, "gate failed": 3}[name]
        assert found.recall is not None
        assert found.recall.weak == (min(weak, candidates) if weak else None)
        assert built.count(WEAK_GUIDE) == (1 if weak else 0)
    finally:
        await memory.aclose()


@pytest.mark.parametrize("chars", [200, 600, 4000])
async def test_the_room_is_the_same_live_and_in_render_list_block(
    tmp_path: Path, store: SessionStore, chars: int
) -> None:
    rows = SCENARIOS["note weak"]
    memory = await live_memory(
        tmp_path, store, rows, weak_matches=3, recall_chars=chars
    )
    try:
        found = await ask(memory, store, QUESTION)
        built = render_list_block(
            entries_of(rows), weak_matches=3, items=False, chars=chars
        )
        assert found.block == "\n\n" + built
    finally:
        await memory.aclose()


async def test_a_search_hint_follows_weak_lines_only_when_the_list_is_empty(
    tmp_path: Path, store: SessionStore
) -> None:
    rows = SCENARIOS["gate failed"]
    memory = await live_memory(
        tmp_path, store, rows, weak_matches=3, search_when_empty="hint"
    )
    try:
        found = await ask(memory, store, QUESTION)
        assert found.search_hint is True
        assert found.block.endswith(f"\n{SEARCH_HINT}") and WEAK_GUIDE in found.block
        assert found.recall is not None and found.recall.weak == 3
        assert found.recall.used == 0
        none = await ask(memory, store, "something else entirely")
        assert none.block == f"\n\n{BLOCK_HEADER}\n{SEARCH_HINT}"
        assert none.recall is not None and none.recall.weak == 0
    finally:
        await memory.aclose()


async def test_nothing_in_memory_is_nothing_with_weak_matches_on(
    tmp_path: Path, store: SessionStore
) -> None:
    cfg = config(recall_form="list", weak_matches=3, retriever="embedding")
    memory = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    provider = FixedEmbedding()
    memory.embedding = lambda: (provider, MODEL)  # type: ignore[method-assign]
    try:
        found = await ask(memory, store, QUESTION)
        assert found.block == "" and found.recall is not None
        assert found.recall.weak == 0
        assert await MemoryTools(memory).search(query=WORDS) == NOTHING_FOUND
    finally:
        await memory.aclose()


def test_the_turns_table_is_what_these_tests_think_it_is() -> None:
    assert [cosine for *_, cosine in TURNS] == [0.70, 0.52, 0.45, 0.42, 0.30]
    assert STRONG == (0, "0.70") and line_of(0, "0.70").startswith("[t1] ")
    assert LIST_MAX == 20 and AUTO_RECORDS == 3
