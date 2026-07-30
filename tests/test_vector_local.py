# SPDX-License-Identifier: Apache-2.0
"""Embedded (local-mode) Qdrant: config semantics + a real end-to-end store.

These are *integration* tests in the honest sense — they build a real
``VectorStore`` backed by qdrant-client's in-process local engine on a real
``tmp_path``, push real dense + sparse vectors through the store's own
``upsert``, and read them back through the store's own search methods. No
fakes, no mocks: if local mode ever stops emulating the Query API (prefetch +
RRF fusion, sparse vectors), these fail.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from my_daemon.config import QdrantConfig
from my_daemon.models import Chunk
from my_daemon.stores.vector import LocalStoreLockedError, VectorStore

# --------------------------------------------------------------------------
# Config model
# --------------------------------------------------------------------------


def test_default_config_is_still_a_server_url() -> None:
    """Backward compatibility: a config that says nothing behaves as before."""
    cfg = QdrantConfig()
    assert cfg.url == "http://localhost:6333"
    assert cfg.path is None
    assert cfg.is_embedded is False


def test_explicit_url_is_server_mode() -> None:
    cfg = QdrantConfig(url="http://qdrant.internal:6333")
    assert cfg.is_embedded is False
    assert cfg.url == "http://qdrant.internal:6333"


def test_path_alone_selects_embedded_mode(tmp_path) -> None:
    cfg = QdrantConfig(path=tmp_path / "qdrant")
    assert cfg.is_embedded is True
    assert cfg.path == tmp_path / "qdrant"


def test_url_and_path_together_is_a_validation_error(tmp_path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        QdrantConfig(url="http://localhost:6333", path=tmp_path)
    message = str(excinfo.value)
    assert "url" in message and "path" in message
    # The message has to tell the user what to *do*, not just that they're wrong.
    assert "embedded" in message.lower()


def test_memory_path_is_accepted() -> None:
    cfg = QdrantConfig(path=":memory:")
    assert cfg.is_embedded is True
    assert cfg.location == ":memory:"


def test_location_reports_the_active_target(tmp_path) -> None:
    assert QdrantConfig().location == "http://localhost:6333"
    assert QdrantConfig(path=tmp_path / "q").location == str(tmp_path / "q")


# --------------------------------------------------------------------------
# Store construction
# --------------------------------------------------------------------------


def test_store_rejects_both_url_and_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        VectorStore(url="http://localhost:6333", collection="chunks", dim=4, path=tmp_path)


def test_store_rejects_neither_url_nor_path() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        VectorStore(url=None, collection="chunks", dim=4)


def test_from_config_builds_an_embedded_store(tmp_path) -> None:
    cfg = QdrantConfig(path=tmp_path / "qdrant", collection="chunks")
    store = VectorStore.from_config(cfg, dim=4, hybrid=True)
    try:
        assert store.is_embedded is True
        store.ensure_collection()
        assert store.count() == 0
    finally:
        store.close()


def test_from_config_builds_a_server_store() -> None:
    """No connection is made until first use, so this stays offline-safe."""
    store = VectorStore.from_config(QdrantConfig(), dim=4, hybrid=True)
    assert store.is_embedded is False
    assert store.url == "http://localhost:6333"


# --------------------------------------------------------------------------
# Real vectors through the real store
# --------------------------------------------------------------------------


def _chunk(i: int, note: str = "note-a") -> Chunk:
    return Chunk(
        id=f"chunk-{i}",
        note_uuid=f"uuid-{note}",
        note_path=f"{note}.md",
        heading_path=["Heading"],
        text=f"body text number {i}",
        chunk_index=i,
        tags=["t"],
        wikilinks=[],
    )


# Four points in a 4-d space. c0/c1 sit near the dense query; c3 is far from it
# but is the *only* point carrying sparse term 42.
_DENSE = {
    0: [1.0, 0.0, 0.0, 0.0],
    1: [0.9, 0.1, 0.0, 0.0],
    # Deliberately no ties: two vectors orthogonal to the query would rank in
    # arbitrary order and the dense assertions below would be meaningless.
    2: [0.3, 0.95, 0.0, 0.0],
    3: [0.0, 0.0, 1.0, 0.0],
}
_SPARSE = {
    0: ([1], [0.4]),
    1: ([2], [0.4]),
    2: ([3], [0.4]),
    3: ([42], [1.0]),
}

_DENSE_QUERY = [1.0, 0.0, 0.0, 0.0]
_SPARSE_QUERY = ([42], [1.0])


def _populate(store: VectorStore, hybrid: bool = True) -> None:
    store.ensure_collection()
    chunks = [_chunk(i) for i in range(4)]
    dense = [_DENSE[i] for i in range(4)]
    sparse = [_SPARSE[i] for i in range(4)] if hybrid else None
    store.upsert(chunks, dense, sparse_vectors=sparse)


def test_local_hybrid_rrf_end_to_end(tmp_path) -> None:
    """The whole point: server-side prefetch + RRF fusion, in-process.

    The proof that fusion actually happened is ``chunk-3``: it is the worst
    possible dense match (orthogonal to the query) and would never survive a
    dense-only search, but it owns the queried sparse term.
    """
    store = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=tmp_path / "q")
    try:
        _populate(store)
        assert store.count() == 4

        results = store.hybrid_search(_DENSE_QUERY, _SPARSE_QUERY, top_k=4)

        assert len(results) == 4
        ids = [r["chunk_id"] for r in results]
        assert set(ids) == {"chunk-0", "chunk-1", "chunk-2", "chunk-3"}
        # The dense winner and the sparse-only winner both land at the top.
        assert set(ids[:2]) == {"chunk-0", "chunk-3"}
        # Real RRF scores, not placeholders, and monotonically non-increasing.
        scores = [r["score"] for r in results]
        assert all(isinstance(s, float) and s > 0 for s in scores)
        assert scores == sorted(scores, reverse=True)
        # Payload round-trips intact.
        assert results[0]["note_path"] == "note-a.md"
        assert results[0]["note_uuid"] == "uuid-note-a"
        assert results[0]["text"].startswith("body text number ")

        # Dense-only over the same collection must *not* surface chunk-3 first,
        # which is what makes the fusion assertion above meaningful.
        dense_only = store.search(_DENSE_QUERY, top_k=2)
        assert [r["chunk_id"] for r in dense_only] == ["chunk-0", "chunk-1"]
    finally:
        store.close()


def test_local_hybrid_persists_across_processes(tmp_path) -> None:
    """tmp_path, not ``:memory:`` — an embedded install has to survive a restart."""
    path = tmp_path / "q"
    first = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=path)
    try:
        _populate(first)
    finally:
        first.close()

    second = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=path)
    try:
        # ensure_collection must recognise the on-disk hybrid schema and not
        # drop it as "legacy" — dropping would silently wipe the user's index.
        second.ensure_collection()
        assert second.count() == 4
        results = second.hybrid_search(_DENSE_QUERY, _SPARSE_QUERY, top_k=4)
        assert set(ids := [r["chunk_id"] for r in results]) == {
            "chunk-0",
            "chunk-1",
            "chunk-2",
            "chunk-3",
        }
        assert set(ids[:2]) == {"chunk-0", "chunk-3"}
    finally:
        second.close()


def test_local_dense_only_mode(tmp_path) -> None:
    store = VectorStore(url=None, collection="chunks", dim=4, hybrid=False, path=tmp_path / "q")
    try:
        _populate(store, hybrid=False)
        assert store.count() == 4
        results = store.search(_DENSE_QUERY, top_k=3)
        assert [r["chunk_id"] for r in results] == ["chunk-0", "chunk-1", "chunk-2"]
        assert results[0]["score"] == pytest.approx(1.0, abs=1e-5)
        assert results[0]["score"] > results[1]["score"] > results[2]["score"]
        assert results[0]["note_path"] == "note-a.md"
    finally:
        store.close()


def test_local_payload_mutation_and_delete(tmp_path) -> None:
    """The rest of the store API (rename + delete) has to work embedded too."""
    store = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=tmp_path / "q")
    try:
        _populate(store)
        store.set_note_path("uuid-note-a", "renamed/note-a.md")
        results = store.hybrid_search(_DENSE_QUERY, _SPARSE_QUERY, top_k=4)
        assert {r["note_path"] for r in results} == {"renamed/note-a.md"}

        store.delete_by_note_uuid("uuid-note-a")
        assert store.count() == 0
    finally:
        store.close()


def test_local_upsert_without_sparse_still_rejected(tmp_path) -> None:
    store = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=tmp_path / "q")
    try:
        store.ensure_collection()
        with pytest.raises(ValueError, match="sparse_vectors"):
            store.upsert([_chunk(0)], [_DENSE[0]])
    finally:
        store.close()


def test_memory_mode_works(tmp_path) -> None:
    store = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=":memory:")
    try:
        _populate(store)
        assert store.count() == 4
    finally:
        store.close()


# --------------------------------------------------------------------------
# The single-process constraint
# --------------------------------------------------------------------------


def test_second_client_on_the_same_path_gets_an_actionable_error(tmp_path) -> None:
    path = tmp_path / "q"
    first = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=path)
    try:
        first.ensure_collection()

        second = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=path)
        with pytest.raises(LocalStoreLockedError) as excinfo:
            second.ensure_collection()
        message = str(excinfo.value)
        assert str(path) in message
        # Actionable: names the cause and the escape hatch.
        assert "another" in message.lower()
        assert "url" in message.lower()
    finally:
        first.close()


def test_lock_is_released_by_close(tmp_path) -> None:
    path = tmp_path / "q"
    first = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=path)
    first.ensure_collection()
    first.close()

    second = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=path)
    try:
        second.ensure_collection()  # no LocalStoreLockedError
        assert second.count() == 0
    finally:
        second.close()


# --------------------------------------------------------------------------
# Dates on the payload: `occurred_at` / `occurred_at_source` / `modified_at`
# --------------------------------------------------------------------------


def _all_payloads(store: VectorStore) -> list[dict]:
    """Read every point's payload back via the real client's scroll.

    No production ``all_payloads()`` method exists on ``VectorStore`` — this
    is test-only plumbing over the same ``scroll`` call `expand.py` uses.
    """
    points, _ = store._client_().scroll(
        collection_name=store.collection, with_payload=True, limit=1000
    )
    return [p.payload or {} for p in points]


def test_payload_omits_date_keys_when_undated(tmp_path) -> None:
    """An absent key gives cleaner range semantics than an explicit null."""
    from datetime import UTC, datetime

    store = VectorStore(url=None, collection="chunks", dim=4, hybrid=True, path=tmp_path / "q")
    try:
        store.ensure_collection()
        dated = Chunk(
            id="c1",
            note_uuid="u1",
            note_path="a.md",
            text="x",
            chunk_index=0,
            occurred_at=datetime(2024, 9, 2, tzinfo=UTC),
            occurred_at_source="frontmatter",
        )
        undated = Chunk(id="c2", note_uuid="u2", note_path="b.md", text="y", chunk_index=0)
        store.upsert(
            [dated, undated],
            [[0.1] * store.dim, [0.2] * store.dim],
            sparse_vectors=[([1], [0.4]), ([2], [0.4])],
        )

        payloads = {p["chunk_id"]: p for p in _all_payloads(store)}
        assert payloads["c1"]["occurred_at"] == "2024-09-02T00:00:00+00:00"
        assert payloads["c1"]["occurred_at_source"] == "frontmatter"
        assert "occurred_at" not in payloads["c2"]
        assert "occurred_at_source" not in payloads["c2"]
        assert "modified_at" not in payloads["c2"]
    finally:
        store.close()


def test_payload_round_trip_keeps_occurred_at_and_modified_at_distinct(tmp_path) -> None:
    from datetime import UTC, datetime

    from my_daemon.stores.vector import chunk_from_payload

    payload = {
        "chunk_id": "c1",
        "note_uuid": "u1",
        "note_path": "a.md",
        "text": "x",
        "chunk_index": 0,
        "occurred_at": "2024-09-02T00:00:00+00:00",
        "occurred_at_source": "frontmatter",
        "modified_at": "2026-07-01T12:00:00+00:00",
    }
    chunk = chunk_from_payload(payload)
    assert chunk.occurred_at == datetime(2024, 9, 2, tzinfo=UTC)
    assert chunk.modified_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert chunk.occurred_at_source == "frontmatter"


def test_chunk_from_payload_tolerates_a_legacy_payload_without_dates() -> None:
    """Every point already in a user's collection predates these keys."""
    from my_daemon.stores.vector import chunk_from_payload

    chunk = chunk_from_payload(
        {"chunk_id": "c", "note_uuid": "u", "note_path": "a.md", "text": "x", "chunk_index": 0}
    )
    assert chunk.occurred_at is None and chunk.modified_at is None
    assert chunk.occurred_at_source is None
