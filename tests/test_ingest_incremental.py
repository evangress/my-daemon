# SPDX-License-Identifier: Apache-2.0
"""Incremental re-ingest must not destroy graph structure or learned weights.

The unit-level contract lives in ``test_graph_update.py``. This file proves the
pipeline actually uses it — the bug was in ``ingest_vault``'s call sequence, not
in ``GraphStore`` itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.pipeline.ingest import ingest_vault
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores.graph import GraphStore
from my_daemon.vault.identity import derive_path_uuid as _u


class FakeEmbedder:
    """Deterministic stand-in — ingest only needs vectors of a consistent width."""

    dimension = 4

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeVectorStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.upserted: list[str] = []

    def ensure_collection(self) -> None:
        pass

    def delete_by_note_uuid(self, note_uuid: str) -> None:
        self.deleted.append(note_uuid)

    def set_note_path(self, note_uuid: str, rel_path: str) -> None:
        self.renamed.append((note_uuid, rel_path))

    def set_occurred_at(self, note_uuid, occurred_at, source, modified_at) -> None:  # noqa: ANN001
        pass

    def upsert(self, chunks, vectors, sparse_vectors=None) -> None:  # noqa: ANN001
        self.upserted.extend(c.id for c in chunks)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("# A\n\nSee [[B]].\n\n#memory\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n\nSomething about daemons.\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(vault: Path, tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.graph.path = tmp_path / "data" / "graph.gpickle"
    s.graph.manifest_path = tmp_path / "data" / "manifest.json"
    s.feedback.db_path = tmp_path / "data" / "state.db"
    return s


def _ingest(settings: Settings, graph_store: GraphStore) -> None:
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), graph_store)


def _edge_weight(store: GraphStore, src: str, dst: str) -> float:
    data = store.graph[f"note::{_u(src)}"][f"note::{_u(dst)}"]
    return max(float(d.get("weight", 1.0)) for d in data.values())


def test_editing_a_note_keeps_inbound_wikilinks_from_other_notes(settings: Settings, vault: Path):
    """Editing B used to delete A's link to B until A was re-ingested too."""
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)
    assert store.graph.has_edge(f"note::{_u('A.md')}", f"note::{_u('B.md')}")

    (vault / "B.md").write_text("# B\n\nRevised thoughts.\n", encoding="utf-8")
    _ingest(settings, store)

    assert store.graph.has_edge(f"note::{_u('A.md')}", f"note::{_u('B.md')}")


def test_editing_a_note_preserves_weights_learned_on_its_own_edges(settings: Settings, vault: Path):
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)
    apply_selection(store, seed_note_uuid=_u("A.md"), selected_note_uuid=_u("B.md"))
    store.save()
    reinforced = _edge_weight(store, "A.md", "B.md")
    assert reinforced > 1.0

    (vault / "A.md").write_text(
        "# A\n\nSee [[B]]. Now with more detail.\n\n#memory\n", encoding="utf-8"
    )
    _ingest(settings, store)

    assert _edge_weight(store, "A.md", "B.md") == reinforced


def test_editing_a_note_preserves_weights_learned_on_inbound_edges(settings: Settings, vault: Path):
    """Editing the *target* of a reinforced edge must not reset it either."""
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)
    apply_selection(store, seed_note_uuid=_u("A.md"), selected_note_uuid=_u("B.md"))
    store.save()
    reinforced = _edge_weight(store, "A.md", "B.md")

    (vault / "B.md").write_text("# B\n\nRevised thoughts.\n", encoding="utf-8")
    _ingest(settings, store)

    assert _edge_weight(store, "A.md", "B.md") == reinforced


def test_editing_a_note_still_replaces_its_vectors(settings: Settings, vault: Path):
    """The differential graph update must not weaken chunk replacement."""
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)

    (vault / "A.md").write_text("# A\n\nWholly different.\n", encoding="utf-8")
    vector_store = FakeVectorStore()
    ingest_vault(settings, FakeEmbedder(), vector_store, store)

    assert _u("A.md") in vector_store.deleted
    assert vector_store.upserted  # and re-added


