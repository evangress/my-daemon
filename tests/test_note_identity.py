# SPDX-License-Identifier: Apache-2.0
"""Reading a note's UUID out of frontmatter, and adopting foreign id keys.

Frontmatter is the source of truth for identity; the SQLite registry is derived
state. Everything here is about getting the identity *off disk* correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.vault.identity import (
    ADOPT_KEYS,
    derive_path_uuid,
    read_note_uuid,
)
from my_daemon.vault.parser import parse_note

UUID = "9f3c1a7e-4b21-4d6f-9c88-1e2a5b0d7f43"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _note(vault: Path, text: str, name: str = "n.md") -> Path:
    path = vault / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_note_carries_its_frontmatter_uuid(vault: Path):
    path = _note(vault, f"---\nuuid: {UUID}\n---\n\n# Hi\n")

    note = parse_note(path, vault)

    assert note.uuid == UUID


def test_a_note_without_a_uuid_has_none(vault: Path):
    path = _note(vault, "---\ntitle: Hi\n---\n\n# Hi\n")

    assert parse_note(path, vault).uuid is None


def test_a_uuid_is_canonicalized_to_lowercase(vault: Path):
    path = _note(vault, f"---\nuuid: {UUID.upper()}\n---\n\n# Hi\n")

    assert parse_note(path, vault).uuid == UUID


def test_a_uuid_wrapped_in_braces_or_urn_form_is_canonicalized(vault: Path):
    path = _note(vault, f"---\nuuid: urn:uuid:{UUID}\n---\n\n# Hi\n")

    assert parse_note(path, vault).uuid == UUID


def test_a_non_uuid_value_is_not_silently_accepted(vault: Path):
    """Garbage is treated as absent, so the migration re-mints deterministically."""
    path = _note(vault, "---\nuuid: not-a-uuid\n---\n\n# Hi\n")

    assert parse_note(path, vault).uuid is None


def test_an_empty_uuid_value_is_treated_as_absent(vault: Path):
    path = _note(vault, "---\nuuid:\n---\n\n# Hi\n")

    assert parse_note(path, vault).uuid is None


def test_a_list_valued_uuid_is_treated_as_absent(vault: Path):
    path = _note(vault, "---\nuuid:\n  - a\n  - b\n---\n\n# Hi\n")

    assert parse_note(path, vault).uuid is None


# ---------------------------------------------------------------------------
# Adoption — other Obsidian plugins write other key names
# ---------------------------------------------------------------------------


def test_adopt_keys_includes_the_common_obsidian_uid_plugins():
    assert ADOPT_KEYS[0] == "uuid"
    assert {"uid", "id", "guid"} <= set(ADOPT_KEYS)


def test_a_foreign_key_holding_a_real_uuid_is_adopted_verbatim():
    found, source = read_note_uuid({"id": UUID})

    assert found == UUID
    assert source == "adopted:id"


def test_our_own_key_wins_over_a_foreign_one():
    other = "11111111-2222-4333-8444-555555555555"
    found, source = read_note_uuid({"id": other, "uuid": UUID})

    assert found == UUID
    assert source == "adopted:uuid"


def test_a_foreign_opaque_id_derives_a_stable_uuid():
    """Zettelkasten-style ids aren't UUIDs, but they are stable identity."""
    first, source = read_note_uuid({"id": "202607261200"})
    second, _ = read_note_uuid({"id": "202607261200"})

    assert first == second  # deterministic across machines, no registry needed
    assert source == "derived:id"
    assert first != "202607261200"


def test_different_opaque_ids_derive_different_uuids():
    a, _ = read_note_uuid({"id": "202607261200"})
    b, _ = read_note_uuid({"id": "202607261201"})

    assert a != b


def test_a_too_short_opaque_id_is_not_trusted():
    """A one-character `id: 3` is a field, not an identity."""
    assert read_note_uuid({"id": "3"}) == (None, None)


def test_no_id_keys_at_all_yields_nothing():
    assert read_note_uuid({"title": "Hi"}) == (None, None)


# ---------------------------------------------------------------------------
# The path-derived fallback
# ---------------------------------------------------------------------------


def test_a_path_derived_uuid_is_deterministic():
    assert derive_path_uuid("Projects/Foo.md") == derive_path_uuid("Projects/Foo.md")


def test_path_derived_uuids_differ_by_path():
    assert derive_path_uuid("A.md") != derive_path_uuid("B.md")


def test_a_path_derived_uuid_is_a_valid_canonical_uuid():
    import uuid as uuidlib

    value = derive_path_uuid("Projects/Foo.md")
    assert str(uuidlib.UUID(value)) == value
