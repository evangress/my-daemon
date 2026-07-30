# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from my_daemon.models import Chunk, DateRange, Note, RetrievedChunk


def test_open_ended_on_either_side_is_allowed():
    """Required by the deferred NL step: an open-ended phrase must not invent a bound."""
    assert DateRange(since=datetime(2026, 1, 1, tzinfo=UTC)).until is None
    assert DateRange(until=datetime(2026, 1, 1, tzinfo=UTC)).since is None


def test_inverted_range_is_rejected():
    with pytest.raises(ValidationError):
        DateRange(
            since=datetime(2026, 6, 1, tzinfo=UTC),
            until=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_equal_bounds_are_allowed_and_select_nothing():
    """`until` is exclusive, so since == until is an empty half-open interval."""
    t = datetime(2026, 6, 1, tzinfo=UTC)
    assert DateRange(since=t, until=t).is_empty is True


def test_a_fully_open_range_is_inert():
    assert DateRange().is_active is False


def test_a_normal_range_is_active_and_not_empty():
    r = DateRange(
        since=datetime(2026, 3, 1, tzinfo=UTC),
        until=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert r.is_active is True
    assert r.is_empty is False


def test_the_model_is_frozen():
    """Callers pass this into retrieval; it must not be mutable en route."""
    import pydantic

    r = DateRange(since=datetime(2026, 3, 1, tzinfo=UTC))
    with pytest.raises(pydantic.ValidationError):
        r.since = datetime(2026, 4, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# IV.10 Task 7: the pre-filter reaches both retrieval arms.
# ---------------------------------------------------------------------------

DIM = 4

RANGE = DateRange(since=datetime(2026, 3, 1, tzinfo=UTC), until=datetime(2026, 6, 1, tzinfo=UTC))


def _vector_store(tmp_path: Path, name: str = "q"):
    from my_daemon.stores.vector import VectorStore

    store = VectorStore(
        url=None, collection="chunks", dim=DIM, hybrid=False, path=str(tmp_path / name)
    )
    store.ensure_collection()
    return store


def _three_chunks() -> list[Chunk]:
    """In-range, out-of-range, and undated — one note each."""
    return [
        Chunk(
            id="in",
            note_uuid="A",
            note_path="in.md",
            text="alpha",
            chunk_index=0,
            occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
            occurred_at_source="frontmatter",
        ),
        Chunk(
            id="out",
            note_uuid="B",
            note_path="out.md",
            text="alpha",
            chunk_index=0,
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
            occurred_at_source="frontmatter",
        ),
        Chunk(id="undated", note_uuid="C", note_path="undated.md", text="alpha", chunk_index=0),
    ]


def _seeded_store_and_chunks(tmp_path: Path):
    chunks = _three_chunks()
    store = _vector_store(tmp_path)
    store.upsert(chunks, [[1.0, 0, 0, 0]] * 3)
    return store, {c.note_uuid: c for c in chunks}


def _seeded_store(tmp_path: Path):
    store, _ = _seeded_store_and_chunks(tmp_path)
    return store


def test_seed_search_prefilters_by_date(tmp_path):
    store = _seeded_store(tmp_path)
    ids = {h["chunk_id"] for h in store.search([1.0, 0, 0, 0], top_k=10, date_range=RANGE)}
    assert ids == {"in"}, "out-of-range and undated must both be excluded"


def test_no_filter_is_built_when_the_range_is_inert(tmp_path):
    store = _seeded_store(tmp_path)
    ids = {h["chunk_id"] for h in store.search([1.0, 0, 0, 0], top_k=10, date_range=DateRange())}
    assert ids == {"in", "out", "undated"}


def test_the_filter_never_falls_back_to_modified_at(tmp_path):
    """A competitor ORs event time with assertion time, which voids the filter."""
    store = _vector_store(tmp_path, "q2")
    store.upsert(
        [
            Chunk(
                id="mt",
                note_uuid="D",
                note_path="d.md",
                text="alpha",
                chunk_index=0,
                modified_at=datetime(2026, 4, 1, tzinfo=UTC),
            )
        ],
        [[1.0, 0, 0, 0]],
    )
    assert store.search([1.0, 0, 0, 0], top_k=10, date_range=RANGE) == [], (
        "modified_at inside the range must not satisfy an occurred_at filter"
    )


def test_result_reports_the_undated_count(tmp_path):
    store = _seeded_store(tmp_path)
    assert store.count_undated() == 1


def test_no_filter_object_is_constructed_when_no_range_is_given(tmp_path):
    """Byte-identical behaviour when the feature is unused.

    Compares omitting the argument entirely against passing ``None`` and
    against an explicit-but-inert ``DateRange()`` — all three must return the
    same chunk-id list as the pre-feature query, not merely a non-empty one.
    """
    store = _seeded_store(tmp_path)

    def ids(hits):
        return [h["chunk_id"] for h in hits]

    omitted = store.search([1.0, 0, 0, 0], top_k=10)
    explicit_none = store.search([1.0, 0, 0, 0], top_k=10, date_range=None)
    inert = store.search([1.0, 0, 0, 0], top_k=10, date_range=DateRange())

    assert ids(omitted) == ids(explicit_none) == ids(inert)


# ---------------------------------------------------------------------------
# Expansion must apply the same filter — see tests/test_expand_seam.py for the
# real GraphStore/Note-construction pattern this borrows.
# ---------------------------------------------------------------------------


def _note(note_uuid: str, rel_path: str, *, wikilinks: list[str] | None = None) -> Note:
    return Note(
        path=Path("/vault") / rel_path,
        relative_path=rel_path,
        title=rel_path.removesuffix(".md"),
        body="body",
        wikilink_uuids=wikilinks or [],
        tags=[],
        mtime=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        word_count=1,
        uuid=note_uuid,
    )


def _seed_from(chunks_by_uuid: dict[str, Chunk], note_uuid: str) -> RetrievedChunk:
    chunk = chunks_by_uuid[note_uuid]
    return RetrievedChunk(
        chunk=chunk,
        vector_score=0.8,
        graph_distance=0,
        seed_chunk_id=chunk.id,
        combined_score=0.8,
    )


def test_expansion_is_filtered_too(tmp_path):
    """Only 1 of 4 arms is filtered in a competing system; that degrades a
    filter to a soft ranking nudge that still lets stale notes through."""
    from my_daemon.retrieval.expand import expand_from_seeds
    from my_daemon.stores import GraphStore

    store, chunks_by_uuid = _seeded_store_and_chunks(tmp_path)

    graph = GraphStore(path=tmp_path / "g.gpickle")
    graph.add_note(_note("A", "in.md", wikilinks=["B", "C"]), chunk_ids=[chunks_by_uuid["A"].id])
    graph.add_note(_note("B", "out.md"), chunk_ids=[chunks_by_uuid["B"].id])
    graph.add_note(_note("C", "undated.md"), chunk_ids=[chunks_by_uuid["C"].id])

    seeds = [_seed_from(chunks_by_uuid, "A")]
    expanded = expand_from_seeds(seeds, graph, store, depth=2, decay=0.5, date_range=RANGE)

    assert [rc.chunk.id for rc in expanded] == [], (
        "B is out of range and C is undated — neither may arrive via the graph"
    )


def test_an_in_range_note_still_arrives_via_expansion(tmp_path):
    """The mirror of the exclusion test. If expansion is filtered too
    aggressively -- say by an accidental IsEmpty on occurred_at -- the
    exclusion test would still pass while the graph stopped working
    entirely."""
    from my_daemon.retrieval.expand import expand_from_seeds
    from my_daemon.stores import GraphStore

    store = _vector_store(tmp_path)
    chunk_a = Chunk(
        id="a1",
        note_uuid="A",
        note_path="a.md",
        text="alpha",
        chunk_index=0,
        occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
        occurred_at_source="frontmatter",
    )
    chunk_b = Chunk(
        id="b1",
        note_uuid="B",
        note_path="b.md",
        text="beta",
        chunk_index=0,
        occurred_at=datetime(2026, 5, 1, tzinfo=UTC),
        occurred_at_source="frontmatter",
    )
    store.upsert([chunk_a, chunk_b], [[1.0, 0, 0, 0], [1.0, 0, 0, 0]])

    graph = GraphStore(path=tmp_path / "g2.gpickle")
    graph.add_note(_note("A", "a.md", wikilinks=["B"]), chunk_ids=[chunk_a.id])
    graph.add_note(_note("B", "b.md"), chunk_ids=[chunk_b.id])

    seeds = [_seed_from({"A": chunk_a}, "A")]
    expanded = expand_from_seeds(seeds, graph, store, depth=2, decay=0.5, date_range=RANGE)

    assert [rc.chunk.id for rc in expanded] == ["b1"], "B is in-range and must still arrive"