def test_deleting_a_note_still_removes_it_from_the_graph(settings: Settings, vault: Path):
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)

    (vault / "B.md").unlink()
    _ingest(settings, store)

    assert f"note::{_u('B.md')}" not in store.graph


def test_deleting_a_notes_target_currently_loses_the_inbound_edge(settings: Settings, vault: Path):
    """Documents a *separate* known defect on the deletion path.

    Incremental and full ingest diverge here. A full rebuild would leave
    ``note::B`` — a dangling placeholder keyed by the raw wikilink text, since
    ``[[B]]`` no longer resolves to a file — preserving A's outgoing link.
    Incremental deletion drops the edge outright and never rebuilds it until
    A is re-ingested for some other reason.

    Fixing it means re-resolving the linkers' wikilinks when a target
    disappears, which is out of scope for the differential-update change.
    This test pins the current behavior so the fix is visible when it lands.
    """
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)

    (vault / "B.md").unlink()
    _ingest(settings, store)

    assert list(store.graph.out_edges(f"note::{_u('A.md')}")) == [
        (f"note::{_u('A.md')}", "tag::memory")
    ]
    assert "dangling::B" not in store.graph  # what a full rebuild would have created


def test_ingest_note_keeps_inbound_links_when_recapturing(settings: Settings, vault: Path):
    """The Hermes capture path re-ingests one note repeatedly within a session."""
    from my_daemon.pipeline.ingest import ingest_note
    from my_daemon.vault.parser import parse_note

    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)

    (vault / "B.md").write_text("# B\n\nRecaptured.\n", encoding="utf-8")
    ingest_note(
        settings,
        FakeEmbedder(),
        FakeVectorStore(),
        store,
        parse_note(vault / "B.md", vault),
    )

    assert store.graph.has_edge(f"note::{_u('A.md')}", f"note::{_u('B.md')}")


def test_ingest_note_preserves_learned_weights(settings: Settings, vault: Path):
    from my_daemon.pipeline.ingest import ingest_note
    from my_daemon.vault.parser import parse_note

    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)
    apply_selection(store, seed_note_uuid=_u("A.md"), selected_note_uuid=_u("B.md"))
    reinforced = _edge_weight(store, "A.md", "B.md")
    assert reinforced > 1.0

    (vault / "B.md").write_text("# B\n\nRecaptured.\n", encoding="utf-8")
    ingest_note(
        settings,
        FakeEmbedder(),
        FakeVectorStore(),
        store,
        parse_note(vault / "B.md", vault),
    )

    assert _edge_weight(store, "A.md", "B.md") == reinforced


def test_removing_a_wikilink_from_a_note_drops_the_edge(settings: Settings, vault: Path):
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)

    (vault / "A.md").write_text("# A\n\nNo links now.\n\n#memory\n", encoding="utf-8")
    _ingest(settings, store)

    assert not store.graph.has_edge(f"note::{_u('A.md')}", f"note::{_u('B.md')}")


def test_ingest_persists_occurred_at_into_the_registry(settings: Settings, vault: Path):
    """The columns exist and Note carries the values; only `_register` connects
    them. A test that builds a NoteRecord by hand passes right over this."""
    from datetime import UTC, datetime

    from my_daemon.stores.registry import NoteRegistry

    (vault / "A.md").write_text(
        '---\ndate: "2024-09-02"\n---\n# A\n\nSee [[B]].\n\n#memory\n', encoding="utf-8"
    )
    store = GraphStore(path=settings.graph.path)
    _ingest(settings, store)

    registry = NoteRegistry(db_path=settings.feedback.db_path)
    dated = registry.get(_u("A.md"))
    undated = registry.get(_u("B.md"))

    assert dated.occurred_at == datetime(2024, 9, 2, tzinfo=UTC)
    assert dated.occurred_at_source == "frontmatter"
    assert undated.occurred_at is None
    assert undated.occurred_at_source is None
