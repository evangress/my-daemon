# SPDX-License-Identifier: Apache-2.0
"""SQLite-backed processed-state tables for the three background-agent jobs.

Lives in the same DB as `FeedbackStore` (``data/feedback.db``) so users have
one state file to back up / wipe. Schemas are additive (``CREATE TABLE IF NOT
EXISTS``) — no migration needed when this module first runs against an
existing feedback DB.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AgentStateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---- link runs --------------------------------------------------------

    def record_link_run(
        self,
        note_path: str,
        *,
        note_mtime: float,
        applied: int,
        suggested: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_link_runs (note_path, last_run_at, note_mtime_seen, applied_count, suggested_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(note_path) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    note_mtime_seen = excluded.note_mtime_seen,
                    applied_count = excluded.applied_count,
                    suggested_count = excluded.suggested_count
                """,
                (note_path, _now_iso(), note_mtime, applied, suggested),
            )

    def link_run_for(self, note_path: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_link_runs WHERE note_path = ?", (note_path,)
            ).fetchone()
            return dict(row) if row else None

    # ---- extract runs -----------------------------------------------------

    def record_extract_run(
        self, note_path: str, *, note_mtime: float, summary_hash: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_extract_runs (note_path, last_run_at, note_mtime_seen, summary_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(note_path) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    note_mtime_seen = excluded.note_mtime_seen,
                    summary_hash = excluded.summary_hash
                """,
                (note_path, _now_iso(), note_mtime, summary_hash),
            )

    def extract_run_for(self, note_path: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_extract_runs WHERE note_path = ?", (note_path,)
            ).fetchone()
            return dict(row) if row else None

    # ---- reflect runs -----------------------------------------------------

    def record_reflect_run(
        self, theme: str, *, source_notes_hash: str, source_chat_count: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_reflect_runs (theme, last_run_at, source_notes_hash, source_chat_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(theme) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    source_notes_hash = excluded.source_notes_hash,
                    source_chat_count = excluded.source_chat_count
                """,
                (theme, _now_iso(), source_notes_hash, source_chat_count),
            )

    def reflect_run_for(self, theme: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_reflect_runs WHERE theme = ?", (theme,)
            ).fetchone()
            return dict(row) if row else None

    # ---- observer runs ----------------------------------------------------

    def record_observer_run(
        self,
        *,
        snapshot_id: str,
        letter_path: str | None,
        model: str,
        events_replayed: int = 0,
        communities_seen: int = 0,
        edges_decayed: int = 0,
        dry_run: bool = False,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_observer_runs (
                    snapshot_id, letter_path, written_at, model,
                    events_replayed, communities_seen, edges_decayed, dry_run
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    letter_path,
                    _now_iso(),
                    model,
                    events_replayed,
                    communities_seen,
                    edges_decayed,
                    1 if dry_run else 0,
                ),
            )
            return int(cur.lastrowid)

    def recent_observer_runs(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_observer_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
