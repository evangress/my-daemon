# SPDX-License-Identifier: Apache-2.0
"""Adaptive edge-weight reinforcement and decay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from my_daemon.retrieval.weights import (
    DEFAULT_CEILING,
    apply_selection,
    decay_unused_edges,
)
from my_daemon.stores.graph import GraphStore
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note

# Fixture notes carry no `uuid:` frontmatter, so their identity is the
# deterministic path-derived fallback.
from my_daemon.vault.identity import derive_path_uuid as _u


def _vault_graph(vault_root: Path, store_path: Path) -> GraphStore:
    store = GraphStore(path=store_path)
    for note in VaultReader(vault_root).read_all():
        chunks = chunk_note(note)
        store.add_note(note, chunk_ids=[c.id for c in chunks])
    return store


def test_apply_selection_reinforces_direct_wikilink(tmp_path: Path, vault_root: Path) -> None:
    """Designing AI Memory → Pullman Daemons is a one-hop wikilink path."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")

    result = apply_selection(
        store,
        seed_note_uuid=_u("Designing AI Memory.md"),
        selected_note_uuid=_u("Pullman Daemons.md"),
    )

    assert result.edges_reinforced >= 1
    assert result.total_delta > 0
    assert result.path[0] == f"note::{_u('Designing AI Memory.md')}"
    assert result.path[-1] == f"note::{_u('Pullman Daemons.md')}"

    # The wikilink edge from Designing AI Memory → Pullman Daemons should be > 1.0.
    edge_data = store.graph[f"note::{_u('Designing AI Memory.md')}"][f"note::{_u('Pullman Daemons.md')}"]
    weights = [d["weight"] for d in edge_data.values()]
    assert max(weights) > 1.0
    # Stamp set.
    assert all(d.get("last_reinforced_at") for d in edge_data.values())


def test_apply_selection_self_is_noop(tmp_path: Path, vault_root: Path) -> None:
    """Picking the seed itself (graph_distance==0) reinforces nothing."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")

    result = apply_selection(
        store,
        seed_note_uuid=_u("Designing AI Memory.md"),
        selected_note_uuid=_u("Designing AI Memory.md"),
    )

    assert result.edges_reinforced == 0
    assert result.total_delta == 0.0


def test_apply_selection_respects_ceiling(tmp_path: Path, vault_root: Path) -> None:
    """Repeated reinforcement caps at ``DEFAULT_CEILING``."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")

    for _ in range(50):
        apply_selection(
            store,
            seed_note_uuid=_u("Designing AI Memory.md"),
            selected_note_uuid=_u("Pullman Daemons.md"),
        )

    edge_data = store.graph[f"note::{_u('Designing AI Memory.md')}"][f"note::{_u('Pullman Daemons.md')}"]
    for d in edge_data.values():
        assert d["weight"] <= DEFAULT_CEILING + 1e-6


def test_weighted_neighbors_ranks_reinforced_path_closer(tmp_path: Path, vault_root: Path) -> None:
    """After reinforcement, the Dijkstra distance to the picked neighbor is < hop count."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")

    # Baseline: undirected hop distance from Designing AI Memory → Pullman Daemons is 1.
    baseline = store.neighbors_within(_u("Designing AI Memory.md"), depth=2, weighted=True)
    pullman_baseline = baseline[_u("Pullman Daemons.md")]

    for _ in range(3):
        apply_selection(
            store,
            seed_note_uuid=_u("Designing AI Memory.md"),
            selected_note_uuid=_u("Pullman Daemons.md"),
        )

    reinforced = store.neighbors_within(_u("Designing AI Memory.md"), depth=2, weighted=True)
    assert reinforced[_u("Pullman Daemons.md")] < pullman_baseline


def test_decay_unused_edges_pulls_toward_baseline(tmp_path: Path, vault_root: Path) -> None:
    """An edge reinforced two half-lives ago decays roughly to 1.0 + 0.25*(orig-1.0)."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")

    apply_selection(
        store,
        seed_note_uuid=_u("Designing AI Memory.md"),
        selected_note_uuid=_u("Pullman Daemons.md"),
    )
    edge_data = store.graph[f"note::{_u('Designing AI Memory.md')}"][f"note::{_u('Pullman Daemons.md')}"]
    pre_weights = {k: d["weight"] for k, d in edge_data.items()}
    assert all(w > 1.0 for w in pre_weights.values())

    # Backdate the reinforcement stamp by 60 days. With half_life_days=30 that's
    # exactly two half-lives → factor 0.25 toward the baseline of 1.0.
    sixty_days_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    for d in edge_data.values():
        d["last_reinforced_at"] = sixty_days_ago

    report = decay_unused_edges(store, half_life_days=30)
    assert report.edges_decayed >= 1
    for k, d in edge_data.items():
        original = pre_weights[k]
        expected = 1.0 + (original - 1.0) * 0.25
        assert abs(d["weight"] - expected) < 1e-3


def test_decay_skips_never_reinforced_edges(tmp_path: Path, vault_root: Path) -> None:
    """Edges without a ``last_reinforced_at`` stamp aren't touched."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")
    edge_count_before = store.graph.number_of_edges()

    report = decay_unused_edges(store, half_life_days=30)

    assert report.edges_visited == 0
    assert report.edges_decayed == 0
    assert store.graph.number_of_edges() == edge_count_before
