"""A personal adjustment to how notes are found: a query adapter (a developer setting).

``[memory] adapter = "on"`` (off by default) applies a small linear map ``W`` to
the embedding of a question before notes are searched. The stored note and turn
vectors are never touched, so there is no reindex, and recall still answers
from the notes: an edit or a forget takes effect at once. ``W`` (d by d, ``I``
at the start) is trained on this computer by NumPy from pairs melo already
holds, and rebuilt from scratch, never added to.

Pairs ``(query, note)`` come from three places, with no model labelling them:

- ``quote``: an active note and the user's own words it was made from, when the
  two differ by more than a trivial rewording;
- ``used``: the typed text of a turn whose ``turn.memory`` event lists the note
  as recalled;
- ``sleep``: a wording sleep merged away (O1) or superseded (O2, O4), and the
  note that kept its place.

Each pair has hard negatives: notes of the same topic with another subject, the
older values of the same subject, and notes of undone sleep changes. People and
Decisions notes, notes brought in from other assistants' files and secrets are
never trained on. Fewer than :data:`MIN_PAIRS` pairs means no adapter.

Training is InfoNCE (temperature 0.05) over the split's notes and the hard
negatives, with a penalty on the distance from ``I`` and early stopping on 10%
of the notes held out. Inputs are sorted and the seed fixed, so the same notes
give the same ``W``. The file is ``adapter.npz`` in the memory folder, mode
0600, written atomically; it names the embedding model and the dimension it was
built for, and is ignored for any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from localmelo.melo.memory._sqlite import make_private_dir
from localmelo.melo.memory.hippo.policy import content_tokens, overlap
from localmelo.melo.memory.long import StoredEntry
from localmelo.melo.memory.long.sqlite import SqliteLongTerm
from localmelo.support.memory import records

if TYPE_CHECKING:
    from localmelo.support.sessions.store import SessionStore

logger = logging.getLogger(__name__)

FILE: Final = "adapter.npz"
MIN_PAIRS: Final = 30
# Notes to hold out need at least this many distinct notes in the pairs.
MIN_NOTES: Final = 10
TRAINABLE_TOPICS: Final = ("about", "preferences", "projects")
TEMPERATURE: Final = 0.05
MAX_STEPS: Final = 200
HOLD_OUT: Final = 0.1
PATIENCE: Final = 20
PENALTY: Final = 1.0
SEED: Final = 373
# Adam's step is lr_scale / d: its first step moves W by about lr_scale in norm.
LR_SCALE: Final = 0.3
MAX_NEGATIVES: Final = 8
MAX_STALE: Final = 2
MAX_UNDONE: Final = 2
MAX_USED: Final = 2_000
MAX_QUERY_CHARS: Final = 2_000
# The words of a quote and its note that count as one wording above this overlap.
TRIVIAL: Final = 0.8
MAX_CHAIN: Final = 5
SOURCES: Final = ("quote", "used", "sleep")


# ── The adapter and its file ──


@dataclass(frozen=True)
class Adapter:
    """``W`` and what it was built for."""

    W: np.ndarray
    embed_model: str
    dim: int
    train_hash: str
    built_at: float
    pairs: int
    held_out_loss: float

    def matches(self, model: str, dim: int) -> bool:
        return bool(model) and self.embed_model == model and self.dim == dim

    def apply(self, vector: Sequence[float]) -> list[float] | None:
        """``W v``, unit length; ``None`` when *vector* has another size or the result is unusable."""
        v = np.asarray(vector, dtype=np.float32)
        if v.shape != (self.dim,):
            return None
        mapped = self.W @ v
        norm = float(np.linalg.norm(mapped))
        if not np.isfinite(norm) or norm <= 0:
            return None
        return [float(x) for x in mapped / norm]


def path_of(directory: str) -> str:
    return os.path.join(directory, FILE)


def save(path: str, adapter: Adapter) -> None:
    """Write *adapter* to *path* atomically with mode 0600 (a temporary file, then a rename)."""
    folder = os.path.dirname(path) or "."
    make_private_dir(folder)
    fd, temporary = tempfile.mkstemp(dir=folder, prefix=".adapter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez(
                handle,
                W=adapter.W.astype(np.float32),
                embed_model=np.array(adapter.embed_model),
                dim=np.array(adapter.dim),
                train_hash=np.array(adapter.train_hash),
                built_at=np.array(adapter.built_at),
                pairs=np.array(adapter.pairs),
                held_out_loss=np.array(adapter.held_out_loss),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def load(path: str) -> Adapter | None:
    """The adapter in *path*; ``None`` when there is none or the file is not a usable one."""
    try:
        with np.load(path, allow_pickle=False) as data:
            weights = np.asarray(data["W"], dtype=np.float32)
            dim = int(data["dim"])
            adapter = Adapter(
                weights,
                str(data["embed_model"]),
                dim,
                str(data["train_hash"]),
                float(data["built_at"]),
                int(data["pairs"]),
                float(data["held_out_loss"]),
            )
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("The query adapter file is not readable; ignoring it")
        return None
    if weights.shape != (dim, dim) or not np.isfinite(weights).all():
        return None
    return adapter


def remove(path: str) -> bool:
    """Delete the adapter file; whether there was one."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


