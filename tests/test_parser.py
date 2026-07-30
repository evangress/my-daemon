# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import UTC, date, datetime
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


def test_malformed_frontmatter_degrades_instead_of_raising(tmp_path: Path):
    """Real vaults contain YAML like `related: [[A]], [[B]]` (unquoted wikilinks).

    One such note killed the entire first real-vault ingest (2026-07-27). The
    contract: parse_note never raises on bad YAML — metadata degrades to {},
    the full raw text (including the broken block) stays as body so the
    content is still embedded, and wikilinks inside the broken frontmatter
    still register via the body regex.
    """
    note = tmp_path / "Docling.md"
    note.write_text(
        "---\n"
        "tags: [python, ocr]\n"
        "related: [[Haystack Framework - Overview]], [[Personal Knowledge Base]]\n"
        "sorted: true\n"
        "---\n\n"
        "# Docling\n\nBody text about document parsing.\n",
        encoding="utf-8",
    )

    parsed = parse_note(note, tmp_path)

    assert parsed.frontmatter == {}
    # No adoptable uuid in broken YAML; derivation happens later in ingest.
    assert parsed.uuid is None
    assert parsed.uuid_source is None
    assert "Body text about document parsing." in parsed.body
    assert "related: [[Haystack Framework - Overview]]" in parsed.body  # raw block preserved
    assert "Haystack Framework - Overview" in parsed.wikilinks
    assert "Personal Knowledge Base" in parsed.wikilinks
    assert parsed.title == "Docling"


def test_parse_note_derives_occurred_at_from_frontmatter(tmp_path):
    p = tmp_path / "note.md"
    p.write_text('---\ndate: "2024-09-02"\n---\n# Title\nbody\n', encoding="utf-8")
    note = parse_note(p, tmp_path)
    assert note.occurred_at == datetime(2024, 9, 2, tzinfo=UTC)
    assert note.occurred_at_source == "frontmatter"


def test_parse_note_derives_occurred_at_from_filename(tmp_path):
    p = tmp_path / "2026-03-31 Journal Entry.md"
    p.write_text("# Entry\nbody\n", encoding="utf-8")
    note = parse_note(p, tmp_path)
    assert note.occurred_at == datetime(2026, 3, 31, tzinfo=UTC)
    assert note.occurred_at_source == "filename"


def test_parse_note_leaves_undated_notes_undated(tmp_path):
    p = tmp_path / "Welcome.md"
    p.write_text("# Welcome\nbody\n", encoding="utf-8")
    note = parse_note(p, tmp_path)
    assert note.occurred_at is None and note.occurred_at_source is None
    assert note.mtime is not None, "mtime is still recorded, just not as the episodic axis"


def test_parse_note_honours_the_mtime_cutoff(tmp_path):
    """The fixture deliberately avoids exact UTC midnight — a midnight mtime would
    pass even if the implementation forgot to normalise mtime down to a day before
    comparing/returning it. Non-midnight proves normalisation actually happens."""
    import os

    p = tmp_path / "Welcome.md"
    p.write_text("# Welcome\nbody\n", encoding="utf-8")
    old = datetime(2026, 2, 21, 13, 3, 47, tzinfo=UTC).timestamp()
    os.utime(p, (old, old))
    note = parse_note(p, tmp_path, mtime_trusted_before=date(2026, 6, 21))
    assert note.occurred_at == datetime(2026, 2, 21, 0, 0, tzinfo=UTC)
    assert note.occurred_at_source == "mtime"
    assert note.mtime == datetime(2026, 2, 21, 13, 3, 47, tzinfo=UTC), (
        "mtime keeps its full precision; only occurred_at is day-normalised"
    )


def test_mtime_and_occurred_at_are_independent(tmp_path):
    """The whole point: when the file was touched is not when the thing happened."""
    import os

    p = tmp_path / "2024-09-02.md"
    p.write_text("# Entry\nbody\n", encoding="utf-8")
    touched = datetime(2026, 7, 1, 12, 0, tzinfo=UTC).timestamp()
    os.utime(p, (touched, touched))
    note = parse_note(p, tmp_path)
    assert note.occurred_at == datetime(2024, 9, 2, tzinfo=UTC)
    assert note.mtime == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
