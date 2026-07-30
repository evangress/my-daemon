# SPDX-License-Identifier: Apache-2.0
"""One-time backfill of `occurred_at` onto chunks ingested before it existed.

These are integration tests over a real embedded ``VectorStore`` (no fakes for
the vector layer — the whole point of this module is a real payload write
that must never touch a vector), a real ``NoteRegistry``, and a real
``VaultReader``. Only the embedder is fake, exactly as elsewhere in the suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.pipeline.ingest import ingest_vault
from my_daemon.stores.graph import GraphStore
from my_daemon.stores.vector import VectorStore


class FakeEmbedder:
    """Deterministic stand-in — ingest only needs vectors of a consistent width."""

    dimension = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def settings_for(tmp_path: Path):
    """Factory: a real ``Settings`` over ``vault``, with daemon state tucked
    into ``tmp_path/data`` (never picked up by the ``*.md`` vault scan) and a
    real embedded (non-hybrid) Qdrant store so backfill's own vector-store
    construction stays consistent with what the tests read back."""

    def _make(*, vault: Path) -> Settings:
        s = Settings()
        s.vault.path = vault
        s.graph.path = tmp_path / "data" / "graph.gpickle"
        s.graph.manifest_path = tmp_path / "data" / "manifest.json"
        s.feedback.db_path = tmp_path / "data" / "state.db"
        s.vector_store.qdrant.path = tmp_path / "data" / "qdrant"
        # Hybrid mode needs a sparse embedder the fake doesn't provide;
        # dense-only keeps `_ingest` a one-liner without weakening what this
        # module actually needs to prove (payload writes, not fusion).
        s.embeddings.hybrid = False
        return s

    return _make


def _ingest(settings: Settings) -> None:
    vector_store = VectorStore.from_config(
        settings.vector_store.qdrant, dim=FakeEmbedder.dimension, hybrid=settings.embeddings.hybrid
    )
    try:
        ingest_vault(settings, FakeEmbedder(), vector_store, GraphStore(path=settings.graph.path))
    finally:
        vector_store.close()


def _all_payloads(settings: Settings) -> list[dict]:
    """Every point's payload, or ``[]`` if the collection was never created —
    which is the honest state of a vault that has never been ingested."""

    store = VectorStore.from_config(
        settings.vector_store.qdrant, dim=FakeEmbedder.dimension, hybrid=settings.embeddings.hybrid
    )
    try:
        points, _ = store._client_().scroll(
            collection_name=store.collection, with_payload=True, with_vectors=True, limit=1000
        )
        return [{"vector": p.vector, **(p.payload or {})} for p in points]
    except ValueError:
        return []
    finally:
        store.close()


def _first_vector(settings: Settings) -> list[float]:
    payloads = _all_payloads(settings)
    assert payloads, "expected at least one ingested point"
    return list(payloads[0]["vector"])


def _stored_occurred_at(settings: Settings, rel_path: str) -> datetime | None:
    for payload in _all_payloads(settings):
        if payload.get("note_path") == rel_path:
            raw = payload.get("occurred_at")
            return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    return None


def _payload_of(settings: Settings, rel_path: str) -> dict:
    """The raw payload dict for a note's first chunk.

    Deliberately distinct from ``_stored_occurred_at``: ``.get("occurred_at")``
    collapses "key absent" and "key present with value None" to the same
    result, and the date filter's ``IsEmpty`` semantics depend on telling
    those two apart. A caller that needs that distinction must use ``in``
    against this dict, never ``.get()``.
    """
    for payload in _all_payloads(settings):
        if payload.get("note_path") == rel_path:
            return payload
    raise AssertionError(f"no point with note_path={rel_path!r}")


def _strip_occurred_at(settings: Settings, rel_path: str) -> None:
    """Simulate a point ingested before the ``occurred_at`` payload key existed.

    Clears both the vector payload key (via the client's own
    ``delete_payload``, mirroring what a legacy point looks like) and the
    registry row, so a dry-run's refusal to restore it is a real assertion
    rather than one that would hold with or without ``dry_run``.
    """

    from qdrant_client.http.models import FieldCondition, Filter, MatchValue

    from my_daemon.stores.registry import NoteRegistry

    registry = NoteRegistry(db_path=settings.feedback.db_path)
    record = registry.by_path(rel_path)
    assert record is not None
    record.occurred_at = None
    record.occurred_at_source = None
    registry.upsert(record)

    store = VectorStore.from_config(
        settings.vector_store.qdrant, dim=FakeEmbedder.dimension, hybrid=settings.embeddings.hybrid
    )
    try:
        store._client_().delete_payload(
            collection_name=store.collection,
            keys=["occurred_at", "occurred_at_source"],
            points=Filter(
                must=[FieldCondition(key="note_uuid", match=MatchValue(value=record.uuid))]
            ),
            wait=True,
        )
    finally:
        store.close()


def test_backfill_reports_the_derivation_breakdown(tmp_path, settings_for):
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    (tmp_path / "dated.md").write_text('---\ndate: "2024-09-02"\n---\nbody\n', encoding="utf-8")
    (tmp_path / "Welcome.md").write_text("# W\nbody\n", encoding="utf-8")

    from my_daemon.pipeline.backfill_dates import backfill_dates

    report = backfill_dates(settings_for(vault=tmp_path))
    assert report.frontmatter == 1
    assert report.filename == 1
    assert report.undated == 1


def test_backfill_is_idempotent(tmp_path, settings_for):
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    from my_daemon.pipeline.backfill_dates import backfill_dates

    s = settings_for(vault=tmp_path)
    _ingest(s)
    first = backfill_dates(s)
    payload_after_first = _payload_of(s, "2026-04-26.md")

    second = backfill_dates(s)

    assert (first.frontmatter, first.filename, first.undated, first.updated) == (
        second.frontmatter,
        second.filename,
        second.undated,
        second.updated,
    )
    # The counts alone derive from vault content, not storage, so they'd be
    # idempotent by construction even if the second run duplicated a registry
    # write or errored on a redundant `delete_payload`. The raw payload has to
    # actually be unchanged too.
    assert _payload_of(s, "2026-04-26.md") == payload_after_first


def test_backfill_does_not_touch_vectors(tmp_path, settings_for):
    """Chunk ids don't hash the timestamp, so this is a payload write, not a re-embed."""
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    from my_daemon.pipeline.backfill_dates import backfill_dates

    s = settings_for(vault=tmp_path)
    _ingest(s)
    before = _first_vector(s)

    backfill_dates(s)

    assert _first_vector(s) == before


