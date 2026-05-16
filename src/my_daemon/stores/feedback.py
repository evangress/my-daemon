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


class FeedbackStore:
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

    def log(self, event: FeedbackEvent) -> int:
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

    def attach_signal(self, event_id: int, signal: FeedbackSignal) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE feedback SET signal = ?, signal_captured_at = ? WHERE id = ?",
                (signal, datetime.now(UTC).isoformat(), event_id),
            )

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
