# SPDX-License-Identifier: Apache-2.0
"""`daemon backup` / `daemon restore` — the safety net, deliberately boring.

Snapshots (`stores/snapshot.py`) freeze state *for analysis* and live under
`./data`, which is exactly the tree `daemon reset` clears — so the daemon had
no way to survive its own maintenance. A backup bundle is the opposite: it
lives outside `./data`, it is restorable, and it excludes the vectors on
purpose, because they are the one part of the state that comes back for free
from `daemon ingest --full`.

The parts that are *not* free — the learned edge weights in `graph.gpickle` and
the ledger/themes/feedback in `feedback.db` — are what these tests guard.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import BACKUP_BUNDLE_VERSION, app
from my_daemon.config import Settings
from my_daemon.models import FeedbackEvent
from my_daemon.stores import FeedbackStore, GraphStore
from my_daemon.stores.db import SCHEMA_VERSION

runner = CliRunner()

_BOX_CHARS = "┏┓┗┛━─│┃┡┩┠┨╇┼├┤┬┴╭╮╰╯╺╸"


def _squash(text: str) -> str:
    stripped = text.translate({ord(c): " " for c in _BOX_CHARS})
    return " ".join(stripped.split())


CONFIG_TEXT = "vault:\n  path: /srv/notes\n"


def _log_query(settings: Settings, query: str) -> int:
    store = FeedbackStore(db_path=settings.feedback.db_path)
    return store.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query=query,
            retrieval_summary={"ranked": []},
            answer="an answer",
            latency_ms=1,
        )
    )


def _write_graph(path: Path, note_count: int) -> None:
    import networkx as nx

    graph = nx.MultiDiGraph()
    for i in range(note_count):
        graph.add_node(f"note::{i}", type="note", chunk_ids=[])
    store = GraphStore(path=path)
    store.graph = graph
    store.save()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    root = tmp_path / "home"
    root.mkdir()
    config = root / "config.yaml"
    config.write_text(CONFIG_TEXT, encoding="utf-8")

    s = Settings()
    s.config_path = config
    s.vault.path = tmp_path / "vault"
    s.graph.path = root / "data" / "graph.gpickle"
    s.graph.manifest_path = root / "data" / "manifest.json"
    s.feedback.db_path = root / "data" / "feedback.db"
    s.backup.dir = root / "backups"

    s.graph.path.parent.mkdir(parents=True, exist_ok=True)
    _write_graph(s.graph.path, note_count=3)
    s.graph.manifest_path.write_text(json.dumps({"uuid-1": {"path": "a.md"}}), encoding="utf-8")
    _log_query(s, "the original query")

    monkeypatch.setattr("my_daemon.cli._load", lambda: s)
    return s


def _only_bundle(settings: Settings) -> Path:
    bundles = sorted(p for p in settings.backup.dir.iterdir() if p.is_dir())
    assert len(bundles) == 1, bundles
    return bundles[0]


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def test_backup_writes_the_four_files_and_its_own_metadata(settings: Settings):
    result = runner.invoke(app, ["backup"])

    assert result.exit_code == 0, result.output
    bundle = _only_bundle(settings)
    assert (bundle / "feedback.db").is_file()
    assert (bundle / "graph.gpickle").is_file()
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "config.yaml").read_text(encoding="utf-8") == CONFIG_TEXT

    meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    assert meta["bundle_version"] == BACKUP_BUNDLE_VERSION
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["my_daemon_version"]
    assert meta["created_at"]
    assert set(meta["files"]) == {"feedback.db", "graph.gpickle", "manifest.json", "config.yaml"}


def test_backup_defaults_to_the_configured_dir(settings: Settings):
    result = runner.invoke(app, ["backup"])

    assert result.exit_code == 0, result.output
    assert _only_bundle(settings).parent == settings.backup.dir


def test_backup_honours_an_explicit_destination(settings: Settings, tmp_path: Path):
    dest = tmp_path / "usb-stick"

    result = runner.invoke(app, ["backup", str(dest)])

    assert result.exit_code == 0, result.output
    bundles = list(dest.iterdir())
    assert len(bundles) == 1
    assert not settings.backup.dir.exists()


def test_backup_says_the_vectors_are_excluded_and_why(settings: Settings):
    result = runner.invoke(app, ["backup"])

    squashed = _squash(result.output)
    assert "Vectors are not included" in squashed
    assert "ingest --full" in squashed


def test_backup_captures_writes_still_sitting_in_the_wal(settings: Settings):
    """A plain file copy of a live SQLite db loses everything in the -wal.

    The store is deliberately left open, which is the realistic case: the GUI
    is running and you take a backup.
    """
    store = FeedbackStore(db_path=settings.feedback.db_path)
    store.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="written moments ago",
            retrieval_summary={"ranked": []},
            answer="",
            latency_ms=0,
        )
    )

    result = runner.invoke(app, ["backup"])

    assert result.exit_code == 0, result.output
    copied = _only_bundle(settings) / "feedback.db"
    conn = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
    try:
        queries = {row[0] for row in conn.execute("SELECT query FROM feedback")}
    finally:
        conn.close()
    assert "written moments ago" in queries


def test_backup_skips_files_that_do_not_exist_yet(settings: Settings):
    settings.graph.manifest_path.unlink()

    result = runner.invoke(app, ["backup"])

    assert result.exit_code == 0, result.output
    bundle = _only_bundle(settings)
    assert not (bundle / "manifest.json").exists()
    meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    assert "manifest.json" not in meta["files"]


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


def test_backup_list_enumerates_bundles(settings: Settings):
    runner.invoke(app, ["backup"])

    result = runner.invoke(app, ["backup", "--list"])

    assert result.exit_code == 0, result.output
    assert _only_bundle(settings).name in _squash(result.output)


def test_backups_is_the_same_listing(settings: Settings):
    runner.invoke(app, ["backup"])

    result = runner.invoke(app, ["backups"])

    assert result.exit_code == 0, result.output
    assert _only_bundle(settings).name in _squash(result.output)


def test_listing_an_empty_backup_dir_says_so(settings: Settings):
    result = runner.invoke(app, ["backups"])

    assert result.exit_code == 0, result.output
    assert "No backups" in _squash(result.output)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_puts_the_old_state_back(settings: Settings):
    assert runner.invoke(app, ["backup"]).exit_code == 0
    bundle = _only_bundle(settings)

    # Move on: new graph, new query, new manifest.
    _write_graph(settings.graph.path, note_count=9)
    _log_query(settings, "a query from after the backup")
    settings.graph.manifest_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["restore", str(bundle), "--yes"])

    assert result.exit_code == 0, result.output
    store = GraphStore(path=settings.graph.path)
    store.load()
    assert store.stats().note_count == 3
    assert json.loads(settings.graph.manifest_path.read_text(encoding="utf-8")) == {
        "uuid-1": {"path": "a.md"}
    }
    queries = {
        row["query"] for row in FeedbackStore(db_path=settings.feedback.db_path).recent(limit=10)
    }
    assert "the original query" in queries
    assert "a query from after the backup" not in queries


def test_restore_keeps_a_pre_restore_copy_of_what_it_overwrote(settings: Settings):
    assert runner.invoke(app, ["backup"]).exit_code == 0
    bundle = _only_bundle(settings)
    _write_graph(settings.graph.path, note_count=9)

    result = runner.invoke(app, ["restore", str(bundle), "--yes"])

    assert result.exit_code == 0, result.output
    siblings = [p for p in bundle.parent.iterdir() if p.name.startswith("pre-restore-")]
    assert len(siblings) == 1, siblings
    saved = GraphStore(path=siblings[0] / "graph.gpickle")
    saved.load()
    assert saved.stats().note_count == 9, "the replaced state was not preserved"


def test_restore_prints_the_reingest_caveat(settings: Settings):
    assert runner.invoke(app, ["backup"]).exit_code == 0

    result = runner.invoke(app, ["restore", str(_only_bundle(settings)), "--yes"])

    assert "ingest --full" in _squash(result.output)


def test_restore_aborts_at_the_prompt_without_touching_anything(settings: Settings):
    assert runner.invoke(app, ["backup"]).exit_code == 0
    bundle = _only_bundle(settings)
    _write_graph(settings.graph.path, note_count=9)

    result = runner.invoke(app, ["restore", str(bundle)], input="no\n")

    assert result.exit_code == 1
    store = GraphStore(path=settings.graph.path)
    store.load()
    assert store.stats().note_count == 9
    assert not [p for p in bundle.parent.iterdir() if p.name.startswith("pre-restore-")]


def test_restore_refuses_a_bundle_from_a_newer_schema(settings: Settings):
    assert runner.invoke(app, ["backup"]).exit_code == 0
    bundle = _only_bundle(settings)
    meta_path = bundle / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["schema_version"] = SCHEMA_VERSION + 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    _write_graph(settings.graph.path, note_count=9)

    result = runner.invoke(app, ["restore", str(bundle), "--yes"])

    assert result.exit_code == 1
    assert "newer" in _squash(result.output)
    store = GraphStore(path=settings.graph.path)
    store.load()
    assert store.stats().note_count == 9


def test_restore_refuses_an_unknown_bundle_version(settings: Settings):
    assert runner.invoke(app, ["backup"]).exit_code == 0
    bundle = _only_bundle(settings)
    meta_path = bundle / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["bundle_version"] = BACKUP_BUNDLE_VERSION + 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = runner.invoke(app, ["restore", str(bundle), "--yes"])

    assert result.exit_code == 1
    assert "version" in _squash(result.output)


def test_restore_refuses_a_directory_that_is_not_a_bundle(
    settings: Settings, tmp_path: Path
):
    stray = tmp_path / "holiday-photos"
    stray.mkdir()

    result = runner.invoke(app, ["restore", str(stray), "--yes"])

    assert result.exit_code == 1
    assert "metadata.json" in _squash(result.output)


def test_restore_refuses_a_missing_directory(settings: Settings, tmp_path: Path):
    result = runner.invoke(app, ["restore", str(tmp_path / "ghost"), "--yes"])

    assert result.exit_code == 1


def test_restore_clears_stale_sqlite_sidecars(settings: Settings):
    """A -wal left over from the replaced database would be applied to the new one."""
    assert runner.invoke(app, ["backup"]).exit_code == 0
    bundle = _only_bundle(settings)

    # An open connection is what keeps a -wal on disk — i.e. the GUI is running.
    conn = sqlite3.connect(settings.feedback.db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        "INSERT INTO feedback (timestamp, query, retrieval_summary, answer, latency_ms) "
        "VALUES ('2026-07-26T00:00:00', 'uncheckpointed', '{}', '', 0)"
    )
    conn.commit()
    wal = settings.feedback.db_path.with_name("feedback.db-wal")
    assert wal.exists(), "precondition: the database must have an active -wal"

    try:
        result = runner.invoke(app, ["restore", str(bundle), "--yes"])
    finally:
        conn.close()

    assert result.exit_code == 0, result.output
    assert not wal.exists()
