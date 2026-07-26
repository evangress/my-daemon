# SPDX-License-Identifier: Apache-2.0
"""The note registry — SQLite as the identity spine.

The registry is *derived* state: frontmatter is the source of truth. Its job is
to answer "what is this uuid's current path", "what lives at this path", and
"how much of the vault is still on a fragile path-derived identity".
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.models import NoteRecord
from my_daemon.stores.registry import NoteRegistry

A = "9f3c1a7e-4b21-4d6f-9c88-1e2a5b0d7f43"
B = "11111111-2222-4333-8444-555555555555"


def test_default_exclude_dirs_match_the_shipped_config():
    """Settings() defaults and config.example.yaml must not drift apart.

    Anything constructing Settings() programmatically — build_core, the Hermes
    provider, tests — would otherwise ingest the daemon's own generated prose
    and its backup copies as if they were the user's notes.
    """
    import yaml

    from my_daemon.config import Settings

    shipped = yaml.safe_load(Path("config.example.yaml").read_text())
    assert set(Settings().vault.exclude_dirs) == set(shipped["vault"]["exclude_dirs"])


@pytest.fixture
def registry(tmp_path: Path) -> NoteRegistry:
    return NoteRegistry(db_path=tmp_path / "state.db")


def _record(uuid: str = A, rel_path: str = "A.md", **kw) -> NoteRecord:
    defaults = dict(
        uuid=uuid,
        rel_path=rel_path,
        title=rel_path.removesuffix(".md"),
        mtime=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        body_sha256="abc123",
        tags=["memory"],
        word_count=42,
        chunk_count=3,
        uuid_source="assigned",
        in_frontmatter=True,
    )
    defaults.update(kw)
    return NoteRecord(**defaults)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_upsert_then_get_round_trips_every_field(registry: NoteRegistry):
    registry.upsert(_record())

    got = registry.get(A)
    assert got is not None
    assert got.rel_path == "A.md"
    assert got.title == "A"
    assert got.tags == ["memory"]
    assert got.word_count == 42
    assert got.chunk_count == 3
    assert got.uuid_source == "assigned"
    assert got.in_frontmatter is True
    assert got.status == "active"


def test_get_returns_none_for_an_unknown_uuid(registry: NoteRegistry):
    assert registry.get(B) is None


def test_upserting_again_updates_the_row_in_place(registry: NoteRegistry):
    registry.upsert(_record())
    registry.upsert(_record(title="Renamed", rel_path="Moved/A.md"))

    got = registry.get(A)
    assert got.title == "Renamed"
    assert got.rel_path == "Moved/A.md"
    assert len(registry.live()) == 1


def test_first_seen_at_survives_an_update(registry: NoteRegistry):
    registry.upsert(_record())
    first_seen = registry.get(A).first_seen_at

    registry.upsert(_record(title="Changed"))

    assert registry.get(A).first_seen_at == first_seen


# ---------------------------------------------------------------------------
# Path lookup
# ---------------------------------------------------------------------------


def test_by_path_finds_the_live_note_there(registry: NoteRegistry):
    registry.upsert(_record())

    assert registry.by_path("A.md").uuid == A


def test_by_path_ignores_a_soft_deleted_note(registry: NoteRegistry):
    registry.upsert(_record())
    registry.soft_delete(A)

    assert registry.by_path("A.md") is None


def test_two_live_notes_cannot_claim_the_same_path(registry: NoteRegistry):
    """The partial unique index catches a duplicate at write time, not read time."""
    registry.upsert(_record(uuid=A, rel_path="A.md"))

    with pytest.raises(sqlite3.IntegrityError):
        registry.upsert(_record(uuid=B, rel_path="A.md"))


def test_a_path_freed_by_a_delete_can_be_reclaimed(registry: NoteRegistry):
    registry.upsert(_record(uuid=A, rel_path="A.md"))
    registry.soft_delete(A)

    registry.upsert(_record(uuid=B, rel_path="A.md"))

    assert registry.by_path("A.md").uuid == B


def test_paths_for_resolves_uuids_in_bulk(registry: NoteRegistry):
    """Reports render paths, never raw uuids — this is that lookup."""
    registry.upsert(_record(uuid=A, rel_path="A.md"))
    registry.upsert(_record(uuid=B, rel_path="B.md"))

    assert registry.paths_for([A, B, "unknown"]) == {A: "A.md", B: "B.md"}


# ---------------------------------------------------------------------------
# Soft delete — the ledger outlives the note
# ---------------------------------------------------------------------------


def test_soft_delete_keeps_the_row_for_history(registry: NoteRegistry):
    registry.upsert(_record())
    registry.soft_delete(A)

    got = registry.get(A)
    assert got is not None
    assert got.deleted_at is not None


def test_live_excludes_soft_deleted_notes(registry: NoteRegistry):
    registry.upsert(_record(uuid=A, rel_path="A.md"))
    registry.upsert(_record(uuid=B, rel_path="B.md"))
    registry.soft_delete(A)

    assert [r.uuid for r in registry.live()] == [B]


def test_forget_hard_deletes_the_row(registry: NoteRegistry):
    """Rollback returns the system to path-only operation — no tombstone left."""
    registry.upsert(_record())

    registry.forget(A)

    assert registry.get(A) is None


def test_forget_is_a_no_op_for_an_unknown_uuid(registry: NoteRegistry):
    registry.forget(B)  # must not raise


def test_reviving_a_deleted_note_clears_the_tombstone(registry: NoteRegistry):
    registry.upsert(_record())
    registry.soft_delete(A)

    registry.upsert(_record())

    assert registry.get(A).deleted_at is None


# ---------------------------------------------------------------------------
# Ordinals — stable small ints for sparse-vector indices
# ---------------------------------------------------------------------------


def test_ordinals_are_allocated_from_one_and_are_stable(registry: NoteRegistry):
    first = registry.ordinal_for(A)
    second = registry.ordinal_for(B)

    assert (first, second) == (1, 2)
    assert registry.ordinal_for(A) == first


def test_an_ordinal_is_never_reused_after_the_note_is_deleted(registry: NoteRegistry):
    """Historical fingerprints keep their meaning only if ordinals are permanent."""
    registry.upsert(_record(uuid=A, rel_path="A.md"))
    a_ordinal = registry.ordinal_for(A)
    registry.soft_delete(A)

    assert registry.ordinal_for(B) != a_ordinal
    assert registry.ordinal_for(A) == a_ordinal


# ---------------------------------------------------------------------------
# Coverage — what `daemon status` reports
# ---------------------------------------------------------------------------


def test_coverage_counts_notes_whose_identity_is_not_rename_stable(
    registry: NoteRegistry,
):
    registry.upsert(_record(uuid=A, rel_path="A.md", in_frontmatter=True))
    registry.upsert(
        _record(
            uuid=B,
            rel_path="B.md",
            in_frontmatter=False,
            uuid_source="derived_path",
            status="ignored",
        )
    )

    coverage = registry.coverage()
    assert coverage.total == 2
    assert coverage.in_frontmatter == 1
    assert coverage.derived_path == 1


def test_coverage_ignores_deleted_notes(registry: NoteRegistry):
    registry.upsert(_record())
    registry.soft_delete(A)

    assert registry.coverage().total == 0
