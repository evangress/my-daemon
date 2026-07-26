# SPDX-License-Identifier: Apache-2.0
"""The `daemon migrate` sub-app."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.stores import db as dbmod

runner = CliRunner()


@pytest.fixture
def settings_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `_load()` at a throwaway state DB and hand back its path."""
    from my_daemon.config import Settings

    settings = Settings()
    db_path = tmp_path / "data" / "feedback.db"
    settings.feedback.db_path = db_path
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    return db_path


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    v = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()
    return v


def test_migrate_db_brings_the_file_up_to_the_current_version(settings_with_db: Path):
    result = runner.invoke(app, ["migrate", "db"])

    assert result.exit_code == 0, result.output
    assert _user_version(settings_with_db) == dbmod.SCHEMA_VERSION


def test_migrate_db_reports_when_there_is_nothing_to_do(settings_with_db: Path):
    runner.invoke(app, ["migrate", "db"])

    result = runner.invoke(app, ["migrate", "db"])

    assert result.exit_code == 0, result.output
    assert "already at" in result.output.lower()


def test_migrate_status_shows_current_and_target_versions(settings_with_db: Path):
    result = runner.invoke(app, ["migrate", "status"])

    assert result.exit_code == 0, result.output
    assert str(dbmod.SCHEMA_VERSION) in result.output


def test_migrate_status_lists_pending_migrations_by_name(settings_with_db: Path):
    """A never-created DB is at version 0, so every migration is pending."""
    result = runner.invoke(app, ["migrate", "status"])

    assert result.exit_code == 0, result.output
    assert "baseline_feedback_and_agent_state" in result.output


# ---------------------------------------------------------------------------
# assign-uuids
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from my_daemon.config import Settings

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n", encoding="utf-8")

    settings = Settings()
    settings.vault.path = vault
    settings.feedback.db_path = tmp_path / "data" / "state.db"
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    return settings, vault


def _uuid_line(note: Path) -> str | None:
    for line in note.read_text().splitlines():
        if line.startswith("uuid:"):
            return line
    return None


def test_assign_uuids_is_a_dry_run_by_default(vault_settings):
    _settings, vault = vault_settings

    result = runner.invoke(app, ["migrate", "assign-uuids", "--grace-minutes", "0"])

    assert result.exit_code == 0, result.output
    assert _uuid_line(vault / "A.md") is None
    assert "dry run" in result.output.lower()


def test_assign_uuids_apply_stamps_the_vault(vault_settings):
    _settings, vault = vault_settings

    result = runner.invoke(
        app, ["migrate", "assign-uuids", "--apply", "--grace-minutes", "0"]
    )

    assert result.exit_code == 0, result.output
    assert _uuid_line(vault / "A.md") is not None


def test_list_runs_shows_an_applied_run(vault_settings):
    runner.invoke(app, ["migrate", "assign-uuids", "--apply", "--grace-minutes", "0"])

    result = runner.invoke(app, ["migrate", "list-runs"])

    assert result.exit_code == 0, result.output
    assert "20" in result.output  # the ISO-ish run id


def test_rollback_removes_the_stamped_key(vault_settings):
    _settings, vault = vault_settings
    runner.invoke(app, ["migrate", "assign-uuids", "--apply", "--grace-minutes", "0"])
    from my_daemon.pipeline.migrate_uuids import list_migration_runs

    run_id = list_migration_runs(_settings)[0]

    result = runner.invoke(
        app, ["migrate", "rollback-uuids", run_id, "--grace-minutes", "0"]
    )

    assert result.exit_code == 0, result.output
    assert _uuid_line(vault / "A.md") is None


def test_rollback_of_an_unknown_run_exits_nonzero(vault_settings):
    result = runner.invoke(app, ["migrate", "rollback-uuids", "nope"])

    assert result.exit_code != 0
