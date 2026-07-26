# SPDX-License-Identifier: Apache-2.0
"""The note registry — SQLite as the identity spine.

Answers three questions the rest of the system keeps asking:

* what is this uuid's current path? (reports render paths, never raw uuids)
* what lives at this path? (rename and duplicate detection during ingest)
* how much of the vault is still on a fragile path-derived identity?

Derived state throughout. Frontmatter is the source of truth; when the two
disagree the frontmatter wins and this is corrected.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from my_daemon.models import NoteRecord, RegistryCoverage
from my_daemon.stores.db import migrate, open_state_db

_MIN_SCHEMA_VERSION = 2

_COLUMNS = (
    "uuid, rel_path, title, mtime, body_sha256, frontmatter_sha256, tags_json, "
    "word_count, chunk_count, uuid_source, in_frontmatter, status, "
    "first_seen_at, last_seen_at, deleted_at"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _to_record(row: sqlite3.Row) -> NoteRecord:
    return NoteRecord(
        uuid=row["uuid"],
        rel_path=row["rel_path"],
        title=row["title"],
        mtime=_dt(row["mtime"]),
        body_sha256=row["body_sha256"],
        frontmatter_sha256=row["frontmatter_sha256"],
        tags=json.loads(row["tags_json"]),
        word_count=row["word_count"],
        chunk_count=row["chunk_count"],
        uuid_source=row["uuid_source"],
        in_frontmatter=bool(row["in_frontmatter"]),
        status=row["status"],
        first_seen_at=_dt(row["first_seen_at"]),
        last_seen_at=_dt(row["last_seen_at"]),
        deleted_at=_dt(row["deleted_at"]),
    )


class NoteRegistry:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            migrate(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return open_state_db(
            self.db_path, read_only=self.read_only, min_version=_MIN_SCHEMA_VERSION
        )

    # ---- writes -----------------------------------------------------------

    def upsert(self, record: NoteRecord) -> None:
        """Insert or refresh a note. ``first_seen_at`` survives; revival clears
        the tombstone, so a note that comes back keeps its whole history."""

        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (
                    uuid, rel_path, title, mtime, body_sha256, frontmatter_sha256,
                    tags_json, word_count, chunk_count, uuid_source, in_frontmatter,
                    status, first_seen_at, last_seen_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(uuid) DO UPDATE SET
                    rel_path = excluded.rel_path,
                    title = excluded.title,
                    mtime = excluded.mtime,
                    body_sha256 = excluded.body_sha256,
                    frontmatter_sha256 = excluded.frontmatter_sha256,
                    tags_json = excluded.tags_json,
                    word_count = excluded.word_count,
                    chunk_count = excluded.chunk_count,
                    uuid_source = excluded.uuid_source,
                    in_frontmatter = excluded.in_frontmatter,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    deleted_at = NULL
                """,
                (
                    record.uuid,
                    record.rel_path,
                    record.title,
                    record.mtime.isoformat() if record.mtime else None,
                    record.body_sha256,
                    record.frontmatter_sha256,
                    json.dumps(record.tags),
                    record.word_count,
                    record.chunk_count,
                    record.uuid_source,
                    int(record.in_frontmatter),
                    record.status,
                    now,
                    now,
                ),
            )

    def soft_delete(self, note_uuid: str) -> None:
        """Tombstone a note. The row stays — the activation ledger is history
        and must outlive the note it refers to."""

        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET deleted_at = ?, status = 'missing' WHERE uuid = ?",
                (_now(), note_uuid),
            )

    def forget(self, note_uuid: str) -> None:
        """Hard-delete a registry row. Only the migration rollback does this —
        it returns the system to path-only operation, so a tombstone would be
        wrong. Ordinals are deliberately *not* freed; they are never reused."""

        with self._connect() as conn:
            conn.execute("DELETE FROM notes WHERE uuid = ?", (note_uuid,))

    # ---- reads ------------------------------------------------------------

    def get(self, note_uuid: str) -> NoteRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM notes WHERE uuid = ?", (note_uuid,)
            ).fetchone()
        return _to_record(row) if row else None

    def by_path(self, rel_path: str) -> NoteRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM notes "
                "WHERE rel_path = ? AND deleted_at IS NULL",
                (rel_path,),
            ).fetchone()
        return _to_record(row) if row else None

    def live(self) -> list[NoteRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM notes WHERE deleted_at IS NULL "
                "ORDER BY rel_path"
            ).fetchall()
        return [_to_record(r) for r in rows]

    def paths_for(self, uuids: Iterable[str]) -> dict[str, str]:
        """Bulk uuid → rel_path. Unknown uuids are simply absent from the result."""

        wanted = list(dict.fromkeys(uuids))
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT uuid, rel_path FROM notes WHERE uuid IN ({placeholders})",
                wanted,
            ).fetchall()
        return {r["uuid"]: r["rel_path"] for r in rows}

    def coverage(self) -> RegistryCoverage:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT uuid_source, in_frontmatter, status FROM notes "
                "WHERE deleted_at IS NULL"
            ).fetchall()

        coverage = RegistryCoverage(total=len(rows))
        for row in rows:
            if row["in_frontmatter"]:
                coverage.in_frontmatter += 1
            if row["uuid_source"] == "derived_path":
                coverage.derived_path += 1
            status = row["status"]
            coverage.by_status[status] = coverage.by_status.get(status, 0) + 1
        return coverage

    # ---- ordinals ---------------------------------------------------------

    def ordinal_for(self, note_uuid: str) -> int:
        """A stable small int for this note, for sparse-vector indices.

        Allocated monotonically and **never reused** — a deleted note's ordinal
        stays permanently bound to it, so historical fingerprints referring to
        it keep their meaning.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT ordinal FROM note_ordinals WHERE note_uuid = ?", (note_uuid,)
            ).fetchone()
            if row:
                return int(row["ordinal"])

            # BEGIN IMMEDIATE + UNIQUE(ordinal) makes the loser of a race fail
            # cleanly rather than silently duplicating; one retry settles it.
            for _attempt in range(2):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO note_ordinals (note_uuid, ordinal, allocated_at) "
                        "VALUES (?, (SELECT COALESCE(MAX(ordinal), 0) + 1 "
                        "FROM note_ordinals), ?)",
                        (note_uuid, _now()),
                    )
                    conn.execute("COMMIT")
                    break
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
            row = conn.execute(
                "SELECT ordinal FROM note_ordinals WHERE note_uuid = ?", (note_uuid,)
            ).fetchone()
            return int(row["ordinal"])
