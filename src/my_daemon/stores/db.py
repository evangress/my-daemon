# SPDX-License-Identifier: Apache-2.0
"""One connection factory and one schema-version ladder for the state DB.

Every store that touches ``data/feedback.db`` goes through :func:`open_state_db`.
That file holds the feedback log, the background-agent bookkeeping, and (from
migration 2 onward) the note registry and activation ledger — one state file to
back up or wipe.

**Adoption.** Databases created before this module existed have
``user_version == 0`` but already contain every table, because the old code ran
``CREATE TABLE IF NOT EXISTS`` at store construction. Migration 1 is therefore
*exactly* that old code, and stamps ``user_version = 1``. A fresh database and a
months-old one converge on identical structure. Nothing below migration 1 may
assume an empty file.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

Migration = Callable[[sqlite3.Connection], None]

_BUSY_TIMEOUT_MS = 5000


class SnapshotSchemaTooOld(RuntimeError):
    """A read-only bundle predates the schema a caller needs.

    Raised instead of migrating, because snapshot bundles are frozen artifacts
    and may sit on read-only media. Callers are expected to degrade — skip the
    analysis that needs the newer tables and carry on — rather than crash.
    """


# ---------------------------------------------------------------------------
# Migration 1 — the baseline. Byte-for-byte what the pre-ladder code produced.
# ---------------------------------------------------------------------------

_M001_SCHEMA = """
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
"""

# Added to `feedback` after its initial release, when candidate-selection
# feedback shipped (M1). Applied by introspection because SQLite has no
# `ADD COLUMN IF NOT EXISTS`.
_M001_FEEDBACK_COLUMNS = {
    "selected_rank": "INTEGER",
    "selected_chunk_id": "TEXT",
    "selected_note_path": "TEXT",
}


def exec_script(conn: sqlite3.Connection, sql: str) -> None:
    """Run a multi-statement DDL script *without* breaking the caller's transaction.

    ``sqlite3.Cursor.executescript`` issues an implicit ``COMMIT`` before it
    runs, which would silently end the ``BEGIN IMMEDIATE`` that :func:`migrate`
    opened and leave each statement auto-committed instead. Splitting on
    semicolons and executing statements individually keeps the migration atomic.

    Limitation: this cannot handle bodies containing semicolons — triggers, or
    ``BEGIN ... END`` blocks. A migration needing those must issue its own
    ``conn.execute`` calls.
    """

    for statement in (s.strip() for s in sql.split(";")):
        if statement:
            conn.execute(statement)


def _m001_baseline(conn: sqlite3.Connection) -> None:
    exec_script(conn, _M001_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(feedback)")}
    for col, typ in _M001_FEEDBACK_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {typ}")


# ---------------------------------------------------------------------------
# Migration 2 — the note registry. SQLite becomes the identity spine: Qdrant
# owns semantics, networkx owns relations, this owns *who a note is*.
# ---------------------------------------------------------------------------

_M002_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    uuid                TEXT PRIMARY KEY,
    rel_path            TEXT NOT NULL,
    title               TEXT NOT NULL DEFAULT '',
    mtime               TEXT,
    body_sha256         TEXT,
    frontmatter_sha256  TEXT,
    tags_json           TEXT NOT NULL DEFAULT '[]',
    word_count          INTEGER NOT NULL DEFAULT 0,
    chunk_count         INTEGER NOT NULL DEFAULT 0,
    uuid_source         TEXT NOT NULL DEFAULT 'assigned',
    in_frontmatter      INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'active',
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    deleted_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_rel_path_live
    ON notes(rel_path) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
CREATE INDEX IF NOT EXISTS idx_notes_last_seen ON notes(last_seen_at);
CREATE TABLE IF NOT EXISTS note_ordinals (
    note_uuid     TEXT PRIMARY KEY,
    ordinal       INTEGER NOT NULL UNIQUE,
    allocated_at  TEXT NOT NULL
);
"""


def _m002_registry(conn: sqlite3.Connection) -> None:
    exec_script(conn, _M002_SCHEMA)


# ---------------------------------------------------------------------------
# Migration 3 — identity on the feedback log. `selected_note_path` stays for
# human-readable reports; the uuid is what weight replay joins on.
# ---------------------------------------------------------------------------

_M003_COLUMNS = {"selected_note_uuid": "TEXT"}


