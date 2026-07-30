# SPDX-License-Identifier: Apache-2.0
"""The identity cutover: UUID as the shared key across all three stores.

Identity and display separate deliberately. `note_uuid` joins the stores;
`note_path` stays on every chunk because prompts, citations, GUI source lists,
and observer letters all render it as prose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.models import Note
from my_daemon.pipeline.ingest import ingest_vault
from my_daemon.stores.graph import GraphStore
from my_daemon.stores.registry import NoteRegistry
from my_daemon.vault.chunker import chunk_note
from my_daemon.vault.identity import derive_path_uuid, effective_uuid

UUID_A = "9f3c1a7e-4b21-4d6f-9c88-1e2a5b0d7f43"
UUID_B = "11111111-2222-4333-8444-555555555555"


def _note(rel_path="A.md", *, uuid=UUID_A, body="# A\n\nsome text", **kw) -> Note:
    return Note(
        path=Path("/vault") / rel_path,
        relative_path=rel_path,
        title=kw.pop("title", "A"),
        body=body,
        mtime=datetime(2026, 7, 26, tzinfo=UTC),
        word_count=len(body.split()),
        uuid=uuid,
        **kw,
    )


# ---------------------------------------------------------------------------
# effective_uuid — a note always has an identity, stamped or derived
# ---------------------------------------------------------------------------


def test_effective_uuid_prefers_the_frontmatter_value():
    assert effective_uuid(_note()) == UUID_A


def test_effective_uuid_falls_back_to_a_path_derived_one():
    assert effective_uuid(_note(uuid=None)) == derive_path_uuid("A.md")


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def test_chunks_carry_both_the_uuid_and_the_display_path():
    chunk = chunk_note(_note())[0]

    assert chunk.note_uuid == UUID_A
    assert chunk.note_path == "A.md"


def test_chunk_ids_are_derived_from_the_uuid_not_the_path():
    """Renaming a note must not change its chunk ids."""
    before = [c.id for c in chunk_note(_note(rel_path="A.md"))]
    after = [c.id for c in chunk_note(_note(rel_path="Moved/A.md"))]

    assert before == after


def test_chunk_ids_differ_between_notes():
    a = [c.id for c in chunk_note(_note(uuid=UUID_A))]
    b = [c.id for c in chunk_note(_note(uuid=UUID_B))]

    assert set(a).isdisjoint(b)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def test_graph_nodes_are_keyed_by_uuid(tmp_path: Path):
    store = GraphStore(path=tmp_path / "g.gpickle")

    store.add_note(_note(), chunk_ids=["c1"])

    assert f"note::{UUID_A}" in store.graph
    assert "note::A.md" not in store.graph


def test_the_graph_remembers_the_display_path_on_the_node(tmp_path: Path):
    """Reports render paths; the graph must be able to supply one."""
    store = GraphStore(path=tmp_path / "g.gpickle")

    store.add_note(_note(), chunk_ids=["c1"])

    assert store.graph.nodes[f"note::{UUID_A}"]["rel_path"] == "A.md"


def test_neighbors_are_returned_as_uuids(tmp_path: Path):
    store = GraphStore(path=tmp_path / "g.gpickle")
    store.add_note(_note("A.md", uuid=UUID_A, wikilink_uuids=[UUID_B]), chunk_ids=["a"])
    store.add_note(_note("B.md", uuid=UUID_B, title="B"), chunk_ids=["b"])

    neighbors = store.neighbors_within(UUID_A, depth=1)

    assert UUID_B in neighbors


def test_renaming_a_note_preserves_its_graph_identity(tmp_path: Path):
    """The whole point: the Librarian can move files without amnesia."""
    store = GraphStore(path=tmp_path / "g.gpickle")
    store.add_note(_note("A.md", wikilink_uuids=[UUID_B]), chunk_ids=["a"])
    store.add_note(_note("B.md", uuid=UUID_B, title="B"), chunk_ids=["b"])

    store.update_note(_note("Archive/A.md", wikilink_uuids=[UUID_B]), chunk_ids=["a"])

    assert store.graph.has_edge(f"note::{UUID_A}", f"note::{UUID_B}")
    assert store.graph.nodes[f"note::{UUID_A}"]["rel_path"] == "Archive/A.md"


# ---------------------------------------------------------------------------
# End-to-end through ingest
# ---------------------------------------------------------------------------


class FakeEmbedder:
    dimension = 4

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encoded.extend(texts)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeVectorStore:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.deleted: list[str] = []
        self.renamed: list[tuple[str, str]] = []

    def ensure_collection(self) -> None:
        pass

    def delete_by_note_uuid(self, note_uuid: str) -> None:
        self.deleted.append(note_uuid)

    def set_note_path(self, note_uuid: str, rel_path: str) -> None:
        self.renamed.append((note_uuid, rel_path))

    def set_occurred_at(self, note_uuid, occurred_at, source, modified_at) -> None:  # noqa: ANN001
        pass

    def upsert(self, chunks, vectors, sparse_vectors=None) -> None:  # noqa: ANN001
        self.payloads.extend({"note_uuid": c.note_uuid, "note_path": c.note_path} for c in chunks)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text(f"---\nuuid: {UUID_A}\n---\n\n# A\n\nSee [[B]].\n", encoding="utf-8")
    (root / "B.md").write_text(f"---\nuuid: {UUID_B}\n---\n\n# B\n\nDaemons.\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(vault: Path, tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.graph.path = tmp_path / "data" / "graph.gpickle"
    s.graph.manifest_path = tmp_path / "data" / "manifest.json"
    s.feedback.db_path = tmp_path / "data" / "state.db"
    return s


def test_ingest_writes_the_uuid_into_the_vector_payload(settings: Settings):
    store = GraphStore(path=settings.graph.path)
    vectors = FakeVectorStore()

    ingest_vault(settings, FakeEmbedder(), vectors, store)

    assert {p["note_uuid"] for p in vectors.payloads} == {UUID_A, UUID_B}
    assert {p["note_path"] for p in vectors.payloads} == {"A.md", "B.md"}


def test_ingest_populates_the_registry(settings: Settings):
    store = GraphStore(path=settings.graph.path)

    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    registry = NoteRegistry(db_path=settings.feedback.db_path)
    assert registry.get(UUID_A).rel_path == "A.md"
    assert registry.coverage().total == 2


def test_renaming_a_note_costs_no_embedding(settings: Settings, vault: Path):
    """The headline win of uuid keying: a move is a payload update."""
    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    (vault / "Archive").mkdir()
    (vault / "A.md").rename(vault / "Archive" / "A.md")
    embedder = FakeEmbedder()
    vectors = FakeVectorStore()
    ingest_vault(settings, embedder, vectors, store)

    assert embedder.encoded == []  # nothing re-embedded
    assert (UUID_A, "Archive/A.md") in vectors.renamed
    assert NoteRegistry(db_path=settings.feedback.db_path).get(UUID_A).rel_path == ("Archive/A.md")


def test_renaming_a_note_preserves_its_learned_weights(settings: Settings, vault: Path):
    from my_daemon.retrieval.weights import apply_selection

    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)
    apply_selection(store, seed_note_uuid=UUID_A, selected_note_uuid=UUID_B)
    store.save()  # ingest reloads the graph from disk
    weight = max(
        float(d.get("weight", 1.0))
        for d in store.graph[f"note::{UUID_A}"][f"note::{UUID_B}"].values()
    )
    assert weight > 1.0

    (vault / "Archive").mkdir()
    (vault / "A.md").rename(vault / "Archive" / "A.md")
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    after = max(
        float(d.get("weight", 1.0))
        for d in store.graph[f"note::{UUID_A}"][f"note::{UUID_B}"].values()
    )
    assert after == weight


def test_an_unstamped_note_still_ingests_on_a_derived_identity(settings: Settings, vault: Path):
    (vault / "C.md").write_text("# C\n\nNo uuid here.\n", encoding="utf-8")
    store = GraphStore(path=settings.graph.path)

    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    record = NoteRegistry(db_path=settings.feedback.db_path).by_path("C.md")
    assert record.uuid == derive_path_uuid("C.md")
    assert record.in_frontmatter is False


def test_deleting_a_note_soft_deletes_its_registry_row(settings: Settings, vault: Path):
    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    (vault / "B.md").unlink()
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    registry = NoteRegistry(db_path=settings.feedback.db_path)
    assert registry.get(UUID_B).deleted_at is not None
    assert registry.by_path("B.md") is None


# ---------------------------------------------------------------------------
# Frontmatter-only edits must not be invisible
# ---------------------------------------------------------------------------


def test_a_frontmatter_only_edit_reaches_the_graph(settings: Settings, vault: Path):
    """Tags live in frontmatter. Skipping on body hash alone hid every one."""
    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)
    assert not store.graph.has_edge(f"note::{UUID_A}", "tag::solitude")

    (vault / "A.md").write_text(
        f"---\nuuid: {UUID_A}\ntags: [solitude]\n---\n\n# A\n\nSee [[B]].\n",
        encoding="utf-8",
    )
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    assert store.graph.has_edge(f"note::{UUID_A}", "tag::solitude")


def test_a_frontmatter_only_edit_refreshes_the_registry(settings: Settings, vault: Path):
    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    (vault / "A.md").write_text(
        f"---\nuuid: {UUID_A}\ntags: [solitude]\n---\n\n# A\n\nSee [[B]].\n",
        encoding="utf-8",
    )
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    assert "solitude" in NoteRegistry(db_path=settings.feedback.db_path).get(UUID_A).tags


def test_a_frontmatter_only_edit_costs_no_embedding(settings: Settings, vault: Path):
    """The body is unchanged, so there is nothing new to embed."""
    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    (vault / "A.md").write_text(
        f"---\nuuid: {UUID_A}\ntags: [solitude]\n---\n\n# A\n\nSee [[B]].\n",
        encoding="utf-8",
    )
    embedder = FakeEmbedder()
    stats = ingest_vault(settings, embedder, FakeVectorStore(), store)

    assert embedder.encoded == []
    assert stats.notes_metadata_refreshed == 1
    assert stats.notes_new_or_updated == 0


def test_a_frontmatter_only_edit_preserves_learned_weights(settings: Settings, vault: Path):
    from my_daemon.retrieval.weights import apply_selection

    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)
    apply_selection(store, seed_note_uuid=UUID_A, selected_note_uuid=UUID_B)
    store.save()
    weight = max(
        float(d.get("weight", 1.0))
        for d in store.graph[f"note::{UUID_A}"][f"note::{UUID_B}"].values()
    )

    (vault / "A.md").write_text(
        f"---\nuuid: {UUID_A}\ntags: [solitude]\n---\n\n# A\n\nSee [[B]].\n",
        encoding="utf-8",
    )
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    after = max(
        float(d.get("weight", 1.0))
        for d in store.graph[f"note::{UUID_A}"][f"note::{UUID_B}"].values()
    )
    assert after == weight


def test_a_truly_unchanged_note_is_still_skipped(settings: Settings):
    store = GraphStore(path=settings.graph.path)
    ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    stats = ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)

    assert stats.skipped_unchanged == 2
    assert stats.notes_metadata_refreshed == 0
