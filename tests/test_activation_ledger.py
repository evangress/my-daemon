# SPDX-License-Identifier: Apache-2.0
"""The activation ledger — which notes fired for which query, and how strongly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_daemon.stores.activations import Activation, ActivationLedger

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"


@pytest.fixture
def ledger(tmp_path: Path) -> ActivationLedger:
    return ActivationLedger(db_path=tmp_path / "state.db")


def _record(ledger: ActivationLedger, text: str, notes, *, surface="cli", ts=None) -> int:
    """`notes` is [(uuid, source, rank), ...]."""
    return ledger.record(
        query_uid=f"uid-{text}",
        text=text,
        surface=surface,
        ts=ts or datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        activations=[
            Activation(note_uuid=u, source=src, rank=rank) for u, src, rank in notes
        ],
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_recording_a_query_returns_its_row_id(ledger: ActivationLedger):
    query_id = _record(ledger, "why do I keep coming back to this", [(A, "vector_seed", 1)])

    assert query_id > 0


def test_activations_are_readable_back(ledger: ActivationLedger):
    query_id = _record(
        ledger, "q", [(A, "vector_seed", 1), (B, "graph_expansion", 2)]
    )

    rows = ledger.activations_for(query_id)

    assert {(r.note_uuid, r.source) for r in rows} == {
        (A, "vector_seed"),
        (B, "graph_expansion"),
    }


def test_one_note_can_fire_via_two_sources_in_one_query(ledger: ActivationLedger):
    """Seed and expansion are different evidence — both belong in the ledger."""
    query_id = _record(ledger, "q", [(A, "vector_seed", 1), (A, "graph_expansion", 4)])

    assert len(ledger.activations_for(query_id)) == 2


def test_a_seed_hit_outranks_a_distant_expansion(ledger: ActivationLedger):
    query_id = _record(ledger, "q", [(A, "vector_seed", 1), (B, "graph_expansion", 8)])

    by_note = {r.note_uuid: r.strength for r in ledger.activations_for(query_id)}
    assert by_note[A] > by_note[B]


def test_recording_updates_the_document_frequency_table(ledger: ActivationLedger):
    _record(ledger, "q1", [(A, "vector_seed", 1)])
    _record(ledger, "q2", [(A, "vector_seed", 1), (B, "vector_seed", 2)])

    assert ledger.note_df([A, B, C]) == {A: 2, B: 1, C: 0}


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_a_fingerprint_is_l2_normalized(ledger: ActivationLedger):
    query_id = _record(ledger, "q", [(A, "vector_seed", 1), (B, "vector_seed", 2)])

    fingerprint = ledger.fingerprint(query_id)

    magnitude = sum(v * v for v in fingerprint.values()) ** 0.5
    assert magnitude == pytest.approx(1.0)


def test_a_note_in_every_query_is_damped_by_idf(ledger: ActivationLedger):
    """Without IDF, a hub note graph expansion drags in everywhere would make
    every fingerprint look alike and the whole feature would collapse."""
    for i in range(6):
        _record(ledger, f"q{i}", [(A, "vector_seed", 1), (B, "vector_seed", 1)])
    distinctive = _record(ledger, "rare", [(A, "vector_seed", 1), (C, "vector_seed", 1)])

    fingerprint = ledger.fingerprint(distinctive)

    assert fingerprint[C] > fingerprint[A]


# ---------------------------------------------------------------------------
# Similarity — the point of the whole thing
# ---------------------------------------------------------------------------


def test_a_reworded_query_matches_on_activations_not_words(ledger: ActivationLedger):
    first = _record(ledger, "what did I think about forgetting", [(A, "vector_seed", 1), (B, "vector_seed", 2)])
    _record(ledger, "noise", [(C, "vector_seed", 1)])
    probe = {A: 0.8, B: 0.6}

    hits = ledger.similar(probe, top_k=5, min_score=0.1)

    assert hits and hits[0].query_id == first


def test_similar_excludes_the_probe_query_itself(ledger: ActivationLedger):
    query_id = _record(ledger, "q", [(A, "vector_seed", 1)])

    hits = ledger.similar(ledger.fingerprint(query_id), exclude_query_id=query_id)

    assert [h.query_id for h in hits] == []


def test_similar_reports_the_notes_the_two_queries_share(ledger: ActivationLedger):
    _record(ledger, "earlier", [(A, "vector_seed", 1), (B, "vector_seed", 2)])

    hits = ledger.similar({A: 1.0}, min_score=0.1)

    assert A in hits[0].shared_notes


def test_similar_honours_a_minimum_score(ledger: ActivationLedger):
    _record(ledger, "earlier", [(A, "vector_seed", 1)])

    assert ledger.similar({B: 1.0}, min_score=0.1) == []


def test_similar_can_be_scoped_to_a_time_window(ledger: ActivationLedger):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    _record(ledger, "ancient", [(A, "vector_seed", 1)], ts=old)

    hits = ledger.similar({A: 1.0}, since=datetime.now(UTC) - timedelta(days=30))

    assert hits == []


def test_similar_can_be_scoped_to_intentional_surfaces(ledger: ActivationLedger):
    """Hermes prefetch fires on every conversational turn; those are ambient,
    not questions the user asked, and would otherwise dominate the space."""
    _record(ledger, "ambient", [(A, "vector_seed", 1)], surface="hermes_prefetch")
    deliberate = _record(ledger, "asked", [(A, "vector_seed", 1)], surface="cli")

    hits = ledger.similar({A: 1.0}, surfaces=("cli", "gui"), min_score=0.1)

    assert [h.query_id for h in hits] == [deliberate]


def test_a_hub_note_is_dropped_from_the_probe(ledger: ActivationLedger):
    """Above `max_df_ratio` a note carries no information and only costs scan."""
    for i in range(60):
        _record(ledger, f"q{i}", [(A, "vector_seed", 1)])

    assert ledger.similar({A: 1.0}, max_df_ratio=0.25, min_score=0.01) == []


def test_hub_pruning_stays_off_on_a_small_corpus(ledger: ActivationLedger):
    """Otherwise every probe returns nothing for the first weeks of use."""
    _record(ledger, "earlier", [(A, "vector_seed", 1)])

    assert ledger.similar({A: 1.0}, max_df_ratio=0.25, min_score=0.01)


# ---------------------------------------------------------------------------
# Linking back to the answer record
# ---------------------------------------------------------------------------


def test_a_query_can_be_linked_to_its_feedback_row(ledger: ActivationLedger, tmp_path: Path):
    from my_daemon.models import FeedbackEvent
    from my_daemon.stores import FeedbackStore

    feedback = FeedbackStore(db_path=tmp_path / "state.db")
    event_id = feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="q",
            retrieval_summary={},
            answer="a",
            latency_ms=1,
        )
    )
    _record(ledger, "q", [(A, "vector_seed", 1)])

    ledger.link_feedback("uid-q", event_id)

    assert ledger.get("uid-q")["feedback_id"] == event_id


# ---------------------------------------------------------------------------
# What the user can see
# ---------------------------------------------------------------------------


def test_hot_notes_ranks_what_attention_actually_lands_on(ledger: ActivationLedger):
    _record(ledger, "q1", [(A, "vector_seed", 1)])
    _record(ledger, "q2", [(A, "vector_seed", 1)])
    _record(ledger, "q3", [(B, "vector_seed", 1)])

    hot = ledger.hot_notes(limit=5)

    assert [n for n, _count, _strength in hot][:2] == [A, B]
