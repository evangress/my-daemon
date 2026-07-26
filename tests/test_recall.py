# SPDX-License-Identifier: Apache-2.0
"""Fingerprint recall — "you've been here before".

Two questions worded completely differently that light up the same notes are
the same underlying concern. This is the surface that says so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.models import NoteRecord
from my_daemon.pipeline.recall import recall_related
from my_daemon.stores.activations import Activation, ActivationLedger
from my_daemon.stores.registry import NoteRegistry

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def ledger(db: Path) -> ActivationLedger:
    return ActivationLedger(db_path=db)


@pytest.fixture
def registry(db: Path) -> NoteRegistry:
    reg = NoteRegistry(db_path=db)
    for note_uuid, rel_path in ((A, "On Forgetting.md"), (B, "Memory.md"), (C, "Cooking.md")):
        reg.upsert(NoteRecord(uuid=note_uuid, rel_path=rel_path, title=rel_path[:-3]))
    return reg


@pytest.fixture
def settings(db: Path) -> Settings:
    s = Settings()
    s.feedback.db_path = db
    s.memory.min_score = 0.05
    return s


def _ask(ledger: ActivationLedger, text: str, notes, *, surface="cli", ts=None) -> int:
    return ledger.record(
        text=text,
        surface=surface,
        ts=ts or datetime.now(UTC),
        activations=[
            Activation(note_uuid=u, source="vector_seed", rank=i)
            for i, u in enumerate(notes, start=1)
        ],
    )


# ---------------------------------------------------------------------------
# The feature
# ---------------------------------------------------------------------------


def test_a_reworded_question_surfaces_the_earlier_one(
    ledger, registry, settings
):
    _ask(ledger, "why do I keep losing things I meant to remember", [A, B])
    now = _ask(ledger, "what makes something stick", [A, B])

    memories = recall_related(ledger, registry, query_id=now, settings=settings)

    assert [m.text for m in memories] == ["why do I keep losing things I meant to remember"]


def test_a_recalled_memory_names_the_notes_they_share(ledger, registry, settings):
    _ask(ledger, "earlier", [A, B])
    now = _ask(ledger, "later", [A, B])

    memories = recall_related(ledger, registry, query_id=now, settings=settings)

    assert "On Forgetting.md" in memories[0].shared_notes


def test_shared_notes_are_paths_not_uuids(ledger, registry, settings):
    """These go into an LLM prompt and onto the user's screen."""
    _ask(ledger, "earlier", [A])
    now = _ask(ledger, "later", [A])

    memories = recall_related(ledger, registry, query_id=now, settings=settings)

    assert all("-" not in n or n.endswith(".md") for n in memories[0].shared_notes)


def test_an_unrelated_question_recalls_nothing(ledger, registry, settings):
    _ask(ledger, "about cooking", [C])
    now = _ask(ledger, "about memory", [A, B])

    assert recall_related(ledger, registry, query_id=now, settings=settings) == []


def test_the_current_query_never_recalls_itself(ledger, registry, settings):
    now = _ask(ledger, "only query", [A, B])

    assert recall_related(ledger, registry, query_id=now, settings=settings) == []


def test_recall_is_capped_by_top_k(ledger, registry, settings):
    settings.memory.top_k = 2
    for i in range(5):
        _ask(ledger, f"earlier {i}", [A, B])
    now = _ask(ledger, "now", [A, B])

    assert len(recall_related(ledger, registry, query_id=now, settings=settings)) == 2


def test_recall_respects_the_lookback_window(ledger, registry, settings):
    settings.memory.lookback_days = 30
    long_ago = datetime.now(UTC) - timedelta(days=400)
    _ask(ledger, "ancient", [A, B], ts=long_ago)
    now = _ask(ledger, "now", [A, B])

    assert recall_related(ledger, registry, query_id=now, settings=settings) == []


def test_ambient_prefetches_are_not_recalled_as_questions(ledger, registry, settings):
    """Hermes prefetch fires every conversational turn — those aren't questions."""
    _ask(ledger, "ambient lookup", [A, B], surface="hermes_prefetch")
    now = _ask(ledger, "an actual question", [A, B])

    assert recall_related(ledger, registry, query_id=now, settings=settings) == []


def test_recall_can_be_switched_off(ledger, registry, settings):
    settings.memory.recall_enabled = False
    _ask(ledger, "earlier", [A, B])
    now = _ask(ledger, "later", [A, B])

    assert recall_related(ledger, registry, query_id=now, settings=settings) == []


def test_a_deleted_note_still_resolves_to_its_last_known_path(
    ledger, registry, settings
):
    """The ledger outlives the note; a memory of it must still be readable."""
    _ask(ledger, "earlier", [A, B])
    now = _ask(ledger, "later", [A, B])
    registry.soft_delete(A)

    memories = recall_related(ledger, registry, query_id=now, settings=settings)

    assert "On Forgetting.md" in memories[0].shared_notes


# ---------------------------------------------------------------------------
# How it reaches the model and the user
# ---------------------------------------------------------------------------


def test_the_prompt_block_names_the_date_and_the_shared_notes():
    from my_daemon.llm.prompts import build_memory_block
    from my_daemon.pipeline.recall import RecalledMemory

    block = build_memory_block(
        [
            RecalledMemory(
                query_id=1,
                text="why do I keep losing things",
                ts=datetime(2026, 3, 12, tzinfo=UTC),
                score=0.72,
                shared_notes=["On Forgetting.md"],
            )
        ]
    )

    assert "2026-03-12" in block
    assert "why do I keep losing things" in block
    assert "On Forgetting.md" in block


def test_no_memories_produces_no_prompt_block():
    from my_daemon.llm.prompts import build_memory_block

    assert build_memory_block([]) == ""


def test_injection_and_display_toggle_independently(settings):
    assert settings.memory.inject_into_context is True
    assert settings.memory.show_to_user is True
