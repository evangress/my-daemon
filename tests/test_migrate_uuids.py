# SPDX-License-Identifier: Apache-2.0
"""`daemon migrate assign-uuids` — the one command that writes to every note.

Dry-run by default, batch-backed-up, and reversible. These tests exercise it
against throwaway vaults; the byte-level frontmatter contract it depends on is
pinned separately in ``test_vault_writer_frontmatter.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.pipeline.migrate_uuids import (
    assign_uuids,
    list_migration_runs,
    rollback_uuids,
)
from my_daemon.stores.registry import NoteRegistry
from my_daemon.vault.identity import canonicalize, derive_path_uuid

FOREIGN = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("---\ntitle: A\n---\n\n# A\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n\nno frontmatter\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(vault: Path, tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.feedback.db_path = tmp_path / "data" / "state.db"
    return s


def _uuid_of(note: Path) -> str | None:
    for line in note.read_text().splitlines():
        if line.startswith("uuid:"):
            return canonicalize(line.split(":", 1)[1].strip())
    return None


def _run(settings: Settings, **kw):
    return assign_uuids(settings, grace_minutes=0, **kw)


# ---------------------------------------------------------------------------
# Dry run is the default
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_nothing(settings: Settings, vault: Path):
    before = {p.name: p.read_bytes() for p in vault.glob("*.md")}

    report = _run(settings)

    assert report.dry_run is True
    assert {p.name: p.read_bytes() for p in vault.glob("*.md")} == before


def test_a_dry_run_still_reports_what_it_would_do(settings: Settings):
    report = _run(settings)

    assert report.scanned == 2
    assert report.would_write == 2
    assert {e.rel_path for e in report.entries} == {"A.md", "B.md"}


def test_a_dry_run_does_not_touch_the_registry(settings: Settings):
    _run(settings)

    assert NoteRegistry(db_path=settings.feedback.db_path).coverage().total == 0


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_apply_stamps_every_note(settings: Settings, vault: Path):
    report = _run(settings, apply=True)

    assert report.written == 2
    assert _uuid_of(vault / "A.md") is not None
    assert _uuid_of(vault / "B.md") is not None


def test_stamped_uuids_are_distinct(settings: Settings, vault: Path):
    _run(settings, apply=True)

    assert _uuid_of(vault / "A.md") != _uuid_of(vault / "B.md")


def test_apply_populates_the_registry(settings: Settings, vault: Path):
    _run(settings, apply=True)

    registry = NoteRegistry(db_path=settings.feedback.db_path)
    coverage = registry.coverage()
    assert coverage.total == 2
    assert coverage.in_frontmatter == 2
    assert registry.by_path("A.md").uuid == _uuid_of(vault / "A.md")


def test_re_running_is_idempotent(settings: Settings, vault: Path):
    _run(settings, apply=True)
    after_first = {p.name: p.read_bytes() for p in vault.glob("*.md")}

    report = _run(settings, apply=True)

    assert report.written == 0
    assert {p.name: p.read_bytes() for p in vault.glob("*.md")} == after_first


def test_an_existing_uuid_is_adopted_not_replaced(settings: Settings, vault: Path):
    (vault / "A.md").write_text(
        f"---\nuuid: {FOREIGN}\n---\n\n# A\n", encoding="utf-8"
    )

    _run(settings, apply=True)

    assert _uuid_of(vault / "A.md") == FOREIGN


def test_a_foreign_id_key_is_stamped_under_our_key_and_left_alone(
    settings: Settings, vault: Path
):
    (vault / "A.md").write_text(f"---\nid: {FOREIGN}\n---\n\n# A\n", encoding="utf-8")

    _run(settings, apply=True)

    text = (vault / "A.md").read_text()
    assert f"id: {FOREIGN}" in text  # theirs, untouched
    assert _uuid_of(vault / "A.md") == FOREIGN  # ours, matching


def test_a_limit_bounds_the_run(settings: Settings, vault: Path):
    report = _run(settings, apply=True, limit=1)

    assert report.written == 1


def test_a_path_glob_scopes_the_run(settings: Settings, vault: Path):
    (vault / "Projects").mkdir()
    (vault / "Projects" / "C.md").write_text("# C\n", encoding="utf-8")

    report = _run(settings, apply=True, path_glob="Projects/**")

    assert [e.rel_path for e in report.entries] == ["Projects/C.md"]
    assert _uuid_of(vault / "A.md") is None


# ---------------------------------------------------------------------------
# Notes that can't be written
# ---------------------------------------------------------------------------


def test_a_daemon_ignore_note_gets_a_path_derived_fallback(
    settings: Settings, vault: Path
):
    (vault / "A.md").write_text(
        "---\ndaemon: ignore\n---\n\n# A\n", encoding="utf-8"
    )

    _run(settings, apply=True)

    assert _uuid_of(vault / "A.md") is None  # not written
    record = NoteRegistry(db_path=settings.feedback.db_path).by_path("A.md")
    assert record.uuid == derive_path_uuid("A.md")
    assert record.uuid_source == "derived_path"
    assert record.in_frontmatter is False


def test_the_report_counts_notes_left_on_a_fragile_identity(
    settings: Settings, vault: Path
):
    (vault / "A.md").write_text(
        "---\ndaemon: ignore\n---\n\n# A\n", encoding="utf-8"
    )

    report = _run(settings, apply=True)

    assert report.fallback == 1


def test_the_agent_folder_is_skipped_when_the_config_excludes_it(
    settings: Settings, vault: Path
):
    """Scope follows `exclude_dirs` — stamping notes ingest never sees is pointless."""
    assert "Agent" in settings.vault.exclude_dirs  # the shipped default
    (vault / "Agent").mkdir()
    (vault / "Agent" / "observer-2026-07-26.md").write_text("# L\n", encoding="utf-8")

    report = _run(settings, apply=True)

    assert [e.rel_path for e in report.entries] == ["A.md", "B.md"]


def test_the_agent_folder_is_stamped_when_the_config_includes_it(
    settings: Settings, vault: Path
):
    """The writer's agent-folder gate must not veto what discovery selected."""
    settings.vault.exclude_dirs = [".obsidian", ".trash", "templates"]
    (vault / "Agent").mkdir()
    (vault / "Agent" / "observer-2026-07-26.md").write_text("# L\n", encoding="utf-8")

    _run(settings, apply=True)

    assert _uuid_of(vault / "Agent" / "observer-2026-07-26.md") is not None


