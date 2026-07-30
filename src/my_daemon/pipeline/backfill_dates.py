# SPDX-License-Identifier: Apache-2.0
"""One-time backfill: populate ``occurred_at`` on chunks ingested before it existed.

``daemon ingest`` skips a note whose ``body_sha256`` is unchanged (see the
skip site in ``pipeline/ingest.py``), so a metadata-derived field added after
a vault was already ingested never reaches those chunks just by re-running
ingest — the body hash comparison has no way to see that a *new payload key*
now exists. This module derives the date exactly as ingest does and writes it
straight onto the existing vector payload and registry row, without touching
a single vector: chunk ids fold in the text, not the date, so this is a
payload correction, never a re-embed.

Only notes that already have a registry record get a vector-store write — a
note with no record has never been ingested and so has no vector points to
correct; a plain ``daemon ingest`` will derive and store its date the first
time it is embedded.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from my_daemon.config import Settings
from my_daemon.stores.registry import NoteRegistry
from my_daemon.stores.vector import VectorStore
from my_daemon.vault import VaultReader
from my_daemon.vault.identity import effective_uuid

# `set_occurred_at` only ever calls `set_payload` / `delete_payload` on an
# *existing* collection; this module never calls `ensure_collection`, so this
# value is never consulted to create one. Kept nameable rather than a bare `1`
# so a future reader isn't left wondering whether it matters.
_INERT_DIM = 1


@dataclass
class BackfillReport:
    dry_run: bool = False
    scanned: int = 0
    frontmatter: int = 0
    filename: int = 0
    mtime: int = 0
    undated: int = 0
    # Notes that already had a registry record (i.e. a prior ingest) and so
    # actually received a vector-store + registry write.
    updated: int = 0
    import_clusters: dict[str, int] = field(default_factory=dict)


def _import_clusters(paths: list[Path]) -> dict[str, int]:
    """Birth-time histogram, best-effort — a diagnostic, never a dependency.

    ``stat -c %w`` is GNU-coreutils-only, and even there most filesystems
    (ext4 without birth-time support, network mounts) answer ``-``. Any
    failure — missing binary, unsupported field, a path that vanished
    mid-scan — is swallowed per-file rather than aborting the whole backfill.
    Only dates shared by more than one note are reported; a lone birth time
    tells the operator nothing about a bulk import.
    """

    counts: Counter[str] = Counter()
    for path in paths:
        try:
            result = subprocess.run(
                ["stat", "-c", "%w", str(path)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        raw = result.stdout.strip()
        if not raw or raw == "-":
            continue  # filesystem doesn't support birth time
        day = raw.split(" ", 1)[0]
        if day:
            counts[day] += 1
    return {day: n for day, n in counts.items() if n > 1}


def backfill_dates(settings: Settings, *, dry_run: bool = False) -> BackfillReport:
    """Derive ``occurred_at`` for every note and refresh already-ingested points.

    ``--dry-run`` writes nothing at all — not to Qdrant, not to the registry —
    and the report is exactly the counts a real run would produce. Idempotent:
    the date is re-derived (not read back), so running this twice reports the
    same breakdown and changes nothing on the second pass.
    """

    reader = VaultReader(
        settings.vault.path,
        exclude_dirs=settings.vault.exclude_dirs,
        date_keys=settings.vault.date_frontmatter_keys,
        mtime_trusted_before=settings.vault.mtime_trusted_before,
    )
    registry = NoteRegistry(db_path=settings.feedback.db_path)
    # Never actually connects unless some note turns out to already be
    # registered — a vault that has never been ingested triggers no Qdrant
    # access at all, embedded lock included.
    vector_store = VectorStore.from_config(
        settings.vector_store.qdrant, dim=_INERT_DIM, hybrid=settings.embeddings.hybrid
    )

    report = BackfillReport(dry_run=dry_run)
    paths: list[Path] = []
    try:
        for note in reader.read_all():
            report.scanned += 1
            paths.append(note.path)

            source = note.occurred_at_source
            if source == "frontmatter":
                report.frontmatter += 1
            elif source == "filename":
                report.filename += 1
            elif source == "mtime":
                report.mtime += 1
            else:
                report.undated += 1

            if dry_run:
                continue

            note_uuid = effective_uuid(note)
            record = registry.get(note_uuid)
            if record is None:
                continue

            vector_store.set_occurred_at(
                note_uuid, note.occurred_at, note.occurred_at_source, note.mtime
            )
            record.occurred_at = note.occurred_at
            record.occurred_at_source = note.occurred_at_source
            registry.upsert(record)
            report.updated += 1
    finally:
        vector_store.close()

    report.import_clusters = _import_clusters(paths)
    return report
