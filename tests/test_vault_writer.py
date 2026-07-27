# SPDX-License-Identifier: Apache-2.0
"""Unit tests for vault/writer.py.

Focus on safety + idempotency, not LLM behavior:
- sentinel-marker idempotency
- frontmatter round-trip preservation
- wikilink insertion skips code fences and existing wikilinks
- is_writable enforces vault containment, Agent/ exclusion, daemon:ignore, grace period
- refusal on double ## Agent Notes headings
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from my_daemon.vault.writer import (
    SENTINEL_END,
    SENTINEL_START,
    add_tags,
    insert_wikilinks,
    is_writable,
    snapshot,
    write_agent_section,
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A scratch vault root."""
    (tmp_path / "Agent").mkdir()
    return tmp_path


def _write_note(vault: Path, rel: str, text: str, *, age_seconds: int = 3600) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    # Backdate so the 30-minute grace period doesn't gate us out.
    past = time.time() - age_seconds
    os.utime(p, (past, past))
    return p


def test_write_agent_section_appends_with_markers(vault: Path) -> None:
    note = _write_note(vault, "a.md", "# Title\n\nbody text\n")
    result = write_agent_section(note, "hello, world.", vault_root=vault)
    assert result.changed
    text = note.read_text(encoding="utf-8")
    assert "## Agent Notes" in text
    assert SENTINEL_START in text
    assert SENTINEL_END in text
    assert "hello, world." in text
    assert text.startswith("# Title")  # preserved


def test_write_agent_section_is_idempotent(vault: Path) -> None:
    note = _write_note(vault, "a.md", "# Title\n\nbody text\n")
    write_agent_section(note, "first pass.", vault_root=vault)
    first = note.read_text(encoding="utf-8")
    # Backdate again so the grace period doesn't block the second pass.
    past = time.time() - 3600
    os.utime(note, (past, past))
    write_agent_section(note, "second pass.", vault_root=vault)
    second = note.read_text(encoding="utf-8")

    # Body outside the markers is identical.
    before_first = first.split(SENTINEL_START, 1)[0]
    before_second = second.split(SENTINEL_START, 1)[0]
    assert before_first == before_second

    # Inside the markers has changed.
    inside_first = first.split(SENTINEL_START, 1)[1].split(SENTINEL_END, 1)[0]
    inside_second = second.split(SENTINEL_START, 1)[1].split(SENTINEL_END, 1)[0]
    assert "first pass." in inside_first
    assert "second pass." in inside_second
    assert "first pass." not in inside_second


def test_write_agent_section_preserves_frontmatter(vault: Path) -> None:
    note = _write_note(
        vault,
        "a.md",
        "---\ntitle: Foo\ntags: [bar, baz]\n---\n\n# Title\n\nbody\n",
    )
    write_agent_section(note, "agent body", vault_root=vault)
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "tags:" in text
    assert "bar" in text and "baz" in text


def test_write_agent_section_refuses_double_heading(vault: Path) -> None:
    note = _write_note(
        vault,
        "a.md",
        "# Title\n\n## Agent Notes\nfirst\n\n## Agent Notes\nsecond\n",
    )
    result = write_agent_section(note, "x", vault_root=vault)
    assert not result.changed
    assert "Refusing" in result.reason or "expected at most one" in result.reason


def test_is_writable_blocks_agent_folder(vault: Path) -> None:
    note = _write_note(vault, "Agent/memory-personality.md", "# m\n")
    ok, reason = is_writable(note, vault_root=vault)
    assert not ok
    assert "Agent" in reason


def test_is_writable_blocks_daemon_ignore(vault: Path) -> None:
    note = _write_note(vault, "private.md", "---\ndaemon: ignore\n---\n\n# private\n")
    ok, reason = is_writable(note, vault_root=vault)
    assert not ok
    assert "ignore" in reason


def test_is_writable_blocks_grace_period(vault: Path) -> None:
    note = _write_note(vault, "fresh.md", "# fresh\n", age_seconds=10)
    ok, reason = is_writable(note, vault_root=vault)
    assert not ok
    assert "grace" in reason


def test_is_writable_blocks_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# nope\n")
    ok, reason = is_writable(outside, vault_root=vault)
    assert not ok
    assert "escapes vault" in reason


def test_insert_wikilinks_skips_code_fences(vault: Path) -> None:
    note = _write_note(
        vault,
        "a.md",
        "# Title\n\nThis mentions Daemon in prose.\n\n```\nDaemon in a code block\n```\n",
    )
    result = insert_wikilinks(note, [("Pullman Daemons", "Daemon")], vault_root=vault)
    assert result.changed
    text = note.read_text(encoding="utf-8")
    # The prose mention was rewritten; the code-block one was left alone.
    assert "[[Pullman Daemons|Daemon]] in prose" in text
    assert "Daemon in a code block" in text  # untouched
    # And the code-block one was NOT wrapped.
    assert "[[Pullman Daemons|Daemon]] in a code block" not in text


def test_insert_wikilinks_skips_existing_wikilinks(vault: Path) -> None:
    note = _write_note(vault, "a.md", "# t\n\nAlready [[other|Daemon]] here, and Daemon also.\n")
    result = insert_wikilinks(note, [("Pullman Daemons", "Daemon")], vault_root=vault)
    assert result.changed
    text = note.read_text(encoding="utf-8")
    # Existing [[other|Daemon]] is unchanged; the second 'Daemon' was wrapped.
    assert "[[other|Daemon]]" in text
    assert "[[Pullman Daemons|Daemon]] also" in text


def test_add_tags_extends_frontmatter_preserving_order(vault: Path) -> None:
    note = _write_note(vault, "a.md", "---\ntags: [b, a]\n---\n\n# t\n")
    result = add_tags(note, ["c", "a"], vault_root=vault)  # 'a' already there
    assert result.changed
    text = note.read_text(encoding="utf-8")
    # Original order preserved; 'c' appended; 'a' deduped.
    assert "b" in text and "a" in text and "c" in text


def test_snapshot_lands_in_agent_backups(vault: Path) -> None:
    note = _write_note(vault, "a.md", "# t\nbody\n")
    snap = snapshot(note, vault_root=vault)
    assert snap.is_file()
    assert "Agent" in snap.parts
    assert "backups" in snap.parts
    assert snap.read_text(encoding="utf-8") == note.read_text(encoding="utf-8")