def test_the_migrations_backup_directory_is_never_scanned(
    settings: Settings, vault: Path
):
    """Belt and braces: backups must be invisible even if Agent is ingestible."""
    settings.vault.exclude_dirs = [".obsidian", ".trash", "templates"]
    _run(settings, apply=True)

    second = _run(settings, apply=True)

    assert [e.rel_path for e in second.entries] == ["A.md", "B.md"]


# ---------------------------------------------------------------------------
# Backup and rollback
# ---------------------------------------------------------------------------


def test_apply_writes_a_batch_backup_with_a_manifest(settings: Settings, vault: Path):
    report = _run(settings, apply=True)

    assert report.backup_dir is not None
    manifest = report.backup_dir / "manifest.jsonl"
    lines = [json.loads(line) for line in manifest.read_text().splitlines()]
    assert {entry["rel_path"] for entry in lines} == {"A.md", "B.md"}
    assert (report.backup_dir / "files" / "A.md").exists()


def test_the_backup_holds_the_original_bytes(settings: Settings, vault: Path):
    original = (vault / "A.md").read_bytes()

    report = _run(settings, apply=True)

    assert (report.backup_dir / "files" / "A.md").read_bytes() == original


def test_runs_can_be_listed(settings: Settings):
    report = _run(settings, apply=True)

    assert list_migration_runs(settings) == [report.run_id]


def test_key_removal_rollback_restores_the_original_bytes(
    settings: Settings, vault: Path
):
    before = {p.name: p.read_bytes() for p in vault.glob("*.md")}
    report = _run(settings, apply=True)

    rollback_uuids(settings, report.run_id, grace_minutes=0)

    assert {p.name: p.read_bytes() for p in vault.glob("*.md")} == before


def test_key_removal_rollback_keeps_edits_made_since(settings: Settings, vault: Path):
    """Surgical by design — it removes one line, never restores a body."""
    report = _run(settings, apply=True)
    stamped = _uuid_of(vault / "B.md")
    (vault / "B.md").write_text(
        f"---\nuuid: {stamped}\n---\n\n# B\n\nNEW PARAGRAPH\n", encoding="utf-8"
    )

    rollback_uuids(settings, report.run_id, grace_minutes=0)

    text = (vault / "B.md").read_text()
    assert "NEW PARAGRAPH" in text
    assert "uuid:" not in text


def test_rollback_clears_the_registry_rows(settings: Settings, vault: Path):
    report = _run(settings, apply=True)

    rollback_uuids(settings, report.run_id, grace_minutes=0)

    assert NoteRegistry(db_path=settings.feedback.db_path).coverage().total == 0


def test_restore_mode_refuses_a_file_edited_since_the_migration(
    settings: Settings, vault: Path
):
    report = _run(settings, apply=True)
    (vault / "B.md").write_text("# B\n\nEDITED SINCE\n", encoding="utf-8")

    result = rollback_uuids(settings, report.run_id, mode="restore", grace_minutes=0)

    assert "B.md" in result.refused
    assert "EDITED SINCE" in (vault / "B.md").read_text()


def test_restore_mode_overwrites_when_forced(settings: Settings, vault: Path):
    original = (vault / "B.md").read_bytes()
    report = _run(settings, apply=True)
    (vault / "B.md").write_text("# B\n\nEDITED SINCE\n", encoding="utf-8")

    rollback_uuids(
        settings, report.run_id, mode="restore", force=True, grace_minutes=0
    )

    assert (vault / "B.md").read_bytes() == original


def test_rolling_back_an_unknown_run_raises(settings: Settings):
    with pytest.raises(FileNotFoundError):
        rollback_uuids(settings, "nope", grace_minutes=0)
