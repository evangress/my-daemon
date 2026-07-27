# SPDX-License-Identifier: Apache-2.0
"""Emergent themes and the tag proposals they generate."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from my_daemon.stores.db import last_insert_id, migrate, open_state_db

_MIN_SCHEMA_VERSION = 5
_DORMANT_AFTER_MISSES = 3


class Theme(BaseModel):
    id: int
    slug: str
    label: str
    summary: str = ""
    centroid: dict[str, float] = Field(default_factory=dict)
    query_count: int = 0
    runs_seen: int = 1
    runs_missing: int = 0
    status: str = "proposed"
    label_locked: bool = False


class TagProposal(BaseModel):
    id: int
    theme_id: int
    note_uuid: str
    tag: str
    decision: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ThemeStore:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            migrate(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return open_state_db(
            self.db_path, read_only=self.read_only, min_version=_MIN_SCHEMA_VERSION
        )

    # ---- themes -----------------------------------------------------------

    def create(
        self,
        *,
        slug: str,
        label: str,
        centroid: dict[str, float],
        snapshot_id: str | None = None,
        summary: str = "",
        query_count: int = 0,
    ) -> int:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO themes (slug, label, summary, centroid_json, query_count, "
                "first_seen_at, last_seen_at, snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, label, summary, json.dumps(centroid), query_count, now, now, snapshot_id),
            )
            return last_insert_id(cur)

    def set_label(self, theme_id: int, *, label: str, summary: str, locked: bool = False) -> None:
        """Rename a theme — unless the user has locked it.

        Once accepted, a label is the user's. The observer may keep finding the
        cluster, but it never gets to rename what you named.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT label_locked FROM themes WHERE id = ?", (theme_id,)
            ).fetchone()
            if row is None or row["label_locked"]:
                return
            conn.execute(
                "UPDATE themes SET label = ?, summary = ?, label_locked = ? WHERE id = ?",
                (label, summary, int(locked), theme_id),
            )

    def touch(
        self,
        theme_id: int,
        *,
        centroid: dict[str, float],
        query_count: int,
        snapshot_id: str | None = None,
    ) -> None:
        """A cluster matched this run. Blend the centroid rather than replacing
        it — a single run's shift should nudge a theme, not redefine it."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT centroid_json FROM themes WHERE id = ?", (theme_id,)
            ).fetchone()
            blended = _blend(json.loads(row["centroid_json"]), centroid) if row else centroid
            conn.execute(
                "UPDATE themes SET centroid_json = ?, query_count = ?, last_seen_at = ?, "
                "runs_seen = runs_seen + 1, runs_missing = 0, snapshot_id = ?, "
                "status = CASE WHEN status = 'dormant' THEN 'proposed' ELSE status END "
                "WHERE id = ?",
                (json.dumps(blended), query_count, _now(), snapshot_id, theme_id),
            )

    def mark_missing(self, theme_id: int) -> None:
        """Never deleted — a theme that flickers should not vanish from your history."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE themes SET runs_missing = runs_missing + 1, "
                "status = CASE WHEN runs_missing + 1 >= ? AND status != 'accepted' "
                "             THEN 'dormant' ELSE status END "
                "WHERE id = ?",
                (_DORMANT_AFTER_MISSES, theme_id),
            )

    def get(self, theme_id: int) -> Theme | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM themes WHERE id = ?", (theme_id,)).fetchone()
        return _to_theme(row) if row else None

    def all(self, *, include_dormant: bool = True) -> list[Theme]:
        sql = "SELECT * FROM themes"
        if not include_dormant:
            sql += " WHERE status != 'dormant'"
        with self._connect() as conn:
            return [_to_theme(r) for r in conn.execute(sql + " ORDER BY id").fetchall()]

    def set_status(self, theme_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE themes SET status = ? WHERE id = ?", (status, theme_id))

    def set_notes(self, theme_id: int, notes: dict[str, float]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM theme_notes WHERE theme_id = ?", (theme_id,))
            conn.executemany(
                "INSERT INTO theme_notes (theme_id, note_uuid, weight) VALUES (?, ?, ?)",
                [(theme_id, u, w) for u, w in notes.items()],
            )

    def notes_for(self, theme_id: int) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT note_uuid, weight FROM theme_notes WHERE theme_id = ? ORDER BY weight DESC",
                (theme_id,),
            ).fetchall()
        return {r["note_uuid"]: float(r["weight"]) for r in rows}

    # ---- tag proposals ----------------------------------------------------

    def propose_tag(self, theme_id: int, note_uuid: str, tag: str) -> None:
        """Idempotent, and never re-proposes something already decided."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO theme_tag_proposals "
                "(theme_id, note_uuid, tag, proposed_at) VALUES (?, ?, ?, ?)",
                (theme_id, note_uuid, tag, _now()),
            )

    def pending_proposals(self, *, limit: int = 100) -> list[TagProposal]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM theme_tag_proposals WHERE decided_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            TagProposal(id=r["id"], theme_id=r["theme_id"], note_uuid=r["note_uuid"], tag=r["tag"])
            for r in rows
        ]

    def decide(self, proposal_id: int, decision: str, write_result: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE theme_tag_proposals SET decision = ?, decided_at = ?, "
                "write_result = ? WHERE id = ?",
                (decision, _now(), write_result, proposal_id),
            )


def _blend(old: dict[str, float], new: dict[str, float], alpha: float = 0.3) -> dict[str, float]:
    keys = set(old) | set(new)
    out = {k: (1 - alpha) * old.get(k, 0.0) + alpha * new.get(k, 0.0) for k in keys}
    magnitude = sum(v * v for v in out.values()) ** 0.5
    return {k: v / magnitude for k, v in out.items()} if magnitude else out


def _to_theme(row: sqlite3.Row) -> Theme:
    return Theme(
        id=row["id"],
        slug=row["slug"],
        label=row["label"],
        summary=row["summary"],
        centroid=json.loads(row["centroid_json"]),
        query_count=row["query_count"],
        runs_seen=row["runs_seen"],
        runs_missing=row["runs_missing"],
        status=row["status"],
        label_locked=bool(row["label_locked"]),
    )
