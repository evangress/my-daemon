# SPDX-License-Identifier: Apache-2.0
"""The schema-version ladder in ``stores/db.py``.

The critical property under test is that migration 1 is a no-op against a
database built by the pre-ladder code — every existing ``data/feedback.db``
in the wild has ``user_version == 0`` but already contains all the tables.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from my_daemon.stores import db as dbmod

# Exactly what the pre-ladder code produced: the two _SCHEMA scripts plus the
# three additively-added selection columns. Kept verbatim so this test keeps
# describing the legacy shape even as stores/db.py evolves.
_LEGACY_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    query TEXT NOT NULL,
    retrieval_summary TEXT NOT NULL,
    answer TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    signal TEXT,
    signal_captured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_timestamp ON feedback(timestamp);
CREATE TABLE IF NOT EXISTS agent_link_runs (
    note_path TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    note_mtime_seen REAL,
    applied_count INTEGER NOT NULL DEFAULT 0,
    suggested_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_extract_runs (
    note_path TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    note_mtime_seen REAL NOT NULL,
    summary_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_reflect_runs (
    theme TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    source_notes_hash TEXT NOT NULL,
    source_chat_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_observer_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    letter_path TEXT,
    written_at TEXT NOT NULL,
    model TEXT NOT NULL,
    events_replayed INTEGER NOT NULL DEFAULT 0,
    communities_seen INTEGER NOT NULL DEFAULT 0,
    edges_decayed INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_observer_runs_written_at
    ON agent_observer_runs(written_at);
ALTER TABLE feedback ADD COLUMN selected_rank INTEGER;
ALTER TABLE feedback ADD COLUMN selected_chunk_id TEXT;
ALTER TABLE feedback ADD COLUMN selected_note_path TEXT;
"""


def _build_legacy_db(path: Path) -> None:
    """A database exactly as the pre-ladder code left it: user_version 0."""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SQL)
    conn.commit()
    conn.close()


def _schema_fingerprint(path: Path) -> set[tuple[str, str]]:
    """(name, normalized sql) for every table and index, for structural equality."""
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    conn.close()
    return {(name, " ".join(sql.split())) for name, sql in rows}


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return cols


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    v = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()
    return v


def test_migrate_fresh_db_reaches_current_schema_version(tmp_path: Path):
    db = tmp_path / "state.db"
    applied = dbmod.migrate(db)

    assert applied == list(range(1, dbmod.SCHEMA_VERSION + 1))
    assert _user_version(db) == dbmod.SCHEMA_VERSION
    assert {"feedback", "agent_link_runs", "agent_observer_runs"} <= {
        name for name, _ in _schema_fingerprint(db)
    }


def test_schema_version_of_a_nonexistent_file_is_zero(tmp_path: Path):
    """`daemon migrate status` must work before the DB has ever been created."""
    assert dbmod.schema_version(tmp_path / "not-yet.db") == 0
    assert not (tmp_path / "not-yet.db").exists()  # and must not create it


def test_schema_version_reads_a_migrated_file(tmp_path: Path):
    db = tmp_path / "state.db"
    dbmod.migrate(db)
    assert dbmod.schema_version(db) == dbmod.SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path: Path):
    db = tmp_path / "state.db"
    dbmod.migrate(db)
    before = _schema_fingerprint(db)

    assert dbmod.migrate(db) == []
    assert _schema_fingerprint(db) == before


def test_legacy_db_converges_to_the_same_structure_as_a_fresh_one(tmp_path: Path):
    """The adoption path: user_version 0 with every table already present."""
    legacy = tmp_path / "legacy.db"
    fresh = tmp_path / "fresh.db"
    _build_legacy_db(legacy)
    assert _user_version(legacy) == 0

    dbmod.migrate(legacy)
    dbmod.migrate(fresh)

    assert _user_version(legacy) == dbmod.SCHEMA_VERSION
    assert _schema_fingerprint(legacy) == _schema_fingerprint(fresh)


def test_legacy_db_keeps_its_rows_through_the_ladder(tmp_path: Path):
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO feedback (timestamp, query, retrieval_summary, answer, latency_ms) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'why', '{}', 'because', 5)"
    )
    conn.commit()
    conn.close()

    dbmod.migrate(db)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT query, answer FROM feedback").fetchone()
    conn.close()
    assert row == ("why", "because")


def test_baseline_adds_selection_columns_to_a_pre_selection_db(tmp_path: Path):
    """A DB predating the selected_* columns still gets them from migration 1."""
    db = tmp_path / "ancient.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        _LEGACY_SQL[: _LEGACY_SQL.index("ALTER TABLE feedback ADD COLUMN selected_rank")]
    )
    conn.commit()
    conn.close()
    assert "selected_rank" not in _columns(db, "feedback")

    dbmod.migrate(db)

    assert {"selected_rank", "selected_chunk_id", "selected_note_path"} <= _columns(db, "feedback")


