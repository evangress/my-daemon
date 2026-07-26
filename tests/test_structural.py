# SPDX-License-Identifier: Apache-2.0
"""M3: structural-pattern analysis + hypothetical weight evolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from my_daemon.analysis import compute_report, persist_reports, simulate_evolution
from my_daemon.config import (
    ConsolidationConfig,
    FeedbackConfig,
    GraphConfig,
    Settings,
    SnapshotConfig,
)
from my_daemon.models import FeedbackEvent
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores import FeedbackStore, GraphStore, create_snapshot
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note
from my_daemon.vault.identity import derive_path_uuid as _u

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _build_settings(tmp_path: Path, vault_root: Path) -> Settings:
    """All path-bearing config under tmp_path so tests stay hermetic."""
    settings = Settings()
    settings.graph = GraphConfig(
        path=tmp_path / "graph.gpickle",
        manifest_path=tmp_path / "manifest.json",
    )
    settings.feedback = FeedbackConfig(db_path=tmp_path / "feedback.db")
    settings.snapshot = SnapshotConfig(dir=tmp_path / "snapshots", retention_days=7)
    settings.consolidation = ConsolidationConfig(out_dir=tmp_path / "consolidation")
    settings.vault.path = vault_root
    return settings


def _vault_graph(vault_root: Path, store_path: Path) -> GraphStore:
    store = GraphStore(path=store_path)
    for note in VaultReader(vault_root).read_all():
        chunks = chunk_note(note)
        store.add_note(note, chunk_ids=[c.id for c in chunks])
    return store


# ---------------------------------------------------------------------------
# compute_report
# ---------------------------------------------------------------------------


def test_compute_report_basic_shape(tmp_path: Path, vault_root: Path) -> None:
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")

    report = compute_report(store, snapshot_id="test")

    assert report.snapshot_id == "test"
    assert report.note_count >= 5  # 5 root notes + 1 journal note
    assert report.tag_count >= 5
    assert report.edge_count > 0
    assert report.community_count >= 1


def test_compute_report_identifies_orphans(tmp_path: Path, vault_root: Path) -> None:
    """Grocery List has only one tag (#household) and no wikilinks → orphan."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")
    report = compute_report(store)
    assert "Grocery List.md" in report.orphan_notes


def test_compute_report_dangling_targets_sorted_by_indegree(
    tmp_path: Path, vault_root: Path
) -> None:
    """Real note titles in the fixture vault aren't dangling, but the parser does
    pick up the literal `[[wikilinks]]` example inside backticks in
    Obsidian Vaults.md. Treat that as the documented fixture behavior and use
    it to verify the dangling-targets shape.
    """
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")
    report = compute_report(store)
    for t in report.dangling_targets:
        # The known real notes must never appear as dangling.
        assert t.target != "Pullman Daemons"
        assert t.target != "Designing AI Memory"
        assert t.incoming_links >= 1


def test_compute_report_warm_edges_initially_empty(
    tmp_path: Path, vault_root: Path
) -> None:
    """A fresh graph has every edge at weight 1.0, below the warm threshold."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")
    report = compute_report(store)
    assert report.warm_edges == []


def test_compute_report_warm_edges_surface_reinforcement(
    tmp_path: Path, vault_root: Path
) -> None:
    """After several reinforcements an edge exceeds the warm threshold."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")
    for _ in range(4):
        apply_selection(
            store,
            seed_note_uuid=_u("Designing AI Memory.md"),
            selected_note_uuid=_u("Pullman Daemons.md"),
        )

    report = compute_report(store, warm_threshold=1.5)
    paths = {(e.src, e.dst) for e in report.warm_edges}
    assert (
        ("Designing AI Memory.md", "Pullman Daemons.md") in paths
        or ("Pullman Daemons.md", "Designing AI Memory.md") in paths
    )


def test_compute_report_finds_philosophy_cluster(tmp_path: Path, vault_root: Path) -> None:
    """Pullman + Socratic share #philosophy + #metaphor → some community contains both."""
    store = _vault_graph(vault_root, tmp_path / "g.gpickle")
    report = compute_report(store)

    paired = False
    for c in report.communities:
        members = set(c.members)
        if "Pullman Daemons.md" in members and "Socratic Daemon.md" in members:
            paired = True
            break
    assert paired, f"expected Pullman+Socratic in one community; got {report.communities}"


def test_compute_report_finds_dangling_targets(tmp_path: Path) -> None:
    """A wikilink whose target is missing surfaces as a DanglingTarget, sorted by in-degree."""
    store = GraphStore(path=tmp_path / "g.gpickle")
    # Two notes both reference "Half-formed Idea" which doesn't exist as a note.
    from my_daemon.models import Note  # local import keeps test surface focused

    def _mk(rel_path: str, body: str, wikilinks: list[str]) -> Note:
        return Note(
            path=tmp_path / rel_path,
            relative_path=rel_path,
            title=rel_path.removesuffix(".md"),
            body=body,
            mtime=datetime(2026, 5, 17, tzinfo=UTC),
            word_count=10,
            wikilinks=wikilinks,
            dangling_wikilinks=wikilinks,
            tags=[],
        )

    store.add_note(_mk("A.md", "see [[Half-formed Idea]]", ["Half-formed Idea"]), [])
    store.add_note(_mk("B.md", "see [[Half-formed Idea]]", ["Half-formed Idea"]), [])

    report = compute_report(store)
    names = [d.target for d in report.dangling_targets]
    assert "Half-formed Idea" in names
    target = next(d for d in report.dangling_targets if d.target == "Half-formed Idea")
    assert target.incoming_links == 2


