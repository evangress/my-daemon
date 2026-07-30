# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from my_daemon.vault.chunker import chunk_note
from my_daemon.vault.parser import parse_note


def test_chunks_preserve_heading_path(vault_root: Path):
    note = parse_note(vault_root / "Designing AI Memory.md", vault_root)
    chunks = chunk_note(note)
    assert chunks, "expected at least one chunk"
    # Look for the "RAG vs Fine-tuning" subsection
    subsection = [c for c in chunks if "RAG vs Fine-tuning" in (c.heading_path or [])]
    assert subsection, "subsection chunk should be present with its heading path"


def test_chunk_ids_are_stable_across_runs(vault_root: Path):
    note = parse_note(vault_root / "Designing AI Memory.md", vault_root)
    a = chunk_note(note)
    b = chunk_note(note)
    assert [c.id for c in a] == [c.id for c in b]


def test_chunk_inherits_parent_tags(vault_root: Path):
    note = parse_note(vault_root / "Designing AI Memory.md", vault_root)
    chunks = chunk_note(note)
    assert all("memory" in c.tags for c in chunks)


def test_note_without_headings_still_chunks(tmp_path: Path):
    p = tmp_path / "flat.md"
    p.write_text("just a flat note with [[Some Link]] and no headings at all.", encoding="utf-8")
    note = parse_note(p, tmp_path)
    chunks = chunk_note(note)
    assert len(chunks) == 1
    assert "Some Link" in chunks[0].wikilinks


def test_chunks_inherit_the_notes_dates(tmp_path: Path):
    from datetime import UTC, datetime

    p = tmp_path / "2026-03-31.md"
    p.write_text("# A\ntext one\n\n## B\ntext two\n", encoding="utf-8")
    note = parse_note(p, tmp_path)
    chunks = chunk_note(note)
    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert c.occurred_at == datetime(2026, 3, 31, tzinfo=UTC)
        assert c.occurred_at_source == "filename"
        assert c.modified_at == note.mtime
