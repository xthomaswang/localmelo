"""The vector bar from the question's own similarities: ``[memory] recall_floor``.

With ``"fixed"`` (the default) one number, ``recall_min_cosine``, is the vector bar of
every question, and the block, the search result and the recall data stay as they
were. With ``"relative"`` the question's similarity to every item that has a vector
is looked at: when the best one is under ``recall_min_cosine`` nothing joins the
vector list; otherwise the bar is the larger of 0.40 and the mean of those
similarities plus ``recall_floor_z`` standard deviations. A question that ``gather``
widens keeps the ``gather`` bar. Synthetic turns and fixed vectors, no model.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest

from localmelo.support.config import MEMORY_RECALL_FLOOR, MEMORY_RECALL_FLOOR_Z
from localmelo.support.memory import ChatMemory
from localmelo.support.memory.index import Document
from localmelo.support.memory.recall import (
    GATHER_COSINE,
    MIN_COSINE,
    RELATIVE_MIN_ITEMS,
    RELATIVE_Z,
    Corpus,
    ranked,
    relative_floor,
    vector_floor,
)
from localmelo.support.memory.service import (
    CHAT_TURN,
    MemoryTools,
    Recall,
    RecallSettings,
)
from localmelo.support.sessions.store import SessionStore
from localmelo.tests.memory.test_gather_recall import (
    AS_ALWAYS,
    GATHER,
    NOTHING_FOUND,
    PLAIN,
    STRONG,
    WORDS,
    listed,
    memory_of,
)
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
    _turn,
    add_turn,
    ask,
    config,
    lines_of,
)
from localmelo.tests.sessions.helpers import Clock


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


# ── Memories whose similarities have a known spread ──

# The answers, then the rest of memory. Each corpus is a question's similarity to every
# item with a vector; the bar that "relative" makes of it is worked out below.
MIDDLE = ([0.70, 0.55, 0.50, 0.42, 0.36], [0.10 + 0.1 * n / 60 for n in range(60)])
# The rest sits so low that mean + 2.5 sigma is under 0.40: the bar stays 0.40.
LOW = ([0.70, 0.50, 0.45, 0.41, 0.39], [0.05 + 0.0005 * n for n in range(100)])
# Many items close to the question: mean + 2.5 sigma is over the fixed bar (0.55).
DENSE = (
    [0.56, 0.58, 0.61, 0.64, 0.67, 0.70, 0.73, 0.76],
    [0.40 + 0.0003 * n for n in range(60)],
)
# Two clusters far apart: mean + 2.5 sigma is over the best item, so the bar stops at it.
SPLIT = ([0.72, *[0.70] * 14], [0.10] * 15)
# One strong item over a background of thirty: the bar is 0.56, well under it.
OUTLIER = ([0.80], [0.30 + 0.002 * n for n in range(29)])
# The best item is under the fixed bar (0.55).
WEAK = ([0.50, 0.46, 0.42], [0.05 + 0.001 * n for n in range(100)])


def bar_of(corpus: tuple[list[float], list[float]], z: float = RELATIVE_Z) -> float:
    """The relative bar of *corpus* worked out by hand (the best item is over 0.55,
    and the corpus has the least number of items)."""
    values = [*corpus[0], *corpus[1]]
    cosines, best = np.asarray(values), max(values)
    assert len(values) >= RELATIVE_MIN_ITEMS and best >= 0.55
    bar = max(0.40, float(cosines.mean() + z * cosines.std()))
    return min(bar, best)


def shown(block: str) -> list[str]:
    """The cosines the list lines of a block or a search result end with, the
    largest first (the order of the lines also follows the age of each turn)."""
    return sorted((line.rsplit(" · ", 1)[1] for line in lines_of(block)), reverse=True)


def two(cosines: list[float]) -> list[str]:
    return [f"{cosine:.2f}" for cosine in cosines]


async def memory_with(
    tmp_path: Path,
    store: SessionStore,
    corpus: tuple[list[float], list[float]],
    **memory: object,
) -> tuple[ChatMemory, object]:
    """A memory with a chat turn for each of the answers (three days apart) and one
    chat with a turn for each of the rest, each at its cosine to the far question."""
    memory.setdefault("retriever", "embedding")
    memory.setdefault("recall_form", "list")
    cfg = config(**memory)
    chat = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    provider = FixedEmbedding()
    chat.embedding = lambda: (provider, MODEL)  # type: ignore[method-assign]
    answers, rest = corpus
    for n, cosine in enumerate(answers):
        await add_turn(
            chat,
            store,
            f"Entry {n}.",
            "Noted.",
            at=time.time() - (n + 1) * 3 * DAY,
            cosine=cosine,
        )
    session_id = (await _turn(store, "Background")).session_id
    _, index = chat.stores()
    await index.add_many(
        [
            Document(
                session_id=session_id,
                turn=n + 1,
                surface="gateway",
                kind="chat",
                title=f"Memo {n}",
                text=f"Memo {n}.",
                created_at=time.time() - (n + 10) * DAY,
                vector=tuple(vector_at(cosine)),
                embed_model=MODEL,
            )
            for n, cosine in enumerate(rest)
        ]
    )
    return chat, cfg


# ── The rule ──


def test_the_constants_and_the_defaults() -> None:
    assert MEMORY_RECALL_FLOOR == ("fixed", "relative")
    assert MEMORY_RECALL_FLOOR_Z == (1.5, 4.0)
    assert RELATIVE_Z == 2.5 and GATHER_COSINE == 0.40 and MIN_COSINE == 0.55
    assert RELATIVE_MIN_ITEMS == 20
    assert RecallSettings().floor == "fixed" and RecallSettings().floor_z == 2.5
    assert RecallSettings().relative is False


def test_relative_floor_is_the_mean_plus_z_deviations_never_under_the_gather_bar() -> (
    None
):
    answers, rest = MIDDLE
    cosines = [*answers, *rest]
    expected = float(np.mean(cosines) + 2.5 * np.std(cosines))  # the population one
    assert 0.40 < expected < 0.55
    assert relative_floor(cosines, 0.55, 2.5) == pytest.approx(expected)
    assert relative_floor(cosines, 0.55, 3.5) == pytest.approx(
        float(np.mean(cosines) + 3.5 * np.std(cosines))
    )
    # Never under 0.40, whatever the spread.
    low = [*LOW[0], *LOW[1]]
    assert np.mean(low) + 2.5 * np.std(low) < 0.40
    assert relative_floor(low, 0.55, 2.5) == 0.40
    # It may lie above the gate.
    dense = [*DENSE[0], *DENSE[1]]
    assert relative_floor(dense, 0.55, 2.5) == pytest.approx(bar_of(DENSE))
    assert bar_of(DENSE) > 0.55


def test_relative_floor_is_none_when_nothing_reaches_the_gate() -> None:
    weak = [*WEAK[0], *WEAK[1]]
    assert max(weak) == 0.50
    assert relative_floor(weak, 0.55, 2.5) is None
    # The gate is the recall bar the caller gives, and the best similarity counts.
    assert relative_floor(weak, 0.50, 2.5) is not None
    assert relative_floor(weak, 0.51, 2.5) is None


def test_relative_floor_is_the_fixed_bar_with_too_few_items_to_read_a_spread_from() -> (
    None
):
    few = [0.70, 0.52, 0.45, 0.42, 0.30]
    assert relative_floor(few, 0.55, 2.5) == 0.55
    assert relative_floor(few, 0.55, 1.5) == 0.55
    assert relative_floor([], 0.55, 2.5) == 0.55
    # The fixed bar also when the best is under it: nothing passes it either way.
    assert relative_floor([0.50, 0.40], 0.55, 2.5) == 0.55
    # One item short of the least number, and the least number: the rule starts.
    rest = [0.10] * (RELATIVE_MIN_ITEMS - 2)
    assert relative_floor([0.70, *rest], 0.55, 2.5) == 0.55
    assert len(rest) + 2 == RELATIVE_MIN_ITEMS
    assert relative_floor([0.70, 0.69, *rest], 0.55, 2.5) != 0.55
    assert relative_floor([0.50, *rest, 0.40], 0.55, 2.5) is None


def test_relative_floor_never_goes_over_the_best_similarity() -> None:
    cosines = [*SPLIT[0], *SPLIT[1]]
    assert len(cosines) == 30
    assert np.mean(cosines) + 2.5 * np.std(cosines) > max(cosines)  # the rule alone
    for z in (1.5, 2.5, 4.0):
        assert relative_floor(cosines, 0.55, z) == max(cosines) == 0.72
    # A bar under the best similarity is the rule's own.
    outlier = [*OUTLIER[0], *OUTLIER[1]]
    assert relative_floor(outlier, 0.55, 2.5) == pytest.approx(bar_of(OUTLIER))
    assert bar_of(OUTLIER) < 0.80


def corpus_of(cosines: list[float]) -> Corpus:
    corpus = Corpus()
    for n, cosine in enumerate(cosines):
        corpus.add(doc_item(f"d:{n}", f"memo {n}", cosine))
    return corpus


def test_ranked_with_relative_z_lists_the_items_over_the_question_s_own_bar() -> None:
    cosines = [*MIDDLE[0], *MIDDLE[1]]
    corpus = corpus_of(cosines)
    now = NOW + 1
    args = (corpus, "zebra yodel", QUERY_VECTOR, MODEL)
    fixed = ranked(*args, now=now)
    assert [item.key for item in fixed] == ["d:0", "d:1"]  # 0.70 and 0.55: over 0.55
    relative = ranked(*args, now=now, relative_z=2.5)
    assert [item.key for item in relative] == ["d:0", "d:1", "d:2"]  # and 0.50
    # None is the rule of today, and the gate is the recall bar.
    assert ranked(*args, now=now, relative_z=None) == fixed
    assert ranked(*args, now=now, min_cosine=0.75, relative_z=2.5) == []
    # A lower z lets more through, a higher one fewer.
    assert len(ranked(*args, now=now, relative_z=1.5)) > len(relative)
    assert len(ranked(*args, now=now, relative_z=4.0)) < len(relative)
    # The bar of a corpus whose best similarity is under the gate: nothing.
    weak = corpus_of([*WEAK[0], *WEAK[1]])
    assert ranked(weak, "zebra yodel", QUERY_VECTOR, MODEL, now=now) == []
    assert (
        ranked(weak, "zebra yodel", QUERY_VECTOR, MODEL, now=now, relative_z=2.5) == []
    )
    # The same memory with the fixed bar lowered: the best items do pass.
    lowered = ranked(weak, "zebra yodel", QUERY_VECTOR, MODEL, now=now, min_cosine=0.40)
    assert [item.key for item in lowered] == ["d:0", "d:1", "d:2"]


def test_ranked_with_few_items_is_the_fixed_bar_and_lists_the_best_item_of_a_spread() -> (
    None
):
    now = NOW + 1
    few = corpus_of([0.70, 0.52, 0.45, 0.42, 0.30])
    args = (few, "zebra yodel", QUERY_VECTOR, MODEL)
    assert ranked(*args, now=now, relative_z=2.5) == ranked(*args, now=now)
    assert [item.key for item in ranked(*args, now=now, relative_z=2.5)] == ["d:0"]
    # The spread of thirty items would put the bar over every one of them.
    split = corpus_of([*SPLIT[0], *SPLIT[1]])
    args = (split, "zebra yodel", QUERY_VECTOR, MODEL)
    assert len(ranked(*args, now=now)) == 15  # fixed: every item over 0.55
    for z in (1.5, 2.5, 4.0):
        listed_keys = [item.key for item in ranked(*args, now=now, relative_z=z)]
        assert listed_keys == ["d:0"]  # the best item, and no other
    outlier = corpus_of([*OUTLIER[0], *OUTLIER[1]])
    found = ranked(outlier, "zebra yodel", QUERY_VECTOR, MODEL, now=now, relative_z=2.5)
    assert [item.key for item in found] == ["d:0"]


def test_vector_floor_is_the_bar_ranked_puts_on_the_vector_list() -> None:
    corpus = corpus_of([*MIDDLE[0], *MIDDLE[1]])
    assert vector_floor(corpus, QUERY_VECTOR, MODEL, 0.55, 2.5) == pytest.approx(
        bar_of(MIDDLE), abs=1e-6
    )
    # Vectors of another model, or none, are no part of the question's background.
    corpus.add(doc_item("d:other", "memo other", None))
    assert vector_floor(corpus, QUERY_VECTOR, MODEL, 0.55, 2.5) == pytest.approx(
        bar_of(MIDDLE), abs=1e-6
    )
    # With no item of that model there is nothing to read a spread from.
    assert vector_floor(corpus, QUERY_VECTOR, "another-model", 0.55, 2.5) == 0.55
    weak = corpus_of([*WEAK[0], *WEAK[1]])
    assert vector_floor(weak, QUERY_VECTOR, MODEL, 0.55, 2.5) is None


def test_ranked_with_relative_z_and_no_query_vector_is_the_words_alone() -> None:
    corpus = corpus_of([*MIDDLE[0], *MIDDLE[1]])
    assert ranked(corpus, "memo", None, "", now=NOW + 1, relative_z=2.5) == ranked(
        corpus, "memo", None, "", now=NOW + 1
    )


# ── The default changes nothing ──


@pytest.mark.parametrize("setting", [None, "fixed"])
@pytest.mark.parametrize("form", ["list", "excerpt"])
async def test_fixed_leaves_the_block_the_search_result_and_the_recall_data_as_they_were(
    tmp_path: Path, store: SessionStore, form: str, setting: str | None
) -> None:
    # The same pins as the gather setting's "off", with the bar's own settings given.
    extra: dict[str, object] = {} if setting is None else {"recall_floor": setting}
    memory, _, _ = await memory_of(
        tmp_path, store, recall_form=form, recall_floor_z=3.5, **extra
    )
    try:
        for question in (GATHER, PLAIN):
            found = await ask(memory, store, question)
            data = found.event()["recall"]  # type: ignore[index]
            assert set(data) == {
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
            assert found.recall is not None
            assert found.recall.gather is None and found.recall.floor is None
            assert found.recall.floor_rule is None
            assert data["candidates"] == 1 and data["used"] == 1
            reply = await MemoryTools(memory).search(query=WORDS)
            if form == "list":
                assert found.block == AS_ALWAYS + listed(STRONG)
                assert reply == listed(STRONG)
            else:
                assert found.block.startswith(AS_ALWAYS + "- the user said")
                assert reply.startswith("1. the user said")
            assert data["block_chars"] == len(found.block)
    finally:
        await memory.aclose()


def test_the_recall_data_carries_the_bar_and_its_rule_only_when_one_is_on() -> None:
    off = Recall().to_json()
    assert not {"gather", "floor", "floor_rule"} & set(off)
    only_floor = Recall(floor=0.44, floor_rule="relative").to_json()
    assert only_floor["floor"] == 0.44 and only_floor["floor_rule"] == "relative"
    assert "gather" not in only_floor
    both = Recall(gather=True, floor=0.4, floor_rule="gather").to_json()
    assert both["gather"] is True and both["floor_rule"] == "gather"
    kept = {k: v for k, v in both.items() if k not in ("gather", "floor", "floor_rule")}
    assert kept == off


# ── Relative: the block ──


async def test_relative_lists_down_to_the_question_s_own_bar_and_not_below_it(
    tmp_path: Path, store: SessionStore
) -> None:
    fixed, _ = await memory_with(tmp_path / "a", store, MIDDLE)
    relative, _ = await memory_with(
        tmp_path / "b", store, MIDDLE, recall_floor="relative"
    )
    try:
        bar = bar_of(MIDDLE)
        assert 0.42 < bar < 0.50  # the third answer is over it, the fourth under
        today = await ask(fixed, store, PLAIN)
        assert shown(today.block) == two([0.70, 0.55])  # as before: over 0.55 only
        found = await ask(relative, store, PLAIN)
        assert shown(found.block) == two([0.70, 0.55, 0.50])
        assert found.block.startswith(AS_ALWAYS)
        assert found.recall is not None and found.recall.floor_rule == "relative"
        assert found.recall.floor == pytest.approx(bar, abs=1e-6)
        assert found.recall.gather is None  # gather is off
        assert found.recall.used == found.recall.candidates == 3
        data = found.event()["recall"]  # type: ignore[index]
        assert data["floor_rule"] == "relative" and "gather" not in data
        assert data["floor"] == pytest.approx(bar, abs=1e-6)
        assert data["block_chars"] == len(found.block)
    finally:
        await fixed.aclose()
        await relative.aclose()


async def test_relative_never_goes_below_the_lowest_bar_and_z_moves_it(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, cfg = await memory_with(tmp_path, store, LOW, recall_floor="relative")
    try:
        cosines = np.asarray([*LOW[0], *LOW[1]])
        assert cosines.mean() + 2.5 * cosines.std() < 0.40  # the bar is its floor, 0.40
        found = await ask(memory, store, PLAIN)
        assert shown(found.block) == two([0.70, 0.50, 0.45, 0.41])  # not 0.39
        assert found.recall is not None and found.recall.floor == 0.40
        assert found.recall.floor_rule == "relative"
        # A lower z cannot take the bar under 0.40 either.
        cfg.memory.recall_floor_z = 1.5  # type: ignore[attr-defined]
        again = await ask(memory, store, PLAIN)
        assert shown(again.block) == two([0.70, 0.50, 0.45, 0.41])
        assert again.recall is not None and again.recall.floor == 0.40
    finally:
        await memory.aclose()


async def test_z_is_read_each_turn(tmp_path: Path, store: SessionStore) -> None:
    memory, cfg = await memory_with(tmp_path, store, MIDDLE, recall_floor="relative")
    try:
        cfg.memory.recall_floor_z = 4.0  # type: ignore[attr-defined]
        high = await ask(memory, store, PLAIN)
        assert high.recall is not None
        assert high.recall.floor == pytest.approx(bar_of(MIDDLE, 4.0), abs=1e-6)
        assert shown(high.block) == two([0.70])  # 4 sigma is over 0.55: the best only
        cfg.memory.recall_floor_z = 1.5  # type: ignore[attr-defined]
        low = await ask(memory, store, PLAIN)
        assert low.recall is not None
        assert low.recall.floor == pytest.approx(bar_of(MIDDLE, 1.5), abs=1e-6)
        assert shown(low.block) == two([0.70, 0.55, 0.50, 0.42])
    finally:
        await memory.aclose()


async def test_a_question_whose_best_item_is_under_the_recall_bar_lists_nothing(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(tmp_path, store, WEAK, recall_floor="relative")
    try:
        cosines = np.asarray([*WEAK[0], *WEAK[1]])
        assert cosines.mean() + 2.5 * cosines.std() < 0.40  # the bar alone would list
        found = await ask(memory, store, PLAIN)
        assert found.block == "" and found.used == ()
        assert found.recall is not None and found.recall.used == 0
        assert found.recall.candidates == 0
        # The bar reported is the gate: no match of this question reaches it.
        assert found.recall.floor == 0.55 and found.recall.floor_rule == "relative"
        assert await MemoryTools(memory).search(query=WORDS) == NOTHING_FOUND
    finally:
        await memory.aclose()


async def test_a_bar_over_the_fixed_one_lists_fewer_items_than_the_fixed_bar(
    tmp_path: Path, store: SessionStore
) -> None:
    fixed, _ = await memory_with(tmp_path / "a", store, DENSE)
    relative, _ = await memory_with(
        tmp_path / "b", store, DENSE, recall_floor="relative"
    )
    try:
        bar = bar_of(DENSE)
        assert 0.64 < bar < 0.67
        today = await ask(fixed, store, PLAIN)
        assert shown(today.block) == two(sorted(DENSE[0], reverse=True))  # all eight
        found = await ask(relative, store, PLAIN)
        assert shown(found.block) == two([0.76, 0.73, 0.70, 0.67])
        assert found.recall is not None and found.recall.floor is not None
        assert found.recall.floor > MIN_COSINE
        assert found.recall.floor == pytest.approx(bar, abs=1e-6)
    finally:
        await fixed.aclose()
        await relative.aclose()


async def test_a_memory_of_few_items_lists_what_the_fixed_bar_lists(
    tmp_path: Path, store: SessionStore
) -> None:
    # Five items with vectors: no spread to read, the bar is the fixed one. (Their
    # spread alone would put the bar over the best item, 0.70, and list nothing.)
    fixed, _, _ = await memory_of(tmp_path / "a", store, recall_form="list")
    relative, _, _ = await memory_of(
        tmp_path / "b", store, recall_form="list", recall_floor="relative"
    )
    try:
        for question in (PLAIN, GATHER):
            today = await ask(fixed, store, question)
            found = await ask(relative, store, question)
            assert found.block == today.block == AS_ALWAYS + listed(STRONG)
            assert found.recall is not None
            assert found.recall.floor == 0.55 and found.recall.floor_rule == "relative"
        reply = await MemoryTools(relative).search(query=WORDS)
        assert reply == await MemoryTools(fixed).search(query=WORDS) == listed(STRONG)
    finally:
        await fixed.aclose()
        await relative.aclose()


async def test_the_bar_stops_at_the_best_item_of_a_wide_spread(
    tmp_path: Path, store: SessionStore
) -> None:
    fixed, _ = await memory_with(tmp_path / "a", store, SPLIT)
    relative, _ = await memory_with(
        tmp_path / "b", store, SPLIT, recall_floor="relative"
    )
    try:
        today = await ask(fixed, store, PLAIN)
        assert shown(today.block) == two([0.72, *[0.70] * 14])
        found = await ask(relative, store, PLAIN)
        assert shown(found.block) == two([0.72])  # the best item, and no other
        assert found.recall is not None and found.recall.floor_rule == "relative"
        assert found.recall.floor == pytest.approx(0.72, abs=1e-6)
        assert shown(await MemoryTools(relative).search(query=WORDS)) == two([0.72])
    finally:
        await fixed.aclose()
        await relative.aclose()


async def test_one_strong_outlier_among_thirty_items_is_listed(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(tmp_path, store, OUTLIER, recall_floor="relative")
    try:
        found = await ask(memory, store, PLAIN)
        assert "0.80" in shown(found.block)
        assert shown(found.block) == two([0.80])
        assert found.recall is not None
        assert found.recall.floor == pytest.approx(bar_of(OUTLIER), abs=1e-6)
    finally:
        await memory.aclose()


async def test_the_excerpt_form_gets_the_relative_bar_too(
    tmp_path: Path, store: SessionStore
) -> None:
    fixed, _ = await memory_with(tmp_path / "a", store, MIDDLE, recall_form="excerpt")
    relative, _ = await memory_with(
        tmp_path / "b", store, MIDDLE, recall_form="excerpt", recall_floor="relative"
    )
    try:
        today = await ask(fixed, store, PLAIN)
        found = await ask(relative, store, PLAIN)
        assert today.recall is not None and found.recall is not None
        assert today.recall.candidates == 2 and found.recall.candidates == 3
        assert found.recall.floor_rule == "relative"
        assert found.recall.floor == pytest.approx(bar_of(MIDDLE), abs=1e-6)
    finally:
        await fixed.aclose()
        await relative.aclose()


async def test_a_message_with_nothing_to_search_reports_the_gate_and_the_rule(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(tmp_path, store, MIDDLE, recall_floor="relative")
    try:
        found = await ask(memory, store, "   ")
        assert found.recall is not None and found.recall.query_from == "none"
        assert found.recall.floor == 0.55 and found.recall.floor_rule == "relative"
    finally:
        await memory.aclose()


async def test_without_vectors_the_words_alone_answer_and_the_bar_is_unused(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(
        tmp_path, store, MIDDLE, recall_floor="relative", retriever="bm25"
    )
    try:
        found = await ask(memory, store, PLAIN)
        assert found.recall is not None and found.recall.retriever == "bm25"
        assert found.block == ""  # no word of the question is in a turn
        assert found.recall.floor == 0.55 and found.recall.floor_rule == "relative"
    finally:
        await memory.aclose()


# ── Relative: the search tool ──


async def test_a_list_form_search_takes_the_relative_bar(
    tmp_path: Path, store: SessionStore
) -> None:
    fixed, _ = await memory_with(tmp_path / "a", store, MIDDLE)
    relative, _ = await memory_with(
        tmp_path / "b", store, MIDDLE, recall_floor="relative"
    )
    try:
        assert CHAT_TURN.get() is None  # no turn is under way
        assert shown(await MemoryTools(fixed).search(query=WORDS)) == two([0.70, 0.55])
        reply = await MemoryTools(relative).search(query=WORDS)
        assert shown(reply) == two([0.70, 0.55, 0.50])
        assert shown(await MemoryTools(relative).search(query=WORDS, k=2)) == two(
            [0.70, 0.55]
        )
    finally:
        await fixed.aclose()
        await relative.aclose()


async def test_a_search_with_no_vector_match_in_range_finds_nothing(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(tmp_path, store, WEAK, recall_floor="relative")
    try:
        assert await MemoryTools(memory).search(query=WORDS) == NOTHING_FOUND
    finally:
        await memory.aclose()


async def test_the_search_of_the_excerpt_form_keeps_the_fixed_bar(
    tmp_path: Path, store: SessionStore
) -> None:
    fixed, _ = await memory_with(tmp_path / "a", store, MIDDLE, recall_form="excerpt")
    relative, _ = await memory_with(
        tmp_path / "b", store, MIDDLE, recall_form="excerpt", recall_floor="relative"
    )
    try:
        today = await MemoryTools(fixed).search(query=WORDS)
        assert len(today.split("\n")) == 2
        assert await MemoryTools(relative).search(query=WORDS) == today
    finally:
        await fixed.aclose()
        await relative.aclose()


# ── Precedence over the gather bar ──


async def test_a_gathering_question_keeps_the_gather_bar_under_wide_and_relative(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(
        tmp_path, store, MIDDLE, recall_floor="relative", gather="wide"
    )
    try:
        found = await ask(memory, store, GATHER)
        assert shown(found.block) == two([0.70, 0.55, 0.50, 0.42])  # down to 0.40
        assert found.recall is not None and found.recall.gather is True
        assert found.recall.floor == 0.40 and found.recall.floor_rule == "gather"
        data = found.event()["recall"]  # type: ignore[index]
        assert data["floor_rule"] == "gather" and data["floor"] == 0.40
        # Any other question on the same memory takes the relative bar.
        plain = await ask(memory, store, PLAIN)
        assert shown(plain.block) == two([0.70, 0.55, 0.50])
        assert plain.recall is not None and plain.recall.gather is False
        assert plain.recall.floor_rule == "relative"
        assert plain.recall.floor == pytest.approx(bar_of(MIDDLE), abs=1e-6)
    finally:
        await memory.aclose()


async def test_gather_with_the_fixed_bar_reports_its_rule_too(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(tmp_path, store, MIDDLE, gather="wide")
    try:
        found = await ask(memory, store, GATHER)
        assert found.recall is not None and found.recall.floor_rule == "gather"
        plain = await ask(memory, store, PLAIN)
        assert plain.recall is not None and plain.recall.floor == 0.55
        assert plain.recall.floor_rule == "fixed"
        assert shown(plain.block) == two([0.70, 0.55])
    finally:
        await memory.aclose()


async def test_in_the_excerpt_form_gather_has_no_bar_so_relative_applies(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(
        tmp_path,
        store,
        MIDDLE,
        recall_form="excerpt",
        recall_floor="relative",
        gather="wide",
    )
    try:
        found = await ask(memory, store, GATHER)
        assert found.recall is not None and found.recall.gather is True
        assert found.recall.floor_rule == "relative"
        assert found.recall.floor == pytest.approx(bar_of(MIDDLE), abs=1e-6)
    finally:
        await memory.aclose()


async def test_a_search_in_a_gathering_turn_goes_down_to_the_gather_bar(
    tmp_path: Path, store: SessionStore
) -> None:
    memory, _ = await memory_with(
        tmp_path, store, MIDDLE, recall_floor="relative", gather="wide"
    )
    tools = MemoryTools(memory)
    try:
        await ask(memory, store, GATHER)
        assert shown(await tools.search(query=WORDS)) == two([0.70, 0.55, 0.50, 0.42])
        # In a turn that does not count or compare, and outside any turn, the
        # search sets its bar from the question's own similarities.
        await ask(memory, store, PLAIN)
        assert shown(await tools.search(query=WORDS)) == two([0.70, 0.55, 0.50])
        CHAT_TURN.set(None)
        assert shown(await tools.search(query=WORDS)) == two([0.70, 0.55, 0.50])
    finally:
        await memory.aclose()


# ── The settings ──


async def test_the_settings_are_read_each_turn_and_a_bad_one_is_the_default(
    tmp_path: Path,
) -> None:
    cfg = config()
    memory = ChatMemory(lambda: cfg, directory=str(tmp_path / "memory"))
    try:
        first = memory.recall_settings()
        assert (first.floor, first.floor_z, first.relative) == ("fixed", 2.5, False)
        cfg.memory.recall_floor = "relative"
        cfg.memory.recall_floor_z = 3.0
        now = memory.recall_settings()
        assert (now.floor, now.floor_z, now.relative) == ("relative", 3.0, True)
        for z in (1.5, 4.0, 2):
            cfg.memory.recall_floor_z = z
            assert memory.recall_settings().floor_z == float(z)
        for bad in ("narrow", True, 1, None):
            cfg.memory.recall_floor = bad  # type: ignore[assignment]
            assert memory.recall_settings().floor == "fixed", bad
        for value in (1.4, 4.1, True, "2.5", None, float("nan"), math.inf):
            cfg.memory.recall_floor_z = value  # type: ignore[assignment]
            assert memory.recall_settings().floor_z == 2.5, value
    finally:
        await memory.aclose()
