# SPDX-License-Identifier: Apache-2.0
"""The reinforcement → expansion seam.

``apply_selection`` bumps edge weights; ``neighbors_within(weighted=True)``
turns those weights into fractional Dijkstra distances; ``expand_from_seeds``
puts each distance onto a ``RetrievedChunk``. Each half had tests; the join
did not — so the first reinforced edge in a real vault produced a fractional
distance that the ``int``-typed field rejected, and every query seeded near
that note died with a ``ValidationError``.

These tests run the two halves together against a real ``GraphStore`` built
from the fixture vault, with only Qdrant faked out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.models import RetrievedChunk
from my_daemon.retrieval.expand import expand_from_seeds
from my_daemon.retrieval.weights import DEFAULT_ALPHA, apply_selection
from my_daemon.stores.graph import GraphStore
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note

# Fixture notes carry no `uuid:` frontmatter, so their identity is the
# deterministic path-derived fallback.
from my_daemon.vault.identity import derive_path_uuid as _u

DESIGNING = _u("Designing AI Memory.md")
PULLMAN = _u("Pullman Daemons.md")
SOCRATIC = _u("Socratic Daemon.md")

SEED_SCORE = 0.8
DECAY = 0.5

# One `apply_selection` bump on a virgin edge: 1.0 + alpha → the weighted
# traversal costs 1/weight, so the hop shrinks from 1.0 to 1/1.5.
REINFORCED_WEIGHT = 1.0 + DEFAULT_ALPHA
REINFORCED_DISTANCE = 1.0 / REINFORCED_WEIGHT


# ---------------------------------------------------------------------------
# fakes — Qdrant only; the graph is real
# ---------------------------------------------------------------------------


class _Point:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


class _FakeQdrantClient:
    """Serves note payloads out of a dict, reading the filter Qdrant-style."""

    def __init__(self, payloads_by_uuid: dict[str, list[dict]]) -> None:
        self.payloads_by_uuid = payloads_by_uuid
        self.scrolled_uuids: list[str] = []

    def scroll(self, *, collection_name, scroll_filter, with_payload, limit):  # noqa: ANN001
        wanted = scroll_filter.must[0].match.value
        self.scrolled_uuids.append(wanted)
        return [_Point(p) for p in self.payloads_by_uuid.get(wanted, [])], None


class _FakeVectorStore:
    collection = "chunks"

    def __init__(self, payloads_by_uuid: dict[str, list[dict]]) -> None:
        self.client = _FakeQdrantClient(payloads_by_uuid)

    def _client_(self) -> _FakeQdrantClient:
        return self.client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _vault_graph_and_payloads(
    vault_root: Path, store_path: Path
) -> tuple[GraphStore, dict[str, list[dict]], dict[str, list]]:
    store = GraphStore(path=store_path)
    payloads: dict[str, list[dict]] = {}
    chunks_by_uuid: dict[str, list] = {}
    for note in VaultReader(vault_root).read_all():
        chunks = chunk_note(note)
        store.add_note(note, chunk_ids=[c.id for c in chunks])
        for c in chunks:
            chunks_by_uuid.setdefault(c.note_uuid, []).append(c)
            payloads.setdefault(c.note_uuid, []).append(
                {
                    "chunk_id": c.id,
                    "note_uuid": c.note_uuid,
                    "note_path": c.note_path,
                    "heading_path": c.heading_path,
                    "text": c.text,
                    "chunk_index": c.chunk_index,
                    "tags": c.tags,
                    "wikilinks": c.wikilinks,
                }
            )
    return store, payloads, chunks_by_uuid


def _seed_from(chunks_by_uuid: dict[str, list], note_uuid: str) -> RetrievedChunk:
    chunk = chunks_by_uuid[note_uuid][0]
    return RetrievedChunk(
        chunk=chunk,
        vector_score=SEED_SCORE,
        graph_distance=0,
        seed_chunk_id=chunk.id,
        combined_score=SEED_SCORE,
    )


def _expand(vault_root: Path, tmp_path: Path, *, reinforcements: int) -> list[RetrievedChunk]:
    store, payloads, chunks_by_uuid = _vault_graph_and_payloads(
        vault_root, tmp_path / "g.gpickle"
    )
    for _ in range(reinforcements):
        apply_selection(store, seed_note_uuid=DESIGNING, selected_note_uuid=PULLMAN)

    return expand_from_seeds(
        [_seed_from(chunks_by_uuid, DESIGNING)],
        store,
        _FakeVectorStore(payloads),
        depth=2,
        decay=DECAY,
    )


def _distances_by_note(expanded: list[RetrievedChunk]) -> dict[str, float]:
    return {rc.chunk.note_uuid: rc.graph_distance for rc in expanded}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_uniform_weights_expand_at_whole_hop_distances(tmp_path: Path, vault_root: Path):
    """Baseline: every edge at 1.0 → every distance is exactly its hop count."""
    expanded = _expand(vault_root, tmp_path, reinforcements=0)
    distances = _distances_by_note(expanded)

    assert distances[PULLMAN] == 1.0
    assert distances[SOCRATIC] == 1.0


def test_a_reinforced_edge_expands_at_a_fractional_distance(tmp_path: Path, vault_root: Path):
    """The bug: one `daemon select` made this raise ValidationError."""
    expanded = _expand(vault_root, tmp_path, reinforcements=1)
    distances = _distances_by_note(expanded)

    assert distances[PULLMAN] == pytest.approx(REINFORCED_DISTANCE, abs=1e-12)
    # 2/3 is not an int, and it must not be rounded into one either.
    assert distances[PULLMAN] != 1
    # An untouched sibling edge is unmoved.
    assert distances[SOCRATIC] == 1.0


def test_the_fractional_distance_flows_into_the_decayed_score(tmp_path: Path, vault_root: Path):
    """The whole point of reinforcing: the picked note rides in higher."""
    expanded = _expand(vault_root, tmp_path, reinforcements=1)
    by_note = {rc.chunk.note_uuid: rc for rc in expanded}

    assert by_note[PULLMAN].combined_score == pytest.approx(
        SEED_SCORE * DECAY**REINFORCED_DISTANCE, abs=1e-12
    )
    assert by_note[SOCRATIC].combined_score == pytest.approx(SEED_SCORE * DECAY, abs=1e-12)
    assert by_note[PULLMAN].combined_score > by_note[SOCRATIC].combined_score


def test_expansion_still_carries_provenance_after_reinforcement(
    tmp_path: Path, vault_root: Path
):
    """Seed attribution survives the float path — `daemon select` depends on it."""
    _store, _payloads, chunks_by_uuid = _vault_graph_and_payloads(
        vault_root, tmp_path / "unused.gpickle"
    )
    seed_chunk_id = chunks_by_uuid[DESIGNING][0].id

    expanded = _expand(vault_root, tmp_path, reinforcements=1)

    assert {rc.seed_chunk_id for rc in expanded} == {seed_chunk_id}
    # The seed's own note is never re-emitted as an expansion.
    assert DESIGNING not in _distances_by_note(expanded)


def test_repeated_reinforcement_keeps_shortening_the_hop(tmp_path: Path, vault_root: Path):
    """Three picks: weight 1.0 → 2.5, distance 1.0 → 0.4."""
    expanded = _expand(vault_root, tmp_path, reinforcements=3)

    assert _distances_by_note(expanded)[PULLMAN] == pytest.approx(
        1.0 / (1.0 + 3 * DEFAULT_ALPHA), abs=1e-12
    )
