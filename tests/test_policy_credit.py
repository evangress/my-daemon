# SPDX-License-Identifier: Apache-2.0
"""Credit assignment: a pick reaches the policy ledger, seeds included.

The bug this closes is the one the maturity evaluation left open. Before
interleaving, `apply_selection` no-opped whenever the seed *was* the selection
— the overwhelmingly common case for a dense hit — so the most confident signal
the user can give the system taught it nothing at all. The graph still cannot
be reinforced along a zero-length path; what changes is that the pick is now
recorded as a win for the ranking that surfaced it.
"""

from __future__ import annotations

from pathlib import Path

from my_daemon.models import Chunk, RetrievalResult, RetrievedChunk
from my_daemon.pipeline.policy import PolicyRecorder
from my_daemon.pipeline.query import build_retrieval_summary
from my_daemon.retrieval.interleave import EXPANSION_TEAM, SEED_TEAM
from my_daemon.retrieval.trace import RetrievalTrace
from my_daemon.stores.policy import RetrievalPolicyStore

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"


def _chunk(note_uuid: str, chunk_id: str) -> Chunk:
    return Chunk(
        id=chunk_id, note_uuid=note_uuid, note_path=f"{chunk_id}.md", text="body", chunk_index=0
    )


def _drafted_result() -> RetrievalResult:
    seed = RetrievedChunk(
        chunk=_chunk(A, "a1"),
        vector_score=0.9,
        graph_distance=0,
        combined_score=0.9,
        team=SEED_TEAM,
    )
    expansion = RetrievedChunk(
        chunk=_chunk(B, "b1"),
        graph_distance=1.0,
        seed_chunk_id="a1",
        combined_score=0.4,
        team=EXPANSION_TEAM,
    )
    return RetrievalResult(
        query="q",
        seeds=[seed.model_copy(update={"team": None})],
        expanded=[expansion.model_copy(update={"team": None})],
        ranked=[seed, expansion],
    )


# ---------------------------------------------------------------------------
# The team has to survive the round trip through the feedback row
# ---------------------------------------------------------------------------


def test_the_retrieval_summary_carries_each_candidate_team():
    """`daemon select` runs long after the retrieval, from the stored summary.
    If the team is not persisted there, the attribution is gone."""

    summary = build_retrieval_summary(_drafted_result())

    assert [row["team"] for row in summary["ranked"]] == [SEED_TEAM, EXPANSION_TEAM]


def test_a_summary_from_a_score_ordered_pool_has_no_team():
    result = _drafted_result()
    result.ranked = [rc.model_copy(update={"team": None}) for rc in result.ranked]

    summary = build_retrieval_summary(result)

    assert all(row["team"] is None for row in summary["ranked"])


# ---------------------------------------------------------------------------
# Impressions
# ---------------------------------------------------------------------------


def test_the_recorder_counts_one_impression_per_drafted_candidate(tmp_path: Path):
    store = RetrievalPolicyStore(db_path=tmp_path / "state.db")
    from datetime import UTC, datetime

    PolicyRecorder(store).on_retrieval(
        RetrievalTrace(
            query="q",
            surface="cli",
            started_at=datetime.now(UTC),
            latency_ms=1,
            result=_drafted_result(),
        )
    )

    assert store.get(SEED_TEAM).impressions == 1
    assert store.get(EXPANSION_TEAM).impressions == 1


def test_the_recorder_ignores_a_pool_with_no_attribution(tmp_path: Path):
    """Interleaving off means no comparison was run, so there is nothing to
    count — recording it would treat score order as if it were a fair draft."""

    from datetime import UTC, datetime

    store = RetrievalPolicyStore(db_path=tmp_path / "state.db")
    result = _drafted_result()
    result.ranked = [rc.model_copy(update={"team": None}) for rc in result.ranked]

    PolicyRecorder(store).on_retrieval(
        RetrievalTrace(
            query="q",
            surface="cli",
            started_at=datetime.now(UTC),
            latency_ms=1,
            result=result,
        )
    )

    assert store.stats() == []


def test_the_recorder_returns_no_query_uid(tmp_path: Path):
    """Only the activation recorder owns the query_uid; a second listener
    claiming one would race it."""

    from datetime import UTC, datetime

    store = RetrievalPolicyStore(db_path=tmp_path / "state.db")

    returned = PolicyRecorder(store).on_retrieval(
        RetrievalTrace(
            query="q",
            surface="cli",
            started_at=datetime.now(UTC),
            latency_ms=1,
            result=_drafted_result(),
        )
    )

    assert returned is None


# ---------------------------------------------------------------------------
# Endorsement — the point of the whole exercise
# ---------------------------------------------------------------------------


def _endorse_fixture(tmp_path: Path, *, team: str | None):
    """A logged answer whose single candidate is its own seed.

    That is the shape `apply_selection` cannot reinforce — a zero-length path —
    and therefore the shape that used to discard the signal entirely.
    """

    from my_daemon.config import FeedbackConfig, GraphConfig, Settings
    from my_daemon.models import FeedbackEvent
    from my_daemon.stores import FeedbackStore

    settings = Settings()
    settings.graph = GraphConfig(
        path=tmp_path / "graph.gpickle", manifest_path=tmp_path / "manifest.json"
    )
    settings.feedback = FeedbackConfig(db_path=tmp_path / "state.db")

    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    from datetime import UTC, datetime

    event_id = feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="q",
            answer="a",
            latency_ms=1,
            retrieval_summary={
                "ranked": [
                    {
                        "chunk_id": "a1",
                        "note_uuid": A,
                        "note_path": "A.md",
                        "seed_note_uuid": A,
                        "team": team,
                    }
                ]
            },
        )
    )
    return settings, feedback, event_id


def test_endorsing_a_seed_pick_records_a_win_although_no_edge_moves(tmp_path: Path):
    """The regression that mattered: seed == selection reinforces nothing, and
    before this the signal simply vanished."""

    from my_daemon.integration.core import DaemonCore
    from my_daemon.stores import GraphStore

    settings, feedback, event_id = _endorse_fixture(tmp_path, team=SEED_TEAM)
    core = DaemonCore(
        settings,
        embedder=None,
        vector_store=None,
        graph_store=GraphStore(path=settings.graph.path),
        feedback_store=feedback,
        llm_client=None,
    )

    result = core.endorse(event_id, 1, require_write_back=False)

    assert result["ok"] is True
    assert result["edges_reinforced"] == 0
    assert RetrievalPolicyStore(db_path=settings.feedback.db_path).get(SEED_TEAM).wins == 1


def test_endorsing_an_unattributed_pick_records_no_win(tmp_path: Path):
    """A pick from a score-ordered pool is confounded by position and must not
    be counted as a policy comparison."""

    from my_daemon.integration.core import DaemonCore
    from my_daemon.stores import GraphStore

    settings, feedback, event_id = _endorse_fixture(tmp_path, team=None)
    core = DaemonCore(
        settings,
        embedder=None,
        vector_store=None,
        graph_store=GraphStore(path=settings.graph.path),
        feedback_store=feedback,
        llm_client=None,
    )

    core.endorse(event_id, 1, require_write_back=False)

    assert RetrievalPolicyStore(db_path=settings.feedback.db_path).stats() == []