def test_failed_migration_rolls_back_and_leaves_the_earlier_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def _boom(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_written (x INTEGER)")
        raise RuntimeError("simulated crash")

    ladder = [*dbmod.MIGRATIONS, (dbmod.SCHEMA_VERSION + 1, "boom", _boom)]
    monkeypatch.setattr(dbmod, "MIGRATIONS", ladder)

    db = tmp_path / "state.db"
    with pytest.raises(RuntimeError, match="boom"):
        dbmod.migrate(db, target=dbmod.SCHEMA_VERSION + 1)

    assert _user_version(db) == dbmod.SCHEMA_VERSION
    assert "half_written" not in {name for name, _ in _schema_fingerprint(db)}


def test_open_state_db_sets_wal_busy_timeout_and_foreign_keys(tmp_path: Path):
    db = tmp_path / "state.db"
    conn = dbmod.open_state_db(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        conn.close()


def test_open_state_db_migrates_on_first_open(tmp_path: Path):
    db = tmp_path / "state.db"
    _build_legacy_db(db)

    dbmod.open_state_db(db).close()

    assert _user_version(db) == dbmod.SCHEMA_VERSION


def test_read_only_open_never_migrates(tmp_path: Path):
    """Snapshot bundles are frozen artifacts, possibly on read-only media."""
    db = tmp_path / "frozen.db"
    _build_legacy_db(db)

    conn = dbmod.open_state_db(db, read_only=True, min_version=0)
    try:
        assert _user_version(db) == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope (x INTEGER)")
    finally:
        conn.close()


def test_read_only_open_raises_when_bundle_is_too_old(tmp_path: Path):
    db = tmp_path / "frozen.db"
    _build_legacy_db(db)

    with pytest.raises(dbmod.SnapshotSchemaTooOld):
        dbmod.open_state_db(db, read_only=True, min_version=1)


# ---------------------------------------------------------------------------
# The stores route through the ladder
# ---------------------------------------------------------------------------


def test_feedback_store_migrates_a_legacy_db(tmp_path: Path):
    from my_daemon.stores import FeedbackStore

    db = tmp_path / "feedback.db"
    _build_legacy_db(db)

    FeedbackStore(db_path=db)

    assert _user_version(db) == dbmod.SCHEMA_VERSION


def test_agent_state_store_migrates_a_legacy_db(tmp_path: Path):
    from my_daemon.stores import AgentStateStore

    db = tmp_path / "feedback.db"
    _build_legacy_db(db)

    AgentStateStore(db_path=db)

    assert _user_version(db) == dbmod.SCHEMA_VERSION


def test_feedback_store_leaves_the_file_in_wal_mode(tmp_path: Path):
    """WAL is what stops `daemon consolidate` from blocking the GUI."""
    from my_daemon.stores import FeedbackStore

    db = tmp_path / "feedback.db"
    FeedbackStore(db_path=db)

    conn = sqlite3.connect(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_feedback_store_reads_an_unstamped_snapshot_bundle(tmp_path: Path):
    """Read-only stores ask for what they need, not for the latest schema.

    Snapshot bundles created before the ladder existed sit at user_version 0
    but already carry the `feedback` table, so opening one must still work.
    """
    from my_daemon.stores import FeedbackStore

    db = tmp_path / "bundled.db"
    _build_legacy_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO feedback (timestamp, query, retrieval_summary, answer, latency_ms) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'old question', '{}', 'old answer', 5)"
    )
    conn.commit()
    conn.close()

    store = FeedbackStore(db_path=db, read_only=True)

    assert store.recent()[0]["query"] == "old question"
    assert _user_version(db) == 0  # untouched


# ---------------------------------------------------------------------------
# last_insert_id — the rowid narrowing every store depends on
# ---------------------------------------------------------------------------


def test_last_insert_id_returns_the_rowid_of_an_insert(tmp_path: Path):
    db = tmp_path / "state.db"
    conn = dbmod.open_state_db(db)
    try:
        cur = conn.execute(
            "INSERT INTO feedback (timestamp, query, retrieval_summary, answer, latency_ms) "
            "VALUES ('2026-01-01T00:00:00+00:00', 'q', '{}', 'a', 1)"
        )
        assert dbmod.last_insert_id(cur) == 1
    finally:
        conn.close()


def test_last_insert_id_names_the_problem_when_there_is_no_rowid(tmp_path: Path):
    """`executemany` leaves `lastrowid` None; say so rather than raise from int()."""
    db = tmp_path / "state.db"
    conn = dbmod.open_state_db(db)
    try:
        cur = conn.executemany(
            "INSERT INTO feedback (timestamp, query, retrieval_summary, answer, latency_ms) "
            "VALUES (?, 'q', '{}', 'a', 1)",
            [("2026-01-01T00:00:00+00:00",), ("2026-01-02T00:00:00+00:00",)],
        )
        with pytest.raises(RuntimeError, match="rowid"):
            dbmod.last_insert_id(cur)
    finally:
        conn.close()