# ── Pairs ──


@dataclass(frozen=True)
class Used:
    """A turn's typed text, when it began, and the note ids its recall carried."""

    typed: str
    at: float
    note_ids: tuple[int, ...]


@dataclass(frozen=True)
class Source:
    """What the stores hold, read once: the active notes, every other row a
    pair can name (older values, merged-away wordings, notes of undone sleep
    changes), sleep's merges and the recall events."""

    active: Sequence[StoredEntry]
    others: Mapping[int, StoredEntry] = field(default_factory=dict)
    merges: Sequence[tuple[int, int]] = ()  # (merged away, kept), from sleep's O1
    undone: Sequence[int] = ()  # ids of notes of undone sleep changes
    used: Sequence[Used] = ()


@dataclass(frozen=True)
class Pair:
    query: str
    note_id: int
    source: str  # one of SOURCES
    negatives: tuple[int, ...] = ()


@dataclass(frozen=True)
class TrainingSet:
    """The pairs, sorted, and every note a pair or a negative names."""

    pairs: tuple[Pair, ...]
    notes: Mapping[int, StoredEntry]

    def counts(self) -> dict[str, int]:
        return {s: sum(p.source == s for p in self.pairs) for s in SOURCES}

    def digest(self, model: str, dim: int) -> str:
        """sha256 of the model, the dimension and each sorted pair's note id and
        text, query and negatives."""
        rows = [
            [p.note_id, self.notes[p.note_id].text, p.query, list(p.negatives)]
            for p in self.pairs
        ]
        body = json.dumps([model, dim, rows], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


async def collect(store: SqliteLongTerm) -> Source:
    """Read the notes and sleep's changes from *store* (no model, no write)."""
    active = await store.active_entries()
    live = {n.id for n in active}
    others: dict[int, StoredEntry] = {}

    async def row(note_id: int | None) -> StoredEntry | None:
        if note_id is None or note_id in live:
            return None
        if note_id not in others and (found := await store.entry(note_id)):
            others[note_id] = found
        return others.get(note_id)

    for note in active:  # the values a note replaced, newest first
        parent = note.supersedes
        for _ in range(MAX_CHAIN):
            old = await row(parent)
            if old is None:
                break
            parent = old.supersedes
    merges: list[tuple[int, int]] = []
    undone: list[int] = []
    for decision in await store.decisions():
        reason = str(decision["reason"] or "")
        if decision["admitted_by"] != "sleep" or decision["target_id"] is None:
            continue
        if await row(decision["target_id"]) is None:
            continue
        if reason.startswith("merged into ") and decision["entry_id"] is not None:
            merges.append((decision["target_id"], decision["entry_id"]))
        elif reason.startswith("undone: sleep "):
            undone.append(decision["target_id"])
    return Source(active, others, merges, sorted(set(undone)))


async def used_turns(chats: SessionStore) -> list[Used]:
    """The turns of chats whose ``turn.memory`` event lists recalled notes."""
    from localmelo.support.memory.service import is_owner_question
    from localmelo.support.sessions.store import MEMORY_EVENT_TYPE

    out: list[Used] = []
    for session in await chats.list_sessions(limit=100_000, kinds=("chat", "front")):
        events = await chats.events_of_type(
            session.id, MEMORY_EVENT_TYPE, limit=100_000
        )
        seen = {e.turn: e.event.get("data", {}) for e in events if e.turn is not None}
        if not seen:
            continue
        for turn in await chats.list_turns(session.id):
            data = seen.get(turn.turn)
            if (
                data is None
                or turn.status != "completed"
                or turn.undone
                or is_owner_question(turn)
            ):
                continue
            ids = tuple(
                int(item["id"])
                for item in data.get("used") or []
                if isinstance(item, dict)
                and item.get("kind") == "record"
                and type(item.get("id")) is int
            )
            typed = turn.typed_text if turn.attachments else turn.user_text
            if ids:
                out.append(Used(typed, turn.started_at, ids))
    out.sort(key=lambda u: (-u.at, u.typed))
    return out[:MAX_USED]


def _plain(text: str) -> str:
    return " ".join(text.lower().split())


def _edited(note: StoredEntry) -> bool:
    return bool(note.provenance.get("edited"))


def _fits(note: StoredEntry, model: str, dim: int) -> bool:
    """Whether a note may be trained on: a topic that is not People or Decisions,
    not imported, no secret, and a vector of the model now set."""
    return (
        note.provenance.get("topic") in TRAINABLE_TOPICS
        and note.provenance.get("source") != "import"
        and note.admitted_by != "import"
        and note.embed_model == model
        and note.vector is not None
        and len(note.vector) == dim
        and not records.sensitive(note.text)
    )


def _trivial(quote: str, text: str) -> bool:
    """Whether *text* only rewords *quote*: nearly the same content words."""
    a, b = set(records.note_terms(quote)), set(records.note_terms(text))
    return bool(a and b) and len(a & b) / len(a | b) >= TRIVIAL


def _query_ok(text: str) -> bool:
    return (
        bool(text.strip())
        and len(text) <= MAX_QUERY_CHARS
        and not records.sensitive(text)
    )


def _unit(note: StoredEntry) -> np.ndarray:
    v = np.asarray(note.vector, dtype=np.float32)
    return v / max(float(np.linalg.norm(v)), 1e-12)


def build(source: Source, model: str, dim: int) -> TrainingSet:
    """The sorted pairs and their hard negatives for the embedding *model* of size *dim*."""
    active = [n for n in source.active if _fits(n, model, dim)]
    by_id = {n.id: n for n in active}
    merged = {old for old, _ in source.merges}
    undone = set(source.undone)
    older = {
        i: n
        for i, n in source.others.items()
        if n.status == "superseded" and _fits(n, model, dim)
    }
    found: dict[tuple[str, int], Pair] = {}

    def add(query: str, note_id: int, kind: str) -> None:
        note = by_id.get(note_id)
        if note is None or not _query_ok(query) or _plain(query) == _plain(note.text):
            return
        found.setdefault((_plain(query), note_id), Pair(query, note_id, kind))

    for note in active:
        quote = str(note.provenance.get("quote") or "")
        if quote and not _edited(note) and not _trivial(quote, note.text):
            add(quote, note.id, "quote")
    for turn in source.used:
        for note_id in dict.fromkeys(turn.note_ids):
            target = by_id.get(note_id)
            if target and float(target.provenance.get("edited_at") or 0) < turn.at:
                add(turn.typed, note_id, "used")
    for old_id, keeper in source.merges:
        if gone := older.get(old_id):
            add(gone.text, keeper, "sleep")
    for note in active:
        parent = older.get(note.supersedes) if note.supersedes is not None else None
        if parent and note.source == "sleep" and parent.id not in undone:
            add(parent.text, note.id, "sleep")

    stale = [n for i, n in older.items() if i not in merged | undone]
    wrong = [n for i, n in older.items() if i in undone]
    stale.sort(key=lambda n: -n.id)
    wrong.sort(key=lambda n: -n.id)
    pool = {n.id: n for n in [*active, *stale, *wrong]}
    keys = {i: records.subject_key(n.text) for i, n in pool.items()}
    matrix = np.stack([_unit(n) for n in active]) if active else np.zeros((0, dim))
    place = {n.id: at for at, n in enumerate(active)}
    wanted: dict[int, list[Pair]] = {}
    for pair in found.values():
        wanted.setdefault(pair.note_id, []).append(pair)
    pairs: list[Pair] = []
    for note_id, mine in sorted(wanted.items()):
        note = by_id[note_id]
        topic, key = note.provenance.get("topic"), keys[note_id]
        # Older values of the subject, then notes of undone changes, then the
        # nearest notes of the topic that say something else.
        near = [i for i in (n.id for n in stale) if key and keys[i] == key][:MAX_STALE]
        near += [
            n.id
            for n in wrong
            if n.supersedes == note_id or (key and keys[n.id] == key)
        ][:MAX_UNDONE]
        sims = matrix @ matrix[place[note_id]]
        terms = content_tokens(note.text)
        for at in sorted(range(len(active)), key=lambda i: (-float(sims[i]), i)):
            other = active[at]
            if len(near) >= MAX_NEGATIVES:
                break
            if (
                other.id != note_id
                and other.provenance.get("topic") == topic
                and not (key and keys[other.id] == key)
                and overlap(terms, content_tokens(other.text))
                < records.SUPERSEDE_OVERLAP
            ):
                near.append(other.id)
        for pair in mine:
            # An older wording that is the query is no negative of its own pair.
            kept = tuple(i for i in near if _plain(pool[i].text) != _plain(pair.query))
            pairs.append(Pair(pair.query, note_id, pair.source, kept))
    pairs.sort(key=lambda p: (p.note_id, _plain(p.query), p.source))
    named = sorted({i for p in pairs for i in (p.note_id, *p.negatives)})
    return TrainingSet(tuple(pairs), {i: pool[i] for i in named})


# ── Training ──


@dataclass(frozen=True)
class Trained:
    """The map, and how it and the identity score on the held-out notes (loss
    and mean reciprocal rank of the right note) at the step it was kept."""

    W: np.ndarray
    held_out_loss: float
    baseline_loss: float
    steps: int
    held_out_mrr: float
    baseline_mrr: float


def _masks(
    rows: np.ndarray,
    negatives: Sequence[Sequence[int]],
    pool: np.ndarray,
    width: int,
    drop: frozenset[int] = frozenset(),
) -> np.ndarray:
    """Which notes each pair of *rows* is scored against: the *pool* (the split's
    notes in the batch) and its own hard negatives, minus *drop*."""
    mask = np.zeros((len(rows), width), dtype=bool)
    mask[:, pool] = True
    for at, row in enumerate(rows):
        mask[at, [j for j in negatives[row] if j not in drop]] = True
    return mask


def _loss(
    w: np.ndarray,
    q: np.ndarray,
    n: np.ndarray,
    y: np.ndarray,
    mask: np.ndarray,
    grad: bool,
) -> tuple[float, np.ndarray | None, float]:
    """InfoNCE of ``normalize(w q)`` against the notes of *mask*, its gradient in
    *w* (when *grad*) and, without it, the mean reciprocal rank of the right note."""
    count = len(y)
    z = q @ w.T
    size = np.maximum(np.linalg.norm(z, axis=1, keepdims=True), 1e-12)
    u = z / size
    logits = np.where(mask, (u @ n.T) / TEMPERATURE, -np.inf)
    logits -= logits.max(axis=1, keepdims=True)
    weights = np.exp(logits)
    total = weights.sum(axis=1, keepdims=True)
    rows = np.arange(count)
    loss = float(np.mean(np.log(total[:, 0]) - logits[rows, y]))
    if not grad:
        ranks = 1 + (logits > logits[rows, y][:, None]).sum(axis=1)
        return loss, None, float(np.mean(1.0 / ranks))
    weights /= total
    weights[rows, y] -= 1.0
    g = (weights @ n) / (count * TEMPERATURE)
    g = (g - np.sum(g * u, axis=1, keepdims=True) * u) / size
    return loss, g.T @ q, 0.0


def train(
    queries: np.ndarray,
    notes: np.ndarray,
    positive: np.ndarray,
    negatives: Sequence[Sequence[int]],
    *,
    penalty: float = PENALTY,
    steps: int = MAX_STEPS,
    seed: int = SEED,
) -> Trained | None:
    """``W`` for unit *queries* (one row per pair) against unit *notes* (one row per note).

    *positive* is each pair's note (a row of *notes*) and *negatives* its hard
    negatives (rows of *notes*). 10% of the notes, never of the pairs, are held
    out: their pairs only measure the loss, and the ``W`` of the step with the
    lowest held-out loss is kept, though never one whose ranking of the held-out
    notes is worse than ``I``'s (a map that flattens every score lowers the loss
    and finds nothing). ``None`` when there are too few notes to hold any out or
    training does not beat ``I`` on them.
    """
    q, n = queries.astype(np.float32), notes.astype(np.float32)
    dim = q.shape[1]
    distinct = np.unique(positive)
    if len(distinct) < MIN_NOTES:
        return None
    rng = np.random.default_rng(seed)
    held = frozenset(
        int(i)
        for i in rng.permutation(distinct)[: max(1, round(HOLD_OUT * len(distinct)))]
    )
    is_held = np.array([int(p) in held for p in positive])
    fit, test = np.flatnonzero(~is_held), np.flatnonzero(is_held)
    pool = distinct[~np.isin(distinct, list(held))]
    fit_mask = _masks(fit, negatives, pool, len(n), held)
    test_mask = _masks(test, negatives, distinct, len(n))
    y_fit, y_test = positive[fit], positive[test]
    q_fit, q_test = q[fit], q[test]
    identity = np.eye(dim, dtype=np.float32)
    w = identity.copy()
    baseline, _, baseline_mrr = _loss(w, q_test, n, y_test, test_mask, False)
    best_loss, best_mrr = baseline, baseline_mrr
    best, best_step, quiet = None, 0, 0
    first, second = np.zeros_like(w), np.zeros_like(w)
    lr = LR_SCALE / dim
    for step in range(1, steps + 1):
        _, g, _ = _loss(w, q_fit, n, y_fit, fit_mask, True)
        assert g is not None
        g = g + 2.0 * penalty * (w - identity)
        first = 0.9 * first + 0.1 * g
        second = 0.999 * second + 0.001 * g * g
        w = w - lr * (first / (1 - 0.9**step)) / (
            np.sqrt(second / (1 - 0.999**step)) + 1e-8
        )
        loss, _, mrr = _loss(w, q_test, n, y_test, test_mask, False)
        if loss < best_loss - 1e-4 and mrr >= baseline_mrr:
            best, best_loss, best_mrr = w.copy(), loss, mrr
            best_step, quiet = step, 0
        elif best is not None:  # the loss may rise before it first falls
            quiet += 1
            if quiet >= PATIENCE:
                break
    if best is None:
        return None
    return Trained(best, best_loss, baseline, best_step, best_mrr, baseline_mrr)


# ── What a rebuild reports ──


@dataclass
class AdapterReport:
    """What one rebuild did: ``built``, ``removed`` (there are too few pairs, or
    training did not help), ``unchanged`` (the training set is the same as the
    saved adapter's) or ``skipped`` (*reason* says why nothing was done)."""

    state: str
    reason: str = ""
    pairs: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    held_out_loss: float | None = None
    baseline_loss: float | None = None
    held_out_mrr: float | None = None
    baseline_mrr: float | None = None
    steps: int = 0
    train_hash: str = ""
    seconds: float = 0.0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        if self.state in ("built", "removed"):
            data["event"] = {
                "type": "memory.adapter",
                "data": {
                    "state": self.state,
                    "pairs": self.pairs,
                    "loss": self.held_out_loss,
                },
            }
        return data


# ── Rebuilding ──

BATCH: Final = 16


def _dim(notes: Sequence[StoredEntry], model: str) -> int:
    """The vector size most notes of *model* have; 0 when none has a vector."""
    sizes = Counter(n.embed_dim for n in notes if n.embed_model == model and n.vector)
    return sizes.most_common(1)[0][0] if sizes else 0


async def _unchanged(store: SqliteLongTerm, training: TrainingSet) -> bool:
    """Whether every note of *training* is still there with its wording."""
    live = {n.id: n.text for n in await store.active_entries()}
    for note_id, note in training.notes.items():
        if note.status == "active":
            if live.get(note_id) != note.text:
                return False
        elif await store.entry(note_id) is None:
            return False
    return True


async def rebuild(
    store: SqliteLongTerm,
    directory: str,
    chats: SessionStore | None,
    *,
    model: str,
    embed: Callable[[Sequence[str]], Awaitable[list[list[float]] | None]],
    idle: Callable[[], Awaitable[None]],
    lock: asyncio.Lock,
    fresh: Callable[[], bool],
    penalty: float = PENALTY,
) -> AdapterReport:
    """Build the adapter from scratch and save it, or say why not.

    *embed* gives the vectors of questions as the embedding model reads them
    (``None`` when it cannot), *idle* raises ``RuntimeError`` while a lock file
    exists or the user is active (checked before each embedding call), *lock*
    is the memory folder's lock, held only to read and to save, and *fresh*
    says whether no forget, edit or undo has happened since the build began.
    *penalty* is the weight of the pull towards ``I``. The training itself is
    NumPy on a worker thread.
    """
    began = time.perf_counter()
    path = path_of(directory)

    def report(state: str, reason: str = "", **fields: Any) -> AdapterReport:
        return AdapterReport(
            state, reason, seconds=time.perf_counter() - began, **fields
        )

    try:
        await idle()
    except RuntimeError as exc:
        return report("skipped", str(exc))
    async with lock:
        source = await collect(store)
    if chats is not None:
        source = replace(source, used=await used_turns(chats))
    dim = _dim(source.active, model)
    if not model or not dim:
        return report(
            "skipped",
            "no note has a vector of the embedding model now set "
            "(melo memory reindex gives them one)",
        )
    training = build(source, model, dim)
    digest = training.digest(model, dim)
    known: dict[str, Any] = {
        "pairs": len(training.pairs),
        "by_source": training.counts(),
        "train_hash": digest,
    }
    distinct = len({p.note_id for p in training.pairs})
    if len(training.pairs) < MIN_PAIRS or distinct < MIN_NOTES:
        why = (
            f"{len(training.pairs)} pairs from {distinct} notes; at least "
            f"{MIN_PAIRS} pairs from {MIN_NOTES} notes are needed"
        )
        return report("removed" if remove(path) else "skipped", why, **known)
    saved = load(path)
    if saved is not None and saved.train_hash == digest and saved.matches(model, dim):
        return report(
            "unchanged",
            "same pairs as the saved adapter",
            **known | {"held_out_loss": saved.held_out_loss},
        )
    vectors: dict[str, list[float]] = {}
    texts = sorted({p.query for p in training.pairs})
    for start in range(0, len(texts), BATCH):
        try:
            await idle()
        except RuntimeError as exc:
            return report("skipped", str(exc), **known)
        batch = texts[start : start + BATCH]
        got = await embed(batch)
        if got is None or len(got) != len(batch) or any(len(v) != dim for v in got):
            return report(
                "skipped", "the embedding model could not embed the questions", **known
            )
        vectors.update(zip(batch, got, strict=True))
    ids = sorted(training.notes)
    row = {note_id: at for at, note_id in enumerate(ids)}
    notes = np.stack([_unit(training.notes[i]) for i in ids])
    queries = np.asarray([vectors[p.query] for p in training.pairs], dtype=np.float32)
    queries /= np.maximum(np.linalg.norm(queries, axis=1, keepdims=True), 1e-12)
    trained = await asyncio.to_thread(
        train,
        queries,
        notes,
        np.array([row[p.note_id] for p in training.pairs]),
        [[row[i] for i in p.negatives] for p in training.pairs],
        penalty=penalty,
    )
    if trained is None:
        why = "training did not beat the identity on the held-out notes"
        return report("removed" if remove(path) else "skipped", why, **known)
    async with lock:
        if not fresh() or not await _unchanged(store, training):
            return report(
                "skipped", "a note changed while the adapter was built", **known
            )
        save(
            path,
            Adapter(
                trained.W,
                model,
                dim,
                digest,
                time.time(),
                len(training.pairs),
                trained.held_out_loss,
            ),
        )
    return report(
        "built",
        held_out_loss=trained.held_out_loss,
        baseline_loss=trained.baseline_loss,
        held_out_mrr=trained.held_out_mrr,
        baseline_mrr=trained.baseline_mrr,
        steps=trained.steps,
        **known,
    )
