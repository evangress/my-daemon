# SPDX-License-Identifier: Apache-2.0
"""SQLite-backed feedback log — the answer + user-signal record for a query.

The schema lives in :mod:`my_daemon.stores.db`, which owns the version ladder
for this file. This store only reads and writes rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from my_daemon.models import FeedbackEvent, FeedbackSignal
from my_daemon.stores.db import last_insert_id, migrate, open_state_db

# The `feedback` table exists from the baseline migration onward, and also in
# every pre-ladder database (which sits at user_version 0). Asking for 0 is what
# lets snapshot bundles taken before the ladder existed still be read back.
_MIN_SCHEMA_VERSION = 0


class FeedbackStore:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            migrate(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return open_state_db(
            self.db_path,
            read_only=self.read_only,
            min_version=_MIN_SCHEMA_VERSION,
        )

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
            return last_insert_id(cur)

    def attach_signal(
        self,
        event_id: int,
        signal: FeedbackSignal,
        *,
        selected_rank: int | None = None,
        selected_chunk_id: str | None = None,
        selected_note_path: str | None = None,
        selected_note_uuid: str | None = None,
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
                    selected_note_path = ?,
                    selected_note_uuid = ?
                WHERE id = ?
                """,
                (
                    signal,
                    datetime.now(UTC).isoformat(),
                    selected_rank,
                    selected_chunk_id,
                    selected_note_path,
                    selected_note_uuid,
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

    def selections_since(
        self,
        cutoff: datetime,
        *,
        limit: int = 5000,
    ) -> list[FeedbackEvent]:
        """All ``candidate_selected`` events at/after ``cutoff``, oldest first.

        Powers the hypothetical weight-evolution replay in the M3 analyzer:
        we walk these events through ``weights.apply_selection`` on a
        deep-copy of the snapshot graph to see what *would* shift if we'd
        decayed and re-applied. Live state is never touched.
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM feedback
                WHERE signal = 'candidate_selected'
                  AND timestamp >= ?
                ORDER BY timestamp ASC, id ASC
                LIMIT ?
                """,
                (cutoff.isoformat(), limit),
            ).fetchall()
            return [_row_to_event(r) for r in rows]


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
        selected_note_uuid=raw.get("selected_note_uuid"),
    )
