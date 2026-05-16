from __future__ import annotations

from pathlib import Path

from my_daemon.vault.parser import parse_note


def test_parses_frontmatter_and_title(vault_root: Path):
    note = parse_note(vault_root / "Designing AI Memory.md", vault_root)
    assert note.title == "Designing AI Memory"
    assert "ai" in note.tags
    assert "memory" in note.tags
    # H1 should be in body too
    assert note.body.startswith("# Designing AI Memory")


def test_extracts_wikilinks_and_inline_tags(vault_root: Path):
    note = parse_note(vault_root / "Designing AI Memory.md", vault_root)
    assert "Pullman Daemons" in note.wikilinks
    assert "Socratic Daemon" in note.wikilinks
    assert "Obsidian Vaults" in note.wikilinks
    # frontmatter + inline tags merged
    assert "rag" in note.tags
    assert "graph" in note.tags


def test_inline_tags_skip_code_fences(tmp_path: Path):
    p = tmp_path / "note.md"
    p.write_text(
        "# Title\n\n#real-tag\n\n```\n#code-block-tag\n```\n",
        encoding="utf-8",
    )
    note = parse_note(p, tmp_path)
    assert "real-tag" in note.tags
    assert "code-block-tag" not in note.tags


def test_note_with_no_wikilinks(vault_root: Path):
    note = parse_note(vault_root / "Grocery List.md", vault_root)
    assert note.wikilinks == []
    assert "household" in note.tags
