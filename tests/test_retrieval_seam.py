# SPDX-License-Identifier: Apache-2.0
"""The post-retrieval seam: every surface records, nothing breaks if it can't."""

from __future__ import annotations

from pathlib import Path

from my_daemon.config import Settings
from my_daemon.models import Chunk, RetrievalResult, RetrievedChunk
from my_daemon.pipeline.activation import ActivationRecorder
from my_daemon.retrieval.orchestrator import RetrievalOrchestrator
from my_daemon.retrieval.trace import activations_from
from my_daemon.stores.activations import ActivationLedger

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"


def _chunk(note_uuid: str, chunk_id: str) -> Chunk:
    return Chunk(id=chunk_id, note_uuid=note_uuid, note_path="N.md", text="body", chunk_index=0)


def _result() -> RetrievalResult:
    seed = RetrievedChunk(
        chunk=_chunk(A, "a1"), vector_score=0.9, graph_distance=0, combined_score=0.9
    )
    expanded = RetrievedChunk(
        chunk=_chunk(B, "b1"), graph_distance=1, seed_chunk_id="a1", combined_score=0.4
    )
    return RetrievalResult(query="q", seeds=[seed], expanded=[expanded], ranked=[seed, expanded])


# ---------------------------------------------------------------------------
# Deriving activations
# ---------------------------------------------------------------------------


def test_seeds_and_expansions_are_labelled_by_their_source():
    activations = activations_from(_result())

    assert {(a.note_uuid, a.source) for a in activations} == {
        (A, "vector_seed"),
        (B, "graph_expansion"),
    }


def test_rank_follows_the_ranked_order():
    activations = activations_from(_result())

    assert [a.rank for a in activations] == [1, 2]


def test_an_expansion_records_the_seed_it_came_from():
    activations = activations_from(_result())

    assert next(a for a in activations if a.note_uuid == B).seed_note_uuid == A


def test_chunks_without_an_identity_are_skipped():
    """Pre-cutover points have no note_uuid; they must not poison the ledger."""
    orphan = RetrievedChunk(chunk=_chunk("", "x"), combined_score=0.5)
    result = RetrievalResult(query="q", ranked=[orphan])

    assert activations_from(result) == []


# ---------------------------------------------------------------------------
# The orchestrator seam
# ---------------------------------------------------------------------------


class _Recording:
    def __init__(self) -> None:
        self.traces = []

    def on_retrieval(self, trace) -> str:  # noqa: ANN001
        self.traces.append(trace)
        return "uid-1"


class _Exploding:
    def on_retrieval(self, trace) -> str:  # noqa: ANN001
        raise RuntimeError("ledger is down")


class _FakeVectorStore:
    collection = "chunks"

    def search(self, vector, top_k=8, date_range=None):  # noqa: ANN001
        return []

    def hybrid_search(self, *a, **kw):  # noqa: ANN001
        return []

    def _client_(self):
        class _NoScroll:
            def scroll(self, **kw):  # noqa: ANN001
                return [], None

        return _NoScroll()


class _FakeGraphStore:
    def neighbors_within(self, note_uuid, depth, *, weighted=False):  # noqa: ANN001
        return {}


class _FakeEmbedder:
    dimension = 4

    def encode_one(self, text: str):  # noqa: ANN001
        return [0.0, 0.0, 0.0, 0.0]

    def encode(self, texts):  # noqa: ANN001
        return [[0.0] * 4 for _ in texts]


def _orchestrator(listeners) -> RetrievalOrchestrator:  # noqa: ANN001
    settings = Settings()
    settings.embeddings.hybrid = False
    return RetrievalOrchestrator(
        settings,
        _FakeEmbedder(),
        _FakeVectorStore(),
        _FakeGraphStore(),
        listeners=listeners,
    )


def test_listeners_default_to_empty_so_existing_callers_are_untouched():
    orchestrator = RetrievalOrchestrator(
        Settings(), _FakeEmbedder(), _FakeVectorStore(), _FakeGraphStore()
    )

    assert orchestrator.listeners == []


def test_the_surface_reaches_the_listener():
    listener = _Recording()

    _orchestrator([listener]).retrieve("q", surface="gui")

    assert [t.surface for t in listener.traces] == ["gui"]


def test_a_failing_listener_never_breaks_the_query():
    """A ledger outage must degrade to the daemon's previous behaviour."""
    result = _orchestrator([_Exploding()]).retrieve("q", surface="cli")

    assert result.query == "q"


# ---------------------------------------------------------------------------
# End to end into the ledger
# ---------------------------------------------------------------------------


def test_the_recorder_writes_a_query_and_its_activations(tmp_path: Path):
    from datetime import UTC, datetime

    from my_daemon.retrieval.trace import RetrievalTrace

    ledger = ActivationLedger(db_path=tmp_path / "state.db")
    recorder = ActivationRecorder(ledger)
    result = _result()

    query_uid = recorder.on_retrieval(
        RetrievalTrace(
            query="q",
            surface="cli",
            started_at=datetime.now(UTC),
            latency_ms=12,
            result=result,
            activations=activations_from(result),
        )
    )

    stored = ledger.get(query_uid)
    assert stored["surface"] == "cli"
    assert stored["activation_count"] == 2


def test_the_recorder_is_a_no_op_for_an_empty_retrieval(tmp_path: Path):
    from datetime import UTC, datetime

    from my_daemon.retrieval.trace import RetrievalTrace

    ledger = ActivationLedger(db_path=tmp_path / "state.db")

    query_uid = ActivationRecorder(ledger).on_retrieval(
        RetrievalTrace(
            query="q",
            surface="cli",
            started_at=datetime.now(UTC),
            latency_ms=1,
            result=RetrievalResult(query="q"),
        )
    )

    assert query_uid is None
    assert ledger.total_queries() == 0
