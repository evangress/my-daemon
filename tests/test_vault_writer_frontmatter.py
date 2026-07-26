# SPDX-License-Identifier: Apache-2.0
"""Textual single-key frontmatter editing.

The UUID migration touches every note in the vault, so its writer must change
exactly one line and leave the rest of the file byte-identical. A
``frontmatter.loads``/``dumps`` round-trip cannot do that — PyYAML reorders
keys, strips comments, requotes strings, expands flow-style lists into block
style, and re-renders dates. These tests pin the byte-level contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.vault.writer import (
    detect_newline,
    remove_frontmatter_key_textual,
    set_frontmatter_key_textual,
)

UUID = "9f3c1a7e-4b21-4d6f-9c88-1e2a5b0d7f43"


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _set(note: Path, vault: Path, key: str = "uuid", value: str = UUID, **kw):
    return set_frontmatter_key_textual(
        note, key, value, vault_root=vault, grace_minutes=0, **kw
    )


# ---------------------------------------------------------------------------
# Line endings — bug B: _atomic_write_text used to force "\n"
# ---------------------------------------------------------------------------


def test_detect_newline_identifies_crlf_and_lf():
    assert detect_newline("a\r\nb\r\n") == "\r\n"
    assert detect_newline("a\nb\n") == "\n"
    assert detect_newline("no newlines at all") == "\n"


def test_detect_newline_picks_the_dominant_ending_in_a_mixed_file():
    assert detect_newline("a\r\nb\r\nc\n") == "\r\n"


def test_writing_a_key_preserves_crlf_line_endings(vault: Path):
    """A Windows-synced vault must not come back as a whole-file diff."""
    note = _write(vault / "n.md", "---\r\ntitle: Hi\r\n---\r\n\r\n# Hi\r\n")

    _set(note, vault)

    raw = note.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.decode() == f"---\r\ntitle: Hi\r\nuuid: {UUID}\r\n---\r\n\r\n# Hi\r\n"


def test_writing_a_key_preserves_lf_line_endings(vault: Path):
    note = _write(vault / "n.md", "---\ntitle: Hi\n---\n\n# Hi\n")

    _set(note, vault)

    assert note.read_bytes() == (
        f"---\ntitle: Hi\nuuid: {UUID}\n---\n\n# Hi\n".encode()
    )


# ---------------------------------------------------------------------------
# Everything outside the one line stays byte-identical
# ---------------------------------------------------------------------------


def test_yaml_comments_survive(vault: Path):
    note = _write(
        vault / "n.md",
        "---\n# a comment PyYAML would eat\ntitle: Hi\n---\n\nbody\n",
    )

    _set(note, vault)

    assert note.read_text() == (
        f"---\n# a comment PyYAML would eat\ntitle: Hi\nuuid: {UUID}\n---\n\nbody\n"
    )


def test_flow_style_lists_are_not_expanded(vault: Path):
    note = _write(vault / "n.md", "---\ntags: [memory, philosophy]\n---\n\nbody\n")

    _set(note, vault)

    assert "tags: [memory, philosophy]" in note.read_text()


def test_key_order_and_quoting_style_survive(vault: Path):
    note = _write(
        vault / "n.md",
        "---\nzebra: 'single quoted'\nalpha: \"double quoted\"\n---\n\nbody\n",
    )

    _set(note, vault)

    assert note.read_text() == (
        "---\nzebra: 'single quoted'\nalpha: \"double quoted\"\n"
        f"uuid: {UUID}\n---\n\nbody\n"
    )


def test_dates_are_not_re_rendered(vault: Path):
    note = _write(vault / "n.md", "---\ncreated: 2026-07-26\n---\n\nbody\n")

    _set(note, vault)

    assert "created: 2026-07-26" in note.read_text()


def test_a_utf8_bom_is_preserved(vault: Path):
    note = vault / "n.md"
    note.write_bytes(b"\xef\xbb\xbf---\ntitle: Hi\n---\n\nbody\n")

    _set(note, vault)

    raw = note.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert f"uuid: {UUID}".encode() in raw


# ---------------------------------------------------------------------------
# Finding the frontmatter block
# ---------------------------------------------------------------------------


def test_a_note_without_frontmatter_gets_a_minimal_block(vault: Path):
    note = _write(vault / "n.md", "# Just a heading\n\nbody\n")

    _set(note, vault)

    assert note.read_text() == f"---\nuuid: {UUID}\n---\n# Just a heading\n\nbody\n"


def test_a_horizontal_rule_in_the_body_is_not_mistaken_for_frontmatter(vault: Path):
    """`---` only opens frontmatter on the very first line."""
    note = _write(vault / "n.md", "# Heading\n\nabove\n\n---\n\nbelow\n")

    _set(note, vault)

    text = note.read_text()
    assert text.startswith(f"---\nuuid: {UUID}\n---\n# Heading")
    assert "above\n\n---\n\nbelow\n" in text


def test_an_existing_value_for_the_same_key_is_replaced_in_place(vault: Path):
    note = _write(
        vault / "n.md", "---\nuuid: 00000000-0000-4000-8000-000000000000\ntitle: Hi\n---\n\nbody\n"
    )

    _set(note, vault)

    assert note.read_text() == f"---\nuuid: {UUID}\ntitle: Hi\n---\n\nbody\n"


def test_an_empty_frontmatter_block_gains_the_key(vault: Path):
    note = _write(vault / "n.md", "---\n---\n\nbody\n")

    _set(note, vault)

    assert note.read_text() == f"---\nuuid: {UUID}\n---\n\nbody\n"


def test_a_similarly_named_key_is_not_mistaken_for_the_target(vault: Path):
    note = _write(vault / "n.md", "---\nuuid_source: manual\n---\n\nbody\n")

    _set(note, vault)

    text = note.read_text()
    assert "uuid_source: manual" in text
    assert f"uuid: {UUID}" in text


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------


def test_daemon_ignore_blocks_the_write(vault: Path):
    note = _write(vault / "n.md", "---\ndaemon: ignore\n---\n\nbody\n")
    before = note.read_bytes()

    result = _set(note, vault)

    assert result.changed is False
    assert "ignore" in result.reason
    assert note.read_bytes() == before


def test_the_agent_folder_is_refused_by_default(vault: Path):
    (vault / "Agent").mkdir()
    note = _write(vault / "Agent" / "letter.md", "---\n---\n\nbody\n")

    result = _set(note, vault)

    assert result.changed is False


def test_the_agent_folder_can_be_opted_into(vault: Path):
    """The migration stamps its own generated files — that is its business."""
    (vault / "Agent").mkdir()
    note = _write(vault / "Agent" / "letter.md", "---\n---\n\nbody\n")

    result = _set(note, vault, allow_agent_folder=True)

    assert result.changed is True
    assert f"uuid: {UUID}" in note.read_text()


def test_a_path_outside_the_vault_is_refused(vault: Path, tmp_path: Path):
    outside = _write(tmp_path / "elsewhere.md", "---\n---\n\nbody\n")

    result = _set(outside, vault)

    assert result.changed is False
    assert "escapes vault" in result.reason


def test_writing_the_same_value_twice_is_a_no_op(vault: Path):
    note = _write(vault / "n.md", "---\ntitle: Hi\n---\n\nbody\n")
    _set(note, vault)
    after_first = note.read_bytes()

    result = _set(note, vault)

    assert result.changed is False
    assert note.read_bytes() == after_first


# ---------------------------------------------------------------------------
# Removal — the rollback path
# ---------------------------------------------------------------------------


def test_removing_a_key_leaves_the_rest_byte_identical(vault: Path):
    original = "---\n# comment\ntags: [a, b]\ntitle: Hi\n---\n\nbody\n"
    note = _write(vault / "n.md", original)
    _set(note, vault)

    remove_frontmatter_key_textual(note, "uuid", vault_root=vault, grace_minutes=0)

    assert note.read_text() == original


def test_removing_a_key_preserves_crlf(vault: Path):
    original = "---\r\ntitle: Hi\r\n---\r\n\r\nbody\r\n"
    note = _write(vault / "n.md", original)
    _set(note, vault)

    remove_frontmatter_key_textual(note, "uuid", vault_root=vault, grace_minutes=0)

    assert note.read_bytes() == original.encode()


def test_removing_the_last_key_can_drop_the_block_it_created(vault: Path):
    """Rollback must leave no trace on a note that had no frontmatter."""
    original = "# Just a heading\n\nbody\n"
    note = _write(vault / "n.md", original)
    _set(note, vault)

    remove_frontmatter_key_textual(
        note, "uuid", vault_root=vault, grace_minutes=0, drop_empty_block=True
    )

    assert note.read_text() == original


def test_an_emptied_block_is_kept_unless_asked_to_drop_it(vault: Path):
    note = _write(vault / "n.md", "# Just a heading\n\nbody\n")
    _set(note, vault)

    remove_frontmatter_key_textual(note, "uuid", vault_root=vault, grace_minutes=0)

    assert note.read_text().startswith("---\n---\n")


def test_dropping_an_empty_block_leaves_a_populated_one_alone(vault: Path):
    note = _write(vault / "n.md", "---\ntitle: Hi\n---\n\nbody\n")
    _set(note, vault)

    remove_frontmatter_key_textual(
        note, "uuid", vault_root=vault, grace_minutes=0, drop_empty_block=True
    )

    assert note.read_text() == "---\ntitle: Hi\n---\n\nbody\n"


def test_removing_an_absent_key_is_a_no_op(vault: Path):
    note = _write(vault / "n.md", "---\ntitle: Hi\n---\n\nbody\n")
    before = note.read_bytes()

    result = remove_frontmatter_key_textual(
        note, "uuid", vault_root=vault, grace_minutes=0
    )

    assert result.changed is False
    assert note.read_bytes() == before
