# SPDX-License-Identifier: Apache-2.0
"""``GraphStore.update_note`` — differential re-ingest of a single note.

Regression cover for the bug this replaced: ingest used to call
``remove_note`` + ``add_note``, and ``remove_node`` drops *all* incident edges
in both directions while ``add_note`` only rebuilds the note's own outgoing
edges at ``weight=1.0``. Editing one note therefore deleted every inbound
wikilink to it and reset every weight M1 had learned on any edge touching it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from my_daemon.models import Note
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores.graph import GraphStore

# Notes are keyed by identity now, so the fixtures carry stable uuids and the
# helpers speak in note names that map onto them.
_UUIDS = {
    "A.md": "aaaaaaaa-0000-4000-8000-000000000001",
    "B.md": "bbbbbbbb-0000-4000-8000-000000000002",
    "C.md": "cccccccc-0000-4000-8000-000000000003",
}


def _uuid(rel_path: str) -> str:
    return _UUIDS[rel_path]


def _note(
    rel_path: str,
    *,
    wikilinks: list[str] | None = None,
    dangling_wikilinks: list[str] | None = None,
    tags: list[str] | None = None,
    title: str | None = None,
    body: str = "body",
) -> Note:
    return Note(
        path=Path("/vault") / rel_path,
        relative_path=rel_path,
        title=title or rel_path.removesuffix(".md"),
        body=body,
        wikilink_uuids=[_uuid(w) for w in (wikilinks or [])],
        dangling_wikilinks=dangling_wikilinks or [],
        tags=tags or [],
        mtime=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        word_count=len(body.split()),
        uuid=_uuid(rel_path),
    )


def _store(tmp_path: Path) -> GraphStore:
    return GraphStore(path=tmp_path / "graph.gpickle")


def _edge_weight(store: GraphStore, src: str, dst: str) -> float:
    data = store.graph[f"note::{_uuid(src)}"][f"note::{_uuid(dst)}"]
    return max(float(d.get("weight", 1.0)) for d in data.values())


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------


def test_updating_a_note_keeps_inbound_links_from_other_notes(tmp_path: Path):
    """Editing B must not delete A's link to B."""
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md"), chunk_ids=["b1"])
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('B.md')}")

    store.update_note(_note("B.md", body="edited"), chunk_ids=["b2"])

    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('B.md')}")


def test_updating_a_note_preserves_learned_edge_weights(tmp_path: Path):
    """The whole point of M1: reinforcement must survive a re-ingest."""
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md"), chunk_ids=["b1"])
    apply_selection(store, seed_note_uuid=_uuid("A.md"), selected_note_uuid=_uuid("B.md"))
    reinforced = _edge_weight(store, "A.md", "B.md")
    assert reinforced > 1.0

    store.update_note(_note("A.md", wikilinks=["B.md"], body="edited"), chunk_ids=["a2"])

    assert _edge_weight(store, "A.md", "B.md") == reinforced


def test_updating_a_note_preserves_last_reinforced_at(tmp_path: Path):
    """`decay_unused_edges` reads this timestamp; losing it resets the clock."""
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md"), chunk_ids=["b1"])
    apply_selection(store, seed_note_uuid=_uuid("A.md"), selected_note_uuid=_uuid("B.md"))
    stamped = [
        d.get("last_reinforced_at")
        for d in store.graph[f"note::{_uuid('A.md')}"][f"note::{_uuid('B.md')}"].values()
    ]
    assert any(stamped)

    store.update_note(_note("A.md", wikilinks=["B.md"], body="edited"), chunk_ids=["a2"])

    after = [
        d.get("last_reinforced_at")
        for d in store.graph[f"note::{_uuid('A.md')}"][f"note::{_uuid('B.md')}"].values()
    ]
    assert after == stamped


def test_updating_a_note_preserves_tag_edge_weights(tmp_path: Path):
    store = _store(tmp_path)
    store.add_note(_note("A.md", tags=["memory"]), chunk_ids=["a1"])
    edata = next(iter(store.graph[f"note::{_uuid('A.md')}"]["tag::memory"].values()))
    edata["weight"] = 2.5

    store.update_note(_note("A.md", tags=["memory"], body="edited"), chunk_ids=["a2"])

    weights = [
        float(d.get("weight", 1.0))
        for d in store.graph[f"note::{_uuid('A.md')}"]["tag::memory"].values()
    ]
    assert weights == [2.5]


