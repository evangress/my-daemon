# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from my_daemon.vault import VaultReader


def test_reader_resolves_wikilinks_to_relative_paths(vault_root: Path):
    reader = VaultReader(vault_root)
    notes = {n.relative_path: n for n in reader.read_all()}

    home = notes["Designing AI Memory.md"]
    # raw wikilink text "Pullman Daemons" should resolve to its relative path
    assert "Pullman Daemons.md" in home.wikilinks
    assert "Obsidian Vaults.md" in home.wikilinks


def test_reader_excludes_hidden_dirs(tmp_path: Path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "config.md").write_text("# config", encoding="utf-8")
    (tmp_path / "real.md").write_text("# real", encoding="utf-8")

    reader = VaultReader(tmp_path, exclude_dirs=[".obsidian"])
    rel_paths = [n.relative_path for n in reader.read_all()]
    assert "real.md" in rel_paths
    assert all(".obsidian" not in p for p in rel_paths)


def test_reader_threads_the_mtime_cutoff_to_every_note(tmp_path):
    """A config key that silently does nothing is worse than an absent one, and
    the primary ingest path reaches parse_note only through VaultReader."""
    import os
    from datetime import UTC, date, datetime

    p = tmp_path / "Welcome.md"
    p.write_text("# Welcome\nbody\n", encoding="utf-8")
    old = datetime(2026, 2, 21, 13, 3, 47, tzinfo=UTC).timestamp()
    os.utime(p, (old, old))

    notes = list(VaultReader(tmp_path, mtime_trusted_before=date(2026, 6, 21)).read_all())
    assert [n.occurred_at for n in notes] == [datetime(2026, 2, 21, 0, 0, tzinfo=UTC)]
    assert [n.occurred_at_source for n in notes] == ["mtime"]


def test_reader_defaults_leave_the_mtime_fallback_off(tmp_path):
    import os
    from datetime import UTC, datetime

    p = tmp_path / "Welcome.md"
    p.write_text("# Welcome\nbody\n", encoding="utf-8")
    old = datetime(2026, 2, 21, tzinfo=UTC).timestamp()
    os.utime(p, (old, old))

    notes = list(VaultReader(tmp_path).read_all())
    assert notes[0].occurred_at is None
