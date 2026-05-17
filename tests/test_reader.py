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
