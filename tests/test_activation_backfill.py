# SPDX-License-Identifier: Apache-2.0
"""Backfill the activation ledger from historical `feedback.retrieval_summary`.

Every query the daemon ever answered already recorded its ranked candidates.
That is a proto-activation record, and it is free signal — but only where the
notes it names can still be resolved to identities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_daemon.models import FeedbackEvent, NoteRecord
from my_daemon.pipeline.backfill import backfill_activations
from my_daemon.stores import FeedbackStore
from my_daemon.stores.activations import ActivationLedger
from my_daemon.stores.registry import NoteRegistry

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def registry(db: Path) -> NoteRegistry:
    reg = NoteRegistry(db_path=db)
    reg.upsert(NoteRecord(uuid=A, rel_path="A.md", title="A"))
    reg.upsert(NoteRecord(uuid=B, rel_path="B.md", title="B"))
    return reg


def _log(feedback: FeedbackStore, query: str, ranked, *, ts=None) -> int:
    return feedback.log(
        FeedbackEvent(
            timestamp=ts or datetime.now(UTC),
            query=query,
            retrieval_summary={"ranked": ranked, "seed_count": 1, "expanded_count": 0},
            answer="an answer",
            latency_ms=10,
        )
    )


def _row(note_path: str, *, distance=0, chunk_id="c1") -> dict:
    """A pre-cutover summary row: paths only, no identities."""
    return {
        "chunk_id": chunk_id,
        "note_path": note_path,
        "heading_path": [],
        "score": 0.8,
        "vector_score": 0.8 if distance == 0 else None,
        "graph_distance": distance,
        "seed_chunk_id": "c1",
        "seed_note_path": "A.md",
    }


def test_a_historical_query_becomes_a_ledger_row(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "what did I think about forgetting", [_row("A.md")])

    report = backfill_activations(db, registry, dry_run=False)

    assert report.queries_written == 1
    assert ActivationLedger(db_path=db).total_queries() == 1


def test_paths_are_resolved_to_identities(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("A.md"), _row("B.md", distance=1, chunk_id="c2")])

    backfill_activations(db, registry, dry_run=False)

    ledger = ActivationLedger(db_path=db)
    rows = ledger.activations_for(1)
    assert {r.note_uuid for r in rows} == {A, B}


def test_the_source_is_inferred_from_graph_distance(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("A.md"), _row("B.md", distance=2, chunk_id="c2")])

    backfill_activations(db, registry, dry_run=False)

    by_note = {r.note_uuid: r.source for r in ActivationLedger(db_path=db).activations_for(1)}
    assert by_note[A] == "vector_seed"
    assert by_note[B] == "graph_expansion"


def test_backfilled_rows_are_marked_as_such(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("A.md")])

    backfill_activations(db, registry, dry_run=False)

    assert ActivationLedger(db_path=db).recent()[0]["origin"] == "backfill"


def test_a_backfilled_query_links_to_its_feedback_row(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    event_id = _log(feedback, "q", [_row("A.md")])

    backfill_activations(db, registry, dry_run=False)

    assert ActivationLedger(db_path=db).recent()[0]["feedback_id"] == event_id


def test_an_unresolvable_note_is_dropped_and_counted(db: Path, registry: NoteRegistry):
    """A note renamed or deleted since has no identity to attach to."""
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("A.md"), _row("Gone.md", chunk_id="c2")])

    report = backfill_activations(db, registry, dry_run=False)

    assert report.activations_dropped == 1
    assert len(ActivationLedger(db_path=db).activations_for(1)) == 1


def test_a_high_drop_rate_is_surfaced(db: Path, registry: NoteRegistry):
    """Partial fingerprints look less similar to everything than they should."""
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("Gone.md"), _row("AlsoGone.md", chunk_id="c2"), _row("A.md", chunk_id="c3")])

    report = backfill_activations(db, registry, dry_run=False)

    assert report.drop_rate > 0.6
    assert report.warning


def test_a_dry_run_writes_nothing(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("A.md")])

    report = backfill_activations(db, registry, dry_run=True)

    assert report.queries_written == 1  # what it *would* write
    assert ActivationLedger(db_path=db).total_queries() == 0


def test_re_running_does_not_duplicate(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("A.md")])
    backfill_activations(db, registry, dry_run=False)

    report = backfill_activations(db, registry, dry_run=False)

    assert report.queries_written == 0
    assert ActivationLedger(db_path=db).total_queries() == 1


def test_a_query_with_no_resolvable_notes_is_skipped_entirely(
    db: Path, registry: NoteRegistry
):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "q", [_row("Gone.md")])

    report = backfill_activations(db, registry, dry_run=False)

    assert report.queries_written == 0
    assert ActivationLedger(db_path=db).total_queries() == 0


def test_a_since_cutoff_bounds_the_run(db: Path, registry: NoteRegistry):
    feedback = FeedbackStore(db_path=db)
    _log(feedback, "ancient", [_row("A.md")], ts=datetime.now(UTC) - timedelta(days=400))
    _log(feedback, "recent", [_row("A.md")])

    report = backfill_activations(
        db, registry, dry_run=False, since=datetime.now(UTC) - timedelta(days=30)
    )

    assert report.queries_written == 1