def _m003_feedback_identity(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(feedback)")}
    for col, typ in _M003_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {typ}")


# ---------------------------------------------------------------------------
# Migration 4 — the activation ledger. Which notes fired for which query, via
# which source, how strongly. The substrate for query fingerprints.
# ---------------------------------------------------------------------------

_M004_SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query_uid        TEXT NOT NULL UNIQUE,
    ts               TEXT NOT NULL,
    text             TEXT NOT NULL,
    text_sha256      TEXT NOT NULL,
    surface          TEXT NOT NULL,
    session_id       TEXT,
    origin           TEXT NOT NULL DEFAULT 'live',
    feedback_id      INTEGER REFERENCES feedback(id) ON DELETE SET NULL,
    seed_count       INTEGER NOT NULL DEFAULT 0,
    expanded_count   INTEGER NOT NULL DEFAULT 0,
    activation_count INTEGER NOT NULL DEFAULT 0,
    l2_norm          REAL    NOT NULL DEFAULT 0.0,
    fingerprint_json TEXT,
    latency_ms       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts);
CREATE INDEX IF NOT EXISTS idx_queries_sha ON queries(text_sha256);
CREATE INDEX IF NOT EXISTS idx_queries_surface_ts ON queries(surface, ts);
CREATE TABLE IF NOT EXISTS query_activations (
    query_id       INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    note_uuid      TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    strength       REAL    NOT NULL,
    raw_score      REAL,
    rank           INTEGER,
    chunk_id       TEXT,
    graph_distance REAL,
    seed_note_uuid TEXT,
    PRIMARY KEY (query_id, note_uuid, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_qa_note_query ON query_activations(note_uuid, query_id);
CREATE TABLE IF NOT EXISTS note_activation_stats (
    note_uuid         TEXT PRIMARY KEY,
    query_count       INTEGER NOT NULL DEFAULT 0,
    last_activated_at TEXT,
    total_strength    REAL NOT NULL DEFAULT 0.0
);
"""


def _m004_activation_ledger(conn: sqlite3.Connection) -> None:
    exec_script(conn, _M004_SCHEMA)


MIGRATIONS: list[tuple[int, str, Migration]] = [
    (1, "baseline_feedback_and_agent_state", _m001_baseline),
    (2, "note_registry_and_ordinals", _m002_registry),
    (3, "feedback_note_identity", _m003_feedback_identity),
    (4, "activation_ledger", _m004_activation_ledger),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


# ---------------------------------------------------------------------------
# Running the ladder
# ---------------------------------------------------------------------------


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def schema_version(db_path: Path) -> int:
    """Schema version of a database file, without creating or migrating it.

    A file that does not exist yet reports 0 — the same as an unstamped
    pre-ladder database — so status commands work before first run.
    """

    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return current_version(conn)
    finally:
        conn.close()


def _apply_pragmas(conn: sqlite3.Connection, *, writable: bool) -> None:
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if writable:
        # Persistent per-file, but harmless to re-assert. Without it a
        # `daemon consolidate` holding a write lock blocks the GUI outright.
        conn.execute("PRAGMA journal_mode = WAL")


def migrate(db_path: Path, *, target: int = SCHEMA_VERSION) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied.

    Each step runs in its own ``BEGIN IMMEDIATE`` transaction that also bumps
    ``user_version``, so a crash part-way up the ladder leaves the database at
    a consistent earlier version rather than half-migrated.
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[int] = []
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit txn control
    try:
        _apply_pragmas(conn, writable=True)
        for version, name, fn in MIGRATIONS:
            if version > target or version <= current_version(conn):
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn)
                # Pragmas cannot be parameterized; int() is the injection guard.
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.execute("COMMIT")
            except Exception as exc:
                # If the migration broke the transaction (see exec_script), the
                # ROLLBACK itself fails. Never let that mask the real cause.
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise RuntimeError(f"migration {version} ({name}) failed") from exc
            applied.append(version)
    finally:
        conn.close()
    return applied


def open_state_db(
    db_path: Path,
    *,
    read_only: bool = False,
    min_version: int = SCHEMA_VERSION,
) -> sqlite3.Connection:
    """The single entry point every store uses.

    Brings the file up to ``SCHEMA_VERSION`` if it is behind, then hands back a
    connection with the pragmas this codebase depends on. In ``read_only`` mode
    (snapshot bundles) it never migrates — it asserts the file is new enough and
    raises :class:`SnapshotSchemaTooOld` if not, so callers can degrade.
    """

    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn, writable=False)
        found = current_version(conn)
        if found < min_version:
            conn.close()
            raise SnapshotSchemaTooOld(
                f"{db_path} is at schema v{found}, need v{min_version} or newer"
            )
        return conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, writable=True)
    if current_version(conn) < SCHEMA_VERSION:
        # Rare — once per database, ever. Reconnecting afterwards is cheaper
        # than threading explicit transaction control through every caller.
        conn.close()
        migrate(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn, writable=True)
    return conn
