# SPDX-License-Identifier: Apache-2.0
"""Textual tag appending — the fix for `add_tags`' destructive round-trip.

M-mem-8 writes theme tags through this path, so it has to stop rewriting the
whole file first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.vault.writer import add_frontmatter_list_values_textual


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _note(vault: Path, text: str) -> Path:
    path = vault / "n.md"
    path.write_bytes(text.encode("utf-8"))
    return path


def _add(note: Path, vault: Path, values, key: str = "tags"):
    return add_frontmatter_list_values_textual(note, key, values, vault_root=vault, grace_minutes=0)


def test_flow_style_stays_flow_style(vault: Path):
    note = _note(vault, "---\ntags: [memory, philosophy]\n---\n\nbody\n")

    _add(note, vault, ["theme/forgetting"])

    assert note.read_text() == ("---\ntags: [memory, philosophy, theme/forgetting]\n---\n\nbody\n")


def test_block_style_stays_block_style(vault: Path):
    note = _note(vault, "---\ntags:\n  - memory\n  - philosophy\n---\n\nbody\n")

    _add(note, vault, ["theme/forgetting"])

    assert note.read_text() == (
        "---\ntags:\n  - memory\n  - philosophy\n  - theme/forgetting\n---\n\nbody\n"
    )


def test_block_style_indentation_is_matched(vault: Path):
    note = _note(vault, "---\ntags:\n    - memory\n---\n\nbody\n")

    _add(note, vault, ["theme/x"])

    assert "\n    - theme/x\n" in note.read_text()


def test_a_scalar_value_becomes_a_flow_list(vault: Path):
    note = _note(vault, "---\ntags: memory\n---\n\nbody\n")

    _add(note, vault, ["theme/x"])

    assert note.read_text() == "---\ntags: [memory, theme/x]\n---\n\nbody\n"


def test_a_missing_key_is_created(vault: Path):
    note = _note(vault, "---\ntitle: Hi\n---\n\nbody\n")

    _add(note, vault, ["theme/x"])

    assert note.read_text() == "---\ntitle: Hi\ntags: [theme/x]\n---\n\nbody\n"


def test_a_note_without_frontmatter_gains_a_minimal_block(vault: Path):
    note = _note(vault, "# Heading\n\nbody\n")

    _add(note, vault, ["theme/x"])

    assert note.read_text() == "---\ntags: [theme/x]\n---\n# Heading\n\nbody\n"


def test_comments_and_other_keys_survive_untouched(vault: Path):
    original = "---\n# a comment\ntitle: Hi\ntags: [a]\ncreated: 2026-07-26\n---\n\nbody\n"
    note = _note(vault, original)

    _add(note, vault, ["theme/x"])

    text = note.read_text()
    assert "# a comment" in text
    assert "created: 2026-07-26" in text
    assert "tags: [a, theme/x]" in text


def test_crlf_is_preserved(vault: Path):
    note = _note(vault, "---\r\ntags: [a]\r\n---\r\n\r\nbody\r\n")

    _add(note, vault, ["theme/x"])

    raw = note.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_an_existing_value_is_not_duplicated(vault: Path):
    note = _note(vault, "---\ntags: [memory]\n---\n\nbody\n")

    result = _add(note, vault, ["memory"])

    assert result.changed is False
    assert note.read_text() == "---\ntags: [memory]\n---\n\nbody\n"


def test_a_leading_hash_is_stripped(vault: Path):
    """Frontmatter tags never carry the inline `#` marker."""
    note = _note(vault, "---\ntags: [a]\n---\n\nbody\n")

    _add(note, vault, ["#theme/x"])

    assert "tags: [a, theme/x]" in note.read_text()


def test_daemon_ignore_still_blocks_the_write(vault: Path):
    note = _note(vault, "---\ndaemon: ignore\ntags: [a]\n---\n\nbody\n")
    before = note.read_bytes()

    result = _add(note, vault, ["theme/x"])

    assert result.changed is False
    assert note.read_bytes() == before