# ---------------------------------------------------------------------------
# It still has to be an *update*
# ---------------------------------------------------------------------------


def test_update_note_drops_wikilinks_the_author_removed(tmp_path: Path):
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md", "C.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md"), chunk_ids=["b1"])
    store.add_note(_note("C.md"), chunk_ids=["c1"])

    store.update_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a2"])

    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('B.md')}")
    assert not store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('C.md')}")


def test_update_note_adds_wikilinks_the_author_introduced(tmp_path: Path):
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md"), chunk_ids=["b1"])

    store.update_note(_note("A.md", wikilinks=["B.md", "C.md"]), chunk_ids=["a2"])

    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('C.md')}")


def test_update_note_drops_tags_the_author_removed(tmp_path: Path):
    store = _store(tmp_path)
    store.add_note(_note("A.md", tags=["memory", "stale"]), chunk_ids=["a1"])

    store.update_note(_note("A.md", tags=["memory"]), chunk_ids=["a2"])

    assert store.graph.has_edge(f"note::{_uuid('A.md')}", "tag::memory")
    assert not store.graph.has_edge(f"note::{_uuid('A.md')}", "tag::stale")


def test_a_removed_then_readded_link_starts_over_at_weight_one(tmp_path: Path):
    """Weight carries across a diff, not across a deliberate deletion."""
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md"), chunk_ids=["b1"])
    apply_selection(store, seed_note_uuid=_uuid("A.md"), selected_note_uuid=_uuid("B.md"))
    assert _edge_weight(store, "A.md", "B.md") > 1.0

    store.update_note(_note("A.md", wikilinks=[]), chunk_ids=["a2"])
    store.update_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a3"])

    assert _edge_weight(store, "A.md", "B.md") == 1.0


def test_update_note_refreshes_node_attributes(tmp_path: Path):
    store = _store(tmp_path)
    store.add_note(_note("A.md", title="Old"), chunk_ids=["a1"])

    store.update_note(_note("A.md", title="New"), chunk_ids=["a2", "a3"])

    node = store.graph.nodes[f"note::{_uuid('A.md')}"]
    assert node["title"] == "New"
    assert node["chunk_ids"] == ["a2", "a3"]


def test_update_note_on_an_unknown_note_behaves_like_add(tmp_path: Path):
    store = _store(tmp_path)

    store.update_note(_note("A.md", wikilinks=["B.md"], tags=["t"]), chunk_ids=["a1"])

    assert store.graph.nodes[f"note::{_uuid('A.md')}"]["type"] == "note"
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('B.md')}")
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", "tag::t")


def test_an_unresolved_link_lands_in_the_dangling_namespace(tmp_path: Path):
    """A target with no note behind it has no identity to key on.

    Keeping it out of `note::` is what stops a "note you keep meaning to
    write" from colliding with a real note's uuid.
    """
    store = _store(tmp_path)

    store.update_note(
        _note("A.md", dangling_wikilinks=["Someday"]), chunk_ids=["a1"]
    )

    assert store.graph.nodes["dangling::Someday"]["dangling"] is True
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", "dangling::Someday")


def test_resolving_a_dangling_link_drops_the_placeholder_edge(tmp_path: Path):
    """Once the target exists the reader resolves it, so the diff moves the edge."""
    store = _store(tmp_path)
    store.update_note(
        _note("A.md", dangling_wikilinks=["B"]), chunk_ids=["a1"]
    )
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", "dangling::B")

    store.add_note(_note("B.md"), chunk_ids=["b1"])
    store.update_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])

    assert not store.graph.has_edge(f"note::{_uuid('A.md')}", "dangling::B")
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('B.md')}")


def test_update_note_does_not_disturb_unrelated_edges(tmp_path: Path):
    store = _store(tmp_path)
    store.add_note(_note("A.md", wikilinks=["B.md"]), chunk_ids=["a1"])
    store.add_note(_note("B.md", wikilinks=["C.md"]), chunk_ids=["b1"])
    store.add_note(_note("C.md"), chunk_ids=["c1"])
    before = store.graph.number_of_edges()

    store.update_note(_note("B.md", wikilinks=["C.md"], body="edited"), chunk_ids=["b2"])

    assert store.graph.number_of_edges() == before
    assert store.graph.has_edge(f"note::{_uuid('A.md')}", f"note::{_uuid('B.md')}")
    assert store.graph.has_edge(f"note::{_uuid('B.md')}", f"note::{_uuid('C.md')}")
