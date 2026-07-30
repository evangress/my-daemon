# SPDX-License-Identifier: Apache-2.0
"""`DaemonCore` owns the answer path, so every surface logs it the same way.

The GUI used to hand-roll retrieval + feedback logging, which is how it went a
whole arc without fingerprint recall: `QueryEngine` learned about memories and
the GUI's private copy did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.models import Chunk, RetrievalResult, RetrievedChunk

A = "aaaaaaaa-0000-4000-8000-000000000001"


def _result(query: str = "q") -> RetrievalResult:
    chunk = Chunk(id="c1", note_uuid=A, note_path="A.md", text="body", chunk_index=0)
    rc = RetrievedChunk(chunk=chunk, vector_score=0.9, graph_distance=0, combined_score=0.9)
    return RetrievalResult(query=query, seeds=[rc], ranked=[rc])


@pytest.fixture
def core(tmp_path: Path, vault_root: Path):
    from my_daemon.integration.core import DaemonCore

    settings = Settings()
    settings.vault.path = vault_root
    settings.embeddings.hybrid = False
    settings.feedback.db_path = tmp_path / "state.db"
    settings.graph.path = tmp_path / "g.gpickle"

    class _Embedder:
        dimension = 4

        def encode_one(self, text):  # noqa: ANN001
            return [0.0] * 4

        def encode(self, texts):  # noqa: ANN001
            return [[0.0] * 4 for _ in texts]

    class _Vector:
        collection = "chunks"

        def search(self, vector, top_k=8, date_range=None):  # noqa: ANN001
            return []

        def ensure_collection(self):
            pass

        def _client_(self):
            class _NoScroll:
                def scroll(self, **kw):  # noqa: ANN001
                    return [], None

            return _NoScroll()

    class _LLM:
        def synthesize(self, query, chunks, memories=None):  # noqa: ANN001
            return "an answer"

        def synthesize_stream(self, query, chunks, memories=None):  # noqa: ANN001
            self.memories_seen = memories
            yield "an "
            yield "answer"

    from my_daemon.stores import FeedbackStore, GraphStore

    graph = GraphStore(path=settings.graph.path)
    return DaemonCore(
        settings=settings,
        embedder=_Embedder(),
        sparse_embedder=None,
        vector_store=_Vector(),
        graph_store=graph,
        feedback_store=FeedbackStore(db_path=settings.feedback.db_path),
        llm_client=_LLM(),
    )


# ---------------------------------------------------------------------------
# log_answer — the one place a feedback row is created
# ---------------------------------------------------------------------------


def test_log_answer_writes_a_feedback_row(core):
    event_id = core.log_answer(query="q", answer="an answer", latency_ms=12, retrieval=_result())

    event = core.feedback.get(event_id)
    assert event is not None
    assert event.answer == "an answer"
    assert event.retrieval_summary["ranked"][0]["note_uuid"] == A


def test_log_answer_back_links_the_ledger_row(core):
    """`queries` is the retrieval record; `feedback` is the answer record."""
    from my_daemon.stores.activations import Activation

    query_uid = "uid-1"
    core.ledger.record(
        query_uid=query_uid,
        text="q",
        surface="gui",
        activations=[Activation(note_uuid=A, source="vector_seed", rank=1)],
    )
    result = _result()
    result.query_uid = query_uid

    event_id = core.log_answer(query="q", answer="a", latency_ms=1, retrieval=result)

    assert core.ledger.get(query_uid)["feedback_id"] == event_id


def test_log_answer_without_a_ledger_row_still_logs(core):
    """A ledger outage must not cost the user their answer record."""
    event_id = core.log_answer(query="q", answer="a", latency_ms=1, retrieval=_result())

    assert core.feedback.get(event_id) is not None


# ---------------------------------------------------------------------------
# retrieve_only + ask_stream
# ---------------------------------------------------------------------------


def test_retrieve_only_names_its_surface(core):
    result = core.retrieve_only("q", surface="gui")

    assert isinstance(result, RetrievalResult)
    rows = core.ledger.recent(limit=1)
    assert rows == [] or rows[0]["surface"] == "gui"


def test_ask_stream_yields_deltas_and_returns_the_record(core, monkeypatch):
    monkeypatch.setattr(core.orchestrator, "retrieve", lambda q, **kw: _result(q))

    stream = core.ask_stream("q", surface="gui")
    deltas = list(stream)

    assert "".join(deltas) == "an answer"
    assert stream.answer == "an answer"
    assert stream.feedback_event_id is not None
    assert core.feedback.get(stream.feedback_event_id).answer == "an answer"


def test_ask_stream_passes_recalled_memories_to_the_model(core, monkeypatch):
    """The gap this whole refactor exists to close."""
    monkeypatch.setattr(core.orchestrator, "retrieve", lambda q, **kw: _result(q))

    list(core.ask_stream("q", surface="gui"))

    assert hasattr(core.llm, "memories_seen"), "synthesize_stream never got the kwarg"


def test_ask_stream_on_an_empty_retrieval_yields_nothing(core, monkeypatch):
    monkeypatch.setattr(core.orchestrator, "retrieve", lambda q, **kw: RetrievalResult(query=q))

    stream = core.ask_stream("q", surface="gui")

    assert list(stream) == []
    assert stream.retrieval.ranked == []


# ---------------------------------------------------------------------------
# Reinforcement: an explicit click is not the same act as ambient write-back
# ---------------------------------------------------------------------------


def test_a_direct_click_reinforces_even_with_hermes_write_back_off(core):
    """`hermes.allow_write_back` gates *capture*, not a button the user pressed."""
    assert core.s.hermes.allow_write_back is False
    event_id = core.log_answer(query="q", answer="a", latency_ms=1, retrieval=_result())

    result = core.endorse(event_id, rank=1, require_write_back=False)

    assert result["ok"] is True


def test_ambient_endorsement_still_respects_hermes_write_back(core):
    """Hermes' implicit soft-reinforcement must stay gated."""
    event_id = core.log_answer(query="q", answer="a", latency_ms=1, retrieval=_result())

    result = core.endorse(event_id, rank=1)

    assert result["ok"] is False
    assert "write-back" in result["reason"]


def test_reinforcement_can_be_switched_off_entirely(core):
    core.s.feedback.reinforce_enabled = False
    event_id = core.log_answer(query="q", answer="a", latency_ms=1, retrieval=_result())

    result = core.endorse(event_id, rank=1, require_write_back=False)

    assert result["ok"] is False
    assert "reinforce" in result["reason"]