def test_dry_run_writes_nothing(tmp_path, settings_for):
    from my_daemon.pipeline.backfill_dates import backfill_dates

    s = settings_for(vault=tmp_path)
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    _ingest(s)
    _strip_occurred_at(s, "2026-04-26.md")  # simulate a point that predates the payload key
    assert _stored_occurred_at(s, "2026-04-26.md") is None

    report = backfill_dates(s, dry_run=True)

    assert report.filename == 1
    # The note is already registered (a prior real `_ingest` ran above), so a
    # real run would write it — the dry-run count must say so too, not just
    # report the derivation breakdown.
    assert report.updated == 1
    assert _stored_occurred_at(s, "2026-04-26.md") is None


def test_frontmatter_edit_refreshes_the_date_without_re_embedding(tmp_path, settings_for):
    """The existing fm_changed branch must now carry occurred_at (finding 5b)."""
    s = settings_for(vault=tmp_path)
    p = tmp_path / "note.md"
    p.write_text('---\ndate: "2024-09-02"\n---\nbody\n', encoding="utf-8")
    _ingest(s)

    p.write_text('---\ndate: "2025-01-15"\n---\nbody\n', encoding="utf-8")
    _ingest(s)

    assert _stored_occurred_at(s, "note.md") == datetime(2025, 1, 15, tzinfo=UTC)


def test_a_renamed_note_refreshes_a_filename_derived_date(tmp_path, settings_for):
    """A filename-derived date changes when the file is renamed, and the body
    hash does not -- so only the `renamed` branch can catch it. Without this,
    renaming 2024-09-02.md to 2026-01-01.md leaves the old date in the payload.
    """
    s = settings_for(vault=tmp_path)
    old = tmp_path / "2024-09-02.md"
    old.write_text("# A\nbody\n", encoding="utf-8")
    _ingest(s)
    assert _stored_occurred_at(s, "2024-09-02.md") == datetime(2024, 9, 2, tzinfo=UTC)

    new = tmp_path / "2026-01-01.md"
    old.rename(new)
    _ingest(s)

    assert _stored_occurred_at(s, "2026-01-01.md") == datetime(2026, 1, 1, tzinfo=UTC)


def test_losing_a_frontmatter_date_removes_the_key_rather_than_nulling_it(tmp_path, settings_for):
    """dated -> undated must REMOVE the payload key, not write it as null.

    The date filter treats an absent key as "undated" via `IsEmptyCondition`;
    a literal null would not match that condition, so the note would silently
    become invisible to both filtered and unfiltered date queries rather than
    correctly falling out of them. Only the `fm_changed` branch can catch
    this: the body is unchanged, so a body-hash comparison alone would skip
    the note entirely and leave the stale date in place.
    """
    s = settings_for(vault=tmp_path)
    p = tmp_path / "note.md"
    p.write_text('---\ndate: "2024-09-02"\n---\nbody\n', encoding="utf-8")
    _ingest(s)
    assert "occurred_at" in _payload_of(s, "note.md")

    p.write_text("body\n", encoding="utf-8")  # frontmatter dropped, body unchanged
    _ingest(s)

    payload = _payload_of(s, "note.md")
    assert "occurred_at" not in payload
    assert "occurred_at_source" not in payload
