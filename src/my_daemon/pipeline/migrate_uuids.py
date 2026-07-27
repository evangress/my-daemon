# SPDX-License-Identifier: Apache-2.0
"""Stamp a stable UUID into every note's frontmatter.

The one command in this project that writes to *every* file in the user's
vault, so it is built around three commitments:

* **Dry-run by default.** ``--apply`` is deliberate.
* **Batch backup.** One directory per run holding a manifest and byte-for-byte
  copies, rather than thousands of loose per-file snapshots.
* **Reversible.** ``key-removal`` rollback deletes the one line we added and
  never restores a body, so it stays safe on files edited since.

Only one line per file changes — see ``vault.writer.set_frontmatter_key_textual``
for why a ``frontmatter.dumps`` round-trip could not be used.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from my_daemon.config import Settings
from my_daemon.models import NoteRecord
from my_daemon.stores.registry import NoteRegistry
from my_daemon.vault import VaultReader
from my_daemon.vault.identity import OUR_KEY, derive_path_uuid
from my_daemon.vault.parser import parse_note
from my_daemon.vault.writer import (
    remove_frontmatter_key_textual,
    set_frontmatter_key_textual,
)

_MIGRATIONS_DIRNAME = "migrations"


@dataclass
class UuidAssignment:
    rel_path: str
    uuid: str
    action: str  # assigned | adopted | stamped | reassigned_duplicate | fallback
    uuid_source: str
    written: bool = False
    reason: str = ""


@dataclass
class UuidMigrationReport:
    dry_run: bool
    scanned: int = 0
    written: int = 0
    would_write: int = 0
    adopted: int = 0
    fallback: int = 0
    run_id: str | None = None
    backup_dir: Path | None = None
    entries: list[UuidAssignment] = field(default_factory=list)


@dataclass
class RollbackReport:
    run_id: str
    mode: str
    reverted: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migrations_root(settings: Settings) -> Path:
    return (
        settings.vault.path.expanduser().resolve()
        / settings.agent.folder_name
        / "backups"
        / _MIGRATIONS_DIRNAME
    )


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def assign_uuids(
    settings: Settings,
    *,
    apply: bool = False,
    path_glob: str | None = None,
    limit: int | None = None,
    grace_minutes: int = 2,
    allow_agent_folder: bool = True,
    backup: bool = True,
) -> UuidMigrationReport:
    """Assign a UUID to every note that lacks one. Dry-run unless ``apply``."""

    vault_root = settings.vault.path.expanduser().resolve()
    reader = VaultReader(vault_root, exclude_dirs=settings.vault.exclude_dirs)

    # Belt and braces. `exclude_dirs` normally keeps the whole Agent folder out
    # of discovery, but a user who removes it from the config must still never
    # have the migration stamp its own backups — they hold duplicate uuids and
    # would trigger the collision path against the originals.
    migrations_root = _migrations_root(settings)
    paths = [p for p in sorted(reader.discover()) if not p.is_relative_to(migrations_root)]

    if path_glob:
        paths = [
            p for p in paths if fnmatch.fnmatch(p.relative_to(vault_root).as_posix(), path_glob)
        ]

    report = UuidMigrationReport(dry_run=not apply)
    registry = NoteRegistry(db_path=settings.feedback.db_path) if apply else None

    run_id = _new_run_id() if apply else None
    backup_dir = None
    # `run_id is not None` is exactly `apply` — spelled this way so the id is
    # provably present where it is joined onto a path.
    if run_id is not None and backup:
        backup_dir = _migrations_root(settings) / run_id
        (backup_dir / "files").mkdir(parents=True, exist_ok=True)
    report.run_id = run_id
    report.backup_dir = backup_dir

    manifest_lines: list[str] = []
    claimed: dict[str, str] = {}  # uuid -> rel_path, for in-run collision detection

    for path in paths:
        if limit is not None and report.scanned >= limit:
            break
        rel_path = path.relative_to(vault_root).as_posix()
        report.scanned += 1

        note = parse_note(path, vault_root)
        resolved = note.uuid
        source = note.uuid_source or "assigned"

        if resolved is None:
            resolved, source = str(uuidlib.uuid4()), "assigned"
            action = "assigned"
        elif source == f"adopted:{OUR_KEY}":
            action = "adopted"
        else:
            # A foreign key carried the identity. Stamp it under our key too,
            # and never touch theirs.
            action = "stamped"

        if resolved in claimed and claimed[resolved] != rel_path:
            # The Obsidian "Make a copy" / sync-conflict path. It will happen.
            resolved, source = str(uuidlib.uuid4()), "assigned"
            action = "reassigned_duplicate"
        claimed[resolved] = rel_path

        entry = UuidAssignment(rel_path=rel_path, uuid=resolved, action=action, uuid_source=source)

        if action == "adopted":
            # Already carries our key with this value — nothing to write.
            report.adopted += 1
        elif not apply:
            report.would_write += 1
        else:
            sha_before = _sha256(path)
            if backup_dir is not None:
                dest = backup_dir / "files" / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)

            result = set_frontmatter_key_textual(
                path,
                OUR_KEY,
                resolved,
                vault_root=vault_root,
                agent_folder=settings.agent.folder_name,
                allow_agent_folder=allow_agent_folder,
                grace_minutes=grace_minutes,
            )
            entry.written = result.changed
            entry.reason = result.reason

            if result.changed:
                report.written += 1
                manifest_lines.append(
                    json.dumps(
                        {
                            "rel_path": rel_path,
                            "uuid": resolved,
                            "action": action,
                            "uuid_source": source,
                            "sha256_before": sha_before,
                            "sha256_after": _sha256(path),
                        }
                    )
                )
            else:
                # Unwritable for a legitimate reason (daemon: ignore, grace
                # window, read-only). Fall back to a deterministic path-derived
                # identity so the note still participates — but record that its
                # identity is NOT rename-stable.
                entry.uuid = resolved = derive_path_uuid(rel_path)
                entry.uuid_source = source = "derived_path"
                entry.action = "fallback"
                report.fallback += 1

        if apply and registry is not None:
            registry.upsert(
                NoteRecord(
                    uuid=resolved,
                    rel_path=rel_path,
                    title=note.title,
                    mtime=note.mtime,
                    body_sha256=hashlib.sha256(note.body.encode()).hexdigest(),
                    tags=note.tags,
                    word_count=note.word_count,
                    uuid_source=source,
                    in_frontmatter=entry.action != "fallback",
                    status="ignored" if entry.action == "fallback" else "active",
                )
            )

        report.entries.append(entry)

    if backup_dir is not None:
        (backup_dir / "manifest.jsonl").write_text(
            "".join(line + "\n" for line in manifest_lines), encoding="utf-8"
        )

    return report


def list_migration_runs(settings: Settings) -> list[str]:
    root = _migrations_root(settings)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "manifest.jsonl").is_file())


def rollback_uuids(
    settings: Settings,
    run_id: str,
    *,
    mode: str = "key-removal",
    force: bool = False,
    grace_minutes: int = 2,
) -> RollbackReport:
    """Undo an ``assign-uuids`` run.

    ``key-removal`` (the default) deletes the one line we added and leaves
    everything else alone, so it is safe on files edited since. ``restore``
    copies the original bytes back and refuses any file whose content has
    changed since the migration unless ``force``.
    """

    vault_root = settings.vault.path.expanduser().resolve()
    run_dir = _migrations_root(settings) / run_id
    manifest = run_dir / "manifest.jsonl"
    if not manifest.is_file():
        raise FileNotFoundError(f"no migration run {run_id!r} under {run_dir}")

    report = RollbackReport(run_id=run_id, mode=mode)
    registry = NoteRegistry(db_path=settings.feedback.db_path)

    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        rel_path = record["rel_path"]
        target = vault_root / rel_path
        if not target.is_file():
            report.refused.append(rel_path)
            continue

        if mode == "restore":
            if not force and _sha256(target) != record["sha256_after"]:
                report.refused.append(rel_path)
                continue
            shutil.copy2(run_dir / "files" / rel_path, target)
        else:
            remove_frontmatter_key_textual(
                target,
                OUR_KEY,
                vault_root=vault_root,
                agent_folder=settings.agent.folder_name,
                allow_agent_folder=True,
                grace_minutes=grace_minutes,
                drop_empty_block=True,
            )

        registry.forget(record["uuid"])
        report.reverted.append(rel_path)

    return report
