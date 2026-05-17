"""SQLite-backed feedback log. v0.1 records; phase 4 will apply the signals."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from my_daemon.models import FeedbackEvent, FeedbackSignal

_SCHEMA = """
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
"""

# Columns added after the initial schema. SQLite's `ADD COLUMN` is idempotent
# only via explicit existence checks, so we introspect PRAGMA and add what's
# missing — keeps upgrades safe for existing data/feedback.db files.
_SELECTION_COLUMNS = {
    "selected_rank": "INTEGER",
    "selected_chunk_id": "TEXT",
    "selected_note_path": "TEXT",
}


class FeedbackStore:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(feedback)")}
            for col, typ in _SELECTION_COLUMNS.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {typ}")

    def log(self, event: FeedbackEvent) -> int:
        if self.read_only:
            raise RuntimeError("FeedbackStore is read-only")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO feedback (timestamp, query, retrieval_summary, answer, latency_ms, signal, signal_captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.timestamp.isoformat(),
                    event.query,
                    json.dumps(event.retrieval_summary),
                    event.answer,
                    event.latency_ms,
                    event.signal,
                    event.signal_captured_at.isoformat() if event.signal_captured_at else None,
                ),
            )
            return int(cur.lastrowid)

    def attach_signal(
        self,
        event_id: int,
        signal: FeedbackSignal,
        *,
        selected_rank: int | None = None,
        selected_chunk_id: str | None = None,
        selected_note_path: str | None = None,
    ) -> None:
        if self.read_only:
            raise RuntimeError("FeedbackStore is read-only")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE feedback
                SET signal = ?,
                    signal_captured_at = ?,
                    selected_rank = ?,
                    selected_chunk_id = ?,
                    selected_note_path = ?
                WHERE id = ?
                """,
                (
                    signal,
                    datetime.now(UTC).isoformat(),
                    selected_rank,
                    selected_chunk_id,
                    selected_note_path,
                    event_id,
                ),
            )

    def get(self, event_id: int) -> FeedbackEvent | None:
        """Return one event by id, or None if it's been pruned away."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE id = ?", (event_id,)
            ).fetchone()
            return _row_to_event(row) if row else None

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]


def _row_to_event(row: sqlite3.Row) -> FeedbackEvent:
    raw = dict(row)
    return FeedbackEvent(
        id=raw["id"],
        timestamp=datetime.fromisoformat(raw["timestamp"]),
        query=raw["query"],
        retrieval_summary=json.loads(raw["retrieval_summary"]) if raw["retrieval_summary"] else {},
        answer=raw["answer"] or "",
        latency_ms=int(raw["latency_ms"]),
        signal=raw["signal"],
        signal_captured_at=(
            datetime.fromisoformat(raw["signal_captured_at"]) if raw["signal_captured_at"] else None
        ),
        selected_rank=raw.get("selected_rank"),
        selected_chunk_id=raw.get("selected_chunk_id"),
        selected_note_path=raw.get("selected_note_path"),
    )