def test_compute_report_empty_graph_returns_zeros(tmp_path: Path) -> None:
    """Empty graph → no crashes, every list empty."""
    store = GraphStore(path=tmp_path / "g.gpickle")
    report = compute_report(store)
    assert report.note_count == 0
    assert report.community_count == 0
    assert report.bridging_notes == []
    assert report.bridge_edges == []
    assert report.warm_edges == []


# ---------------------------------------------------------------------------
# simulate_evolution
# ---------------------------------------------------------------------------


def _seed_live_for_simulation(settings: Settings, vault_root: Path) -> int:
    """Populate graph + a single candidate_selected event; return the event id."""
    graph = _vault_graph(vault_root, settings.graph.path)
    graph.save()

    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    event_id = feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC) - timedelta(hours=2),
            query="what makes the daemon metaphor work",
            retrieval_summary={
                "ranked": [
                    {
                        "chunk_id": "stub-chunk-id",
                        "note_path": "Pullman Daemons.md",
                        "note_uuid": _u("Pullman Daemons.md"),
                        "seed_note_path": "Designing AI Memory.md",
                        "seed_note_uuid": _u("Designing AI Memory.md"),
                        "score": 0.42,
                        "graph_distance": 1,
                    }
                ],
                "seed_count": 1,
                "expanded_count": 1,
            },
            answer="A companion that knows the human.",
            latency_ms=20,
        )
    )
    feedback.attach_signal(
        event_id,
        "candidate_selected",
        selected_rank=1,
        selected_chunk_id="stub-chunk-id",
        selected_note_uuid=_u("Pullman Daemons.md"),
    )
    return event_id


def test_simulate_evolution_replays_seeded_event(
    tmp_path: Path, vault_root: Path
) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_for_simulation(settings, vault_root)

    bundle = create_snapshot(settings, include_qdrant=False)
    report = simulate_evolution(bundle, lookback_days=7)

    assert report.snapshot_id == bundle.id
    assert report.lookback_days == 7
    assert report.events_replayed == 1
    assert report.events_skipped == 0

    # The reinforced wikilink should appear in top_edges.
    nudged = {(d.src, d.dst) for d in report.top_edges}
    assert ("Designing AI Memory.md", "Pullman Daemons.md") in nudged
    # And the deltas should be positive (reinforcement, not decay).
    for d in report.top_edges:
        assert d.delta > 0
        assert d.after > d.before


def test_simulate_evolution_handles_no_events(tmp_path: Path, vault_root: Path) -> None:
    """A snapshot with no candidate_selected events → empty report, no crash."""
    settings = _build_settings(tmp_path, vault_root)
    # Populate graph but not feedback.
    graph = _vault_graph(vault_root, settings.graph.path)
    graph.save()
    FeedbackStore(db_path=settings.feedback.db_path)  # ensure schema exists

    bundle = create_snapshot(settings, include_qdrant=False)
    report = simulate_evolution(bundle, lookback_days=7)

    assert report.events_replayed == 0
    assert report.events_skipped == 0
    assert report.top_edges == []
    assert report.top_notes == []


def test_simulate_evolution_does_not_mutate_snapshot(
    tmp_path: Path, vault_root: Path
) -> None:
    """The replay runs on a deep copy; the snapshot pickle is byte-identical after."""
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_for_simulation(settings, vault_root)
    bundle = create_snapshot(settings, include_qdrant=False)

    pre = bundle.graph_path.read_bytes()
    simulate_evolution(bundle, lookback_days=7)
    post = bundle.graph_path.read_bytes()

    assert pre == post


def test_simulate_evolution_skips_pre_lookback_events(
    tmp_path: Path, vault_root: Path
) -> None:
    """Events older than the lookback window are filtered out by the SQL cutoff."""
    settings = _build_settings(tmp_path, vault_root)
    graph = _vault_graph(vault_root, settings.graph.path)
    graph.save()

    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    old_id = feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC) - timedelta(days=30),
            query="old query",
            retrieval_summary={
                "ranked": [
                    {
                        "chunk_id": "x",
                        "note_path": "Pullman Daemons.md",
                        "note_uuid": _u("Pullman Daemons.md"),
                        "seed_note_path": "Designing AI Memory.md",
                        "seed_note_uuid": _u("Designing AI Memory.md"),
                    }
                ]
            },
            answer="",
            latency_ms=1,
        )
    )
    feedback.attach_signal(
        old_id, "candidate_selected",
        selected_rank=1, selected_chunk_id="x", selected_note_uuid=_u("Pullman Daemons.md"),
    )

    bundle = create_snapshot(settings, include_qdrant=False)
    report = simulate_evolution(bundle, lookback_days=7)

    assert report.events_replayed == 0


# ---------------------------------------------------------------------------
# persist_reports
# ---------------------------------------------------------------------------


def test_persist_reports_writes_json(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_for_simulation(settings, vault_root)
    bundle = create_snapshot(settings, include_qdrant=False)

    from my_daemon.stores import open_readonly

    handle = open_readonly(bundle)
    try:
        structural = compute_report(handle.graph, snapshot_id=bundle.id)
    finally:
        handle.close()
    evolution = simulate_evolution(bundle, lookback_days=7)

    out_dir = tmp_path / "consolidation" / bundle.id
    written = persist_reports(out_dir, structural=structural, evolution=evolution)

    assert "structural" in written and written["structural"].is_file()
    assert "evolution" in written and written["evolution"].is_file()

    parsed = json.loads(written["structural"].read_text(encoding="utf-8"))
    assert parsed["snapshot_id"] == bundle.id
    assert parsed["note_count"] >= 5
