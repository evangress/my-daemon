# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from my_daemon.stores.graph import GraphStore
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note

# Fixture notes carry no `uuid:` frontmatter, so their identity is the
# deterministic path-derived fallback.
from my_daemon.vault.identity import derive_path_uuid as _u


def _populated_store(vault_root: Path, store_path: Path) -> GraphStore:
    store = GraphStore(path=store_path)
    reader = VaultReader(vault_root)
    for note in reader.read_all():
        chunks = chunk_note(note)
        store.add_note(note, chunk_ids=[c.id for c in chunks])
    return store


def test_graph_builds_from_fixture_vault(tmp_path: Path, vault_root: Path):
    store = _populated_store(vault_root, tmp_path / "graph.gpickle")
    stats = store.stats()
    assert stats.note_count >= 5
    assert stats.tag_count >= 3
    assert stats.edge_count > 0


def test_neighbors_within_walks_wikilinks(tmp_path: Path, vault_root: Path):
    store = _populated_store(vault_root, tmp_path / "graph.gpickle")
    # Designing AI Memory wikilinks Pullman Daemons, Socratic Daemon, Obsidian Vaults
    neighbors = store.neighbors_within(_u("Designing AI Memory.md"), depth=1)
    assert _u("Pullman Daemons.md") in neighbors
    assert _u("Socratic Daemon.md") in neighbors
    assert _u("Obsidian Vaults.md") in neighbors


def test_neighbors_reaches_via_shared_tag(tmp_path: Path, vault_root: Path):
    store = _populated_store(vault_root, tmp_path / "graph.gpickle")
    # Pullman Daemons and Socratic Daemon both have #metaphor — they should be
    # reachable from each other at depth 2 (note → tag → note).
    neighbors = store.neighbors_within(_u("Pullman Daemons.md"), depth=2)
    assert _u("Socratic Daemon.md") in neighbors


def test_persistence_roundtrip(tmp_path: Path, vault_root: Path):
    path = tmp_path / "graph.gpickle"
    store = _populated_store(vault_root, path)
    store.save()

    reloaded = GraphStore(path=path)
    reloaded.load()
    assert reloaded.stats().note_count == store.stats().note_count
    assert reloaded.stats().edge_count == store.stats().edge_count
