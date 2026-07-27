# SPDX-License-Identifier: Apache-2.0
"""Snapshot bundle creation, read-only access, list/delete/prune."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_daemon.config import (
    FeedbackConfig,
    GraphConfig,
    Settings,
    SnapshotConfig,
)
from my_daemon.models import FeedbackEvent
from my_daemon.stores import (
    FeedbackStore,
    GraphStore,
    create_snapshot,
    delete_snapshot,
    get_snapshot,
    list_snapshots,
    open_readonly,
    prune_snapshots,
)
from my_daemon.stores.snapshot import BUNDLE_VERSION
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note


def _build_settings(tmp_path: Path, vault_root: Path) -> Settings:
    """Wire all path-bearing config to land under ``tmp_path``."""
    settings = Settings()
    settings.graph = GraphConfig(
        path=tmp_path / "graph.gpickle",
        manifest_path=tmp_path / "manifest.json",
    )
    settings.feedback = FeedbackConfig(db_path=tmp_path / "feedback.db")
    settings.snapshot = SnapshotConfig(dir=tmp_path / "snapshots", retention_days=7)
    settings.vault.path = vault_root
    return settings


def _seed_live_state(settings: Settings, vault_root: Path) -> None:
    """Populate graph + feedback + manifest the way ingest would, minus Qdrant."""
    graph = GraphStore(path=settings.graph.path)
    for note in VaultReader(vault_root).read_all():
        chunks = chunk_note(note)
        graph.add_note(note, chunk_ids=[c.id for c in chunks])
    graph.save()

    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="why are we here",
            retrieval_summary={"ranked": [{"note_path": "Pullman Daemons.md"}]},
            answer="To remember.",
            latency_ms=12,
        )
    )

    settings.graph.manifest_path.write_text(
        json.dumps({"version": 1, "notes": {}}, indent=2), encoding="utf-8"
    )


def test_create_snapshot_writes_bundle(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)

    bundle = create_snapshot(settings, include_qdrant=False)

    assert bundle.dir.is_dir()
    assert bundle.graph_path.is_file()
    assert bundle.feedback_path.is_file()
    assert bundle.manifest_path is not None and bundle.manifest_path.is_file()
    assert bundle.qdrant_snapshot is None  # explicitly skipped

    meta = json.loads((bundle.dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["bundle_version"] == BUNDLE_VERSION
    assert meta["id"] == bundle.id


def test_open_readonly_graph_refuses_writes(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)
    bundle = create_snapshot(settings, include_qdrant=False)

    handle = open_readonly(bundle)
    try:
        # Reads work — the graph was actually populated.
        assert handle.graph.stats().note_count >= 1
        # Writes raise.
        with pytest.raises(RuntimeError, match="read-only"):
            handle.graph.save()
    finally:
        handle.close()


def test_open_readonly_feedback_refuses_writes(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)
    bundle = create_snapshot(settings, include_qdrant=False)

    handle = open_readonly(bundle)
    try:
        # Reading the seeded event back through the read-only handle works.
        rows = handle.feedback.recent(limit=5)
        assert rows and rows[0]["query"] == "why are we here"

        # Attempting to write through the read-only store raises.
        with pytest.raises(RuntimeError, match="read-only"):
            handle.feedback.attach_signal(rows[0]["id"], "explicit_up")

        with pytest.raises(RuntimeError, match="read-only"):
            handle.feedback.log(
                FeedbackEvent(
                    timestamp=datetime.now(UTC),
                    query="should fail",
                    retrieval_summary={},
                    answer="",
                    latency_ms=0,
                )
            )
    finally:
        handle.close()


def test_open_readonly_does_not_mutate_live_state(tmp_path: Path, vault_root: Path) -> None:
    """A snapshot is a frozen *copy* — touching it cannot reach the live files."""
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)
    bundle = create_snapshot(settings, include_qdrant=False)

    pre_size = settings.graph.path.stat().st_size
    pre_mtime = settings.graph.path.stat().st_mtime

    handle = open_readonly(bundle)
    try:
        # Mutate the in-memory graph (doesn't persist because read_only blocks save).
        node = next(iter(handle.graph.graph.nodes))
        handle.graph.graph.nodes[node]["scratch"] = "snapshot-only"
    finally:
        handle.close()

    assert settings.graph.path.stat().st_size == pre_size
    assert settings.graph.path.stat().st_mtime == pre_mtime


def test_list_snapshots_returns_bundles_in_order(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)

    b1 = create_snapshot(settings, include_qdrant=False)
    # Pre-fabricate a second bundle whose id sorts after b1's so the ordering
    # check passes even when both fall in the same wall-clock second.
    later_id = (b1.created_at + timedelta(minutes=1)).strftime("%Y-%m-%dT%H-%M-%SZ")
    later_dir = settings.snapshot.dir / later_id
    later_dir.mkdir(parents=True, exist_ok=True)
    (later_dir / "graph.gpickle").write_bytes(b1.graph_path.read_bytes())
    (later_dir / "feedback.db").write_bytes(b1.feedback_path.read_bytes())
    (later_dir / "metadata.json").write_text(
        json.dumps(
            {
                "bundle_version": BUNDLE_VERSION,
                "id": later_id,
                "created_at": (b1.created_at + timedelta(minutes=1)).isoformat(),
                "qdrant_collection": settings.vector_store.qdrant.collection,
                "qdrant_snapshot": None,
                "qdrant_snapshot_name": None,
                "graph_path": str(later_dir / "graph.gpickle"),
                "feedback_path": str(later_dir / "feedback.db"),
                "manifest_path": None,
                "notes": {},
            }
        ),
        encoding="utf-8",
    )

    bundles = list_snapshots(settings)
    ids = [b.id for b in bundles]
    assert ids == sorted(ids)
    assert b1.id in ids
    assert later_id in ids


def test_get_snapshot_returns_none_for_missing(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    assert get_snapshot(settings, "does-not-exist") is None


def test_delete_snapshot_removes_bundle_dir(tmp_path: Path, vault_root: Path) -> None:
    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)
    bundle = create_snapshot(settings, include_qdrant=False)

    delete_snapshot(bundle)

    assert not bundle.dir.exists()
    assert get_snapshot(settings, bundle.id) is None


def test_prune_snapshots_drops_aged_bundles(tmp_path: Path, vault_root: Path) -> None:
    """Backdate a fabricated bundle so prune sees one fresh + one stale."""
    settings = _build_settings(tmp_path, vault_root)
    settings.snapshot = SnapshotConfig(dir=tmp_path / "snapshots", retention_days=3)
    _seed_live_state(settings, vault_root)

    fresh = create_snapshot(settings, include_qdrant=False)

    # Pre-fabricate a second bundle dated 30 days ago. We sidestep
    # create_snapshot here because its id resolution is whole-seconds, so two
    # back-to-back calls would collide.
    stale_created_at = datetime.now(UTC) - timedelta(days=30)
    stale_id = stale_created_at.strftime("%Y-%m-%dT%H-%M-%SZ") + "-aged"
    stale_dir = settings.snapshot.dir / stale_id
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "graph.gpickle").write_bytes(fresh.graph_path.read_bytes())
    (stale_dir / "feedback.db").write_bytes(fresh.feedback_path.read_bytes())
    (stale_dir / "metadata.json").write_text(
        json.dumps(
            {
                "bundle_version": BUNDLE_VERSION,
                "id": stale_id,
                "created_at": stale_created_at.isoformat(),
                "qdrant_collection": settings.vector_store.qdrant.collection,
                "qdrant_snapshot": None,
                "qdrant_snapshot_name": None,
                "graph_path": str(stale_dir / "graph.gpickle"),
                "feedback_path": str(stale_dir / "feedback.db"),
                "manifest_path": None,
                "notes": {},
            }
        ),
        encoding="utf-8",
    )

    deleted = prune_snapshots(settings)

    deleted_ids = {b.id for b in deleted}
    assert stale_id in deleted_ids
    assert fresh.id not in deleted_ids
    assert fresh.dir.exists()
    assert not stale_dir.exists()


def test_create_snapshot_survives_missing_live_state(tmp_path: Path, vault_root: Path) -> None:
    """First-time `snapshot create` with no graph/feedback/manifest still produces a bundle."""
    settings = _build_settings(tmp_path, vault_root)
    # Note: NO _seed_live_state call.

    bundle = create_snapshot(settings, include_qdrant=False)

    # Empty-but-valid stores for the readers downstream.
    assert bundle.graph_path.is_file()
    assert bundle.feedback_path.is_file()
    assert bundle.manifest_path is None  # absent in source → absent in bundle
    handle = open_readonly(bundle)
    try:
        assert handle.graph.stats().note_count == 0
        assert handle.feedback.recent() == []
    finally:
        handle.close()


def test_qdrant_returning_no_snapshot_degrades_to_a_warning(
    tmp_path: Path, vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create_snapshot` is Optional in qdrant-client, and None used to be an
    AttributeError three lines later. It should read as a skipped vector
    snapshot — the rest of the bundle is still worth writing."""

    import qdrant_client

    class _NoSnapshotClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create_snapshot(self, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(qdrant_client, "QdrantClient", _NoSnapshotClient)

    settings = _build_settings(tmp_path, vault_root)
    _seed_live_state(settings, vault_root)

    warnings: list[str] = []
    bundle = create_snapshot(settings, include_qdrant=True, on_warning=warnings.append)

    assert bundle.qdrant_snapshot is None
    assert bundle.graph_path.is_file()
    assert warnings and "did not create a snapshot" in warnings[0]
