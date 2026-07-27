# SPDX-License-Identifier: Apache-2.0
"""The daemon must not read its own conclusions back as evidence.

Theme tags are the daemon's own output. If they feed the signals that produced
them — graph expansion, Louvain communities, the observer's evidence, the
linker's tag propagation — the system converges on its own reflection.

The boundary is deliberate and narrow: `theme/*` tags stay *real* in the vault
and in `stats()`. They are excluded only where the daemon would cite them back
to itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.models import THEME_TAG_PREFIX, Note, is_daemon_authored_tag
from my_daemon.stores.graph import GraphStore

UUIDS = {
    name: f"{chr(97 + i) * 8}-0000-4000-8000-00000000000{i}" for i, name in enumerate("ABCDEFGH")
}


def _note(name: str, *, tags=None, links=None) -> Note:
    return Note(
        path=Path("/vault") / f"{name}.md",
        relative_path=f"{name}.md",
        title=name,
        body=f"# {name}\n\nbody",
        tags=tags or [],
        wikilink_uuids=[UUIDS[t] for t in (links or [])],
        mtime=datetime(2026, 7, 26, tzinfo=UTC),
        word_count=3,
        uuid=UUIDS[name],
    )


# ---------------------------------------------------------------------------
# The predicate lives in exactly one place
# ---------------------------------------------------------------------------


def test_daemon_authored_tags_are_recognised():
    assert is_daemon_authored_tag(f"{THEME_TAG_PREFIX}why-projects-stall") is True
    assert is_daemon_authored_tag("philosophy") is False


def test_analysis_reexports_the_same_predicate():
    """One implementation, or the exclusions drift apart."""
    from my_daemon.analysis.themes import is_self_referential_tag

    assert is_self_referential_tag is is_daemon_authored_tag


# ---------------------------------------------------------------------------
# C — the observer must not cite its own labels
# ---------------------------------------------------------------------------


@pytest.fixture
def theme_glued_graph(tmp_path: Path) -> GraphStore:
    """Two unrelated notes whose *only* connection is a daemon-authored tag."""
    store = GraphStore(path=tmp_path / "g.gpickle")
    # Their ONLY shared structure is the daemon-authored tag.
    store.add_note(_note("A", tags=[f"{THEME_TAG_PREFIX}x", "alpha"]), ["a1"])
    store.add_note(_note("B", tags=[f"{THEME_TAG_PREFIX}x", "beta"]), ["b1"])
    # A genuinely separate pair, linked to each other, sharing a real tag.
    store.add_note(_note("C", tags=["cooking"], links=["D"]), ["c1"])
    store.add_note(_note("D", tags=["cooking"], links=["C"]), ["d1"])
    return store


def test_a_theme_tag_is_not_louvain_glue(theme_glued_graph: GraphStore):
    from my_daemon.analysis.structural import compute_report

    report = compute_report(theme_glued_graph)

    for community in report.communities:
        assert not ({"A.md", "B.md"} <= set(community.members)), (
            "notes joined a community solely via a daemon-authored tag"
        )


def test_a_theme_tag_never_reaches_the_observers_top_tags(
    theme_glued_graph: GraphStore,
):
    from my_daemon.analysis.structural import compute_report

    report = compute_report(theme_glued_graph)

    surfaced = {tag for c in report.communities for tag, _count in c.top_tags}
    assert not any(is_daemon_authored_tag(t) for t in surfaced)


def test_a_reinforced_theme_tag_edge_is_not_reported_as_warm(
    theme_glued_graph: GraphStore,
):
    """A warm edge is evidence in the letter. A daemon tag must not become one."""
    from my_daemon.analysis.structural import compute_report

    edge = next(
        iter(theme_glued_graph.graph[f"note::{UUIDS['A']}"][f"tag::{THEME_TAG_PREFIX}x"].values())
    )
    edge["weight"] = 4.0

    report = compute_report(theme_glued_graph, warm_threshold=1.5)

    assert not any(
        is_daemon_authored_tag(e.src) or is_daemon_authored_tag(e.dst) for e in report.warm_edges
    )


def test_an_ordinary_tag_still_works_as_glue(theme_glued_graph: GraphStore):
    """The exclusion must be surgical — real tags keep carrying signal."""
    from my_daemon.analysis.structural import compute_report

    report = compute_report(theme_glued_graph)

    assert any({"C.md", "D.md"} <= set(c.members) for c in report.communities)


# ---------------------------------------------------------------------------
# The boundary Evan chose: still real in the vault
# ---------------------------------------------------------------------------


def test_theme_tags_are_still_counted_in_graph_stats(theme_glued_graph: GraphStore):
    """Excluded from the daemon's evidence, NOT hidden from the daemon."""
    stats = theme_glued_graph.stats()

    assert any(is_daemon_authored_tag(t) for t, _count in stats.top_tags)


# ---------------------------------------------------------------------------
# E — the linker must not spread the daemon's conclusions
# ---------------------------------------------------------------------------


def test_the_linker_never_proposes_a_daemon_authored_tag(tmp_path: Path):
    from my_daemon.pipeline.agent_link import _gather_tag_candidates

    store = GraphStore(path=tmp_path / "g.gpickle")
    subject = _note("A", links=["B", "C", "D", "E"])
    neighbours = [
        _note(name, tags=[f"{THEME_TAG_PREFIX}x", "philosophy"]) for name in ("B", "C", "D", "E")
    ]
    store.add_note(subject, ["a1"])
    for n in neighbours:
        store.add_note(n, [f"{n.title.lower()}1"])
    by_uuid = {UUIDS[n.title]: n for n in [subject, *neighbours]}

    candidates = _gather_tag_candidates(subject, store, by_uuid, min_neighbor_count=4)

    assert "philosophy" in candidates, "an ordinary tag should still propagate"
    assert not any(is_daemon_authored_tag(t) for t in candidates)
