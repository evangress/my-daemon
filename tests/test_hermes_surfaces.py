# SPDX-License-Identifier: Apache-2.0
"""Hermes has two retrieval paths, and they are not the same act.

`prefetch` fires on *every* conversational turn — ambient. The `mydaemon_recall`
tool is the agent deliberately looking something up on the user's behalf —
intentional. Classifying both as intentional (which is what shipped) lets
ambient lookups dominate the fingerprint space, so themes become
themes-of-Hermes-turns, and floods the observer/reflect prompts with
answer-less rows.

These tests pin the separation at every point it has to hold: the ledger row's
surface string, the fingerprint/recall/clustering inputs, and the two prompt
builders that render "recent chats".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from my_daemon.config import (
    FeedbackConfig,
    GraphConfig,
    HermesConfig,
    LLMConfig,
    Settings,
)
from my_daemon.hermes.provider import MyDaemonProvider
from my_daemon.integration.core import DaemonCore
from my_daemon.models import FeedbackEvent
from my_daemon.stores import FeedbackStore, GraphStore
from my_daemon.stores.activations import INTENTIONAL_SURFACES

NOTE_UUID = "aaaaaaaa-0000-4000-8000-000000000001"

# ---------------------------------------------------------------------------
# fakes — one vector hit, no Qdrant, no Anthropic
# ---------------------------------------------------------------------------


class FakeEmbedder:
    dimension = 4

    def encode_one(self, text: str) -> list[float]:
        return [0.0] * 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


class FakeVectorStore:
    """Returns exactly one hit, so every retrieval produces one activation."""

    collection = "chunks"

    def search(self, vector, top_k: int = 8) -> list[dict]:  # noqa: ANN001
        return [
            {
                "chunk_id": "c1",
                "note_uuid": NOTE_UUID,
                "note_path": "Pullman Daemons.md",
                "heading_path": ["Daemons"],
                "text": "A daemon is an external manifestation of the soul.",
                "chunk_index": 0,
                "score": 0.9,
            }
        ]

    def ensure_collection(self) -> None:
        pass

    def _client_(self):
        class _NoScroll:
            def scroll(self, **kw):  # noqa: ANN003
                return [], None

        return _NoScroll()


class FakeLLM:
    """Enough of an LLMClient for the agents' model-picking, and nothing more."""

    def __init__(self) -> None:
        self.config = LLMConfig(
            model="claude-sonnet-4-6", batch_model="claude-haiku-4-5", temperature=None
        )

    def synthesize(self, query, chunks, memories=None) -> str:  # noqa: ANN001
        raise AssertionError("recall must not synthesize")


def _settings(tmp_path: Path, vault: Path) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.graph = GraphConfig(
        path=tmp_path / "graph.gpickle", manifest_path=tmp_path / "manifest.json"
    )
    s.feedback = FeedbackConfig(db_path=tmp_path / "state.db")
    s.hermes = HermesConfig(provider_enabled=True, allow_write_back=False, recall_top_k=8)
    return s


def _core(settings: Settings) -> DaemonCore:
    return DaemonCore(
        settings,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        graph_store=GraphStore(path=settings.graph.path),
        feedback_store=FeedbackStore(db_path=settings.feedback.db_path),
        llm_client=FakeLLM(),
    )


def _provider(tmp_path: Path, vault: Path) -> tuple[MyDaemonProvider, DaemonCore]:
    settings = _settings(tmp_path, vault)
    core = _core(settings)
    return MyDaemonProvider(settings=settings, core=core), core


# ---------------------------------------------------------------------------
# the surface constants themselves
# ---------------------------------------------------------------------------


def test_prefetch_surface_is_not_intentional() -> None:
    from my_daemon.stores.activations import AMBIENT_SURFACES, HERMES_PREFETCH_SURFACE

    assert HERMES_PREFETCH_SURFACE == "hermes_prefetch"
    assert HERMES_PREFETCH_SURFACE not in INTENTIONAL_SURFACES
    assert HERMES_PREFETCH_SURFACE in AMBIENT_SURFACES
    # The deliberate tool stays intentional — the user's agent asked on purpose.
    assert "hermes_recall" in INTENTIONAL_SURFACES
    assert INTENTIONAL_SURFACES == ("cli", "gui", "hermes_recall", "mcp")


# ---------------------------------------------------------------------------
# the ledger row each path writes
# ---------------------------------------------------------------------------


def test_prefetch_writes_the_ambient_surface(tmp_path: Path, vault_root: Path) -> None:
    provider, core = _provider(tmp_path, vault_root)

    provider.prefetch("what is a daemon", session_id="s1")

    rows = core.ledger.recent(limit=5)
    assert [r["surface"] for r in rows] == ["hermes_prefetch"]
    assert [r["text"] for r in rows] == ["what is a daemon"]


def test_recall_tool_writes_the_intentional_surface(
    tmp_path: Path, vault_root: Path
) -> None:
    provider, core = _provider(tmp_path, vault_root)

    provider.handle_tool_call("mydaemon_recall", {"query": "what is a daemon"})

    rows = core.ledger.recent(limit=5)
    assert [r["surface"] for r in rows] == ["hermes_recall"]


def test_direct_recall_still_defaults_to_intentional(
    tmp_path: Path, vault_root: Path
) -> None:
    """Backward compatibility: a caller that names no surface gets today's."""
    core = _core(_settings(tmp_path, vault_root))

    core.recall("what is a daemon")
    core.recall_block("what is a daemon", budget_chars=2000)

    assert [r["surface"] for r in core.ledger.recent(limit=5)] == [
        "hermes_recall",
        "hermes_recall",
    ]


# ---------------------------------------------------------------------------
# exclusion from fingerprint recall + theme clustering
# ---------------------------------------------------------------------------


def _both_paths(tmp_path: Path, vault_root: Path) -> DaemonCore:
    provider, core = _provider(tmp_path, vault_root)
    provider.handle_tool_call("mydaemon_recall", {"query": "the deliberate question"})
    provider.prefetch("an ambient turn", session_id="s1")
    return core


def test_ambient_rows_are_excluded_from_clustering_input(
    tmp_path: Path, vault_root: Path
) -> None:
    from my_daemon.analysis.themes import cluster_fingerprints

    core = _both_paths(tmp_path, vault_root)

    # `cluster_fingerprints` reads exactly this, with exactly these surfaces.
    rows = core.ledger.query_fingerprints(surfaces=INTENTIONAL_SURFACES)
    assert [text for _id, text, _fp in rows] == ["the deliberate question"]

    assert cluster_fingerprints.__defaults__ is None
    assert cluster_fingerprints.__kwdefaults__["surfaces"] == INTENTIONAL_SURFACES

    # Unfiltered, both rows are there — the filter is doing the work.
    unfiltered = core.ledger.query_fingerprints()
    assert sorted(text for _id, text, _fp in unfiltered) == [
        "an ambient turn",
        "the deliberate question",
    ]


def test_ambient_rows_are_excluded_from_fingerprint_recall(
    tmp_path: Path, vault_root: Path
) -> None:
    core = _both_paths(tmp_path, vault_root)

    rows = core.ledger.query_fingerprints()
    by_text = {text: qid for qid, text, _fp in rows}
    probe = core.ledger.fingerprint(by_text["the deliberate question"])

    intentional_only = core.ledger.similar(
        probe,
        exclude_query_id=by_text["the deliberate question"],
        surfaces=INTENTIONAL_SURFACES,
    )
    assert [h.text for h in intentional_only] == []

    everything = core.ledger.similar(
        probe, exclude_query_id=by_text["the deliberate question"]
    )
    assert [h.text for h in everything] == ["an ambient turn"]


# ---------------------------------------------------------------------------
# prompt pollution: answer-less rows never reach the observer / reflect prompts
# ---------------------------------------------------------------------------


def _seed_feedback(db_path: Path) -> FeedbackStore:
    store = FeedbackStore(db_path=db_path)
    store.log(
        FeedbackEvent(
            timestamp=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
            query="what is a daemon",
            retrieval_summary={},
            answer="A companion.",
            latency_ms=10,
        )
    )
    # What an ambient prefetch leaves behind: a retrieval record with no answer.
    store.log(
        FeedbackEvent(
            timestamp=datetime(2026, 7, 20, 9, 1, tzinfo=UTC),
            query="an ambient turn",
            retrieval_summary={},
            answer="",
            latency_ms=4,
        )
    )
    return store


def test_observer_recent_feedback_skips_answerless_rows(tmp_path: Path) -> None:
    from my_daemon.pipeline.agent_observe import _recent_feedback

    store = _seed_feedback(tmp_path / "state.db")

    events = _recent_feedback(store, limit=20)

    assert [e.query for e in events] == ["what is a daemon"]


def test_reflect_recent_chats_skips_answerless_rows(tmp_path: Path) -> None:
    from my_daemon.pipeline.agent_reflect import _recent_chats

    store = _seed_feedback(tmp_path / "state.db")

    events = _recent_chats(store, limit=20)

    assert [e.query for e in events] == ["what is a daemon"]


def test_render_recent_chats_drops_answerless_rows() -> None:
    from my_daemon.llm.agents import _render_recent_chats

    answered = FeedbackEvent(
        timestamp=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        query="what is a daemon",
        retrieval_summary={},
        answer="A companion.",
        latency_ms=10,
    )
    ambient = FeedbackEvent(
        timestamp=datetime(2026, 7, 20, 9, 1, tzinfo=UTC),
        query="an ambient turn",
        retrieval_summary={},
        answer="",
        latency_ms=4,
    )

    assert _render_recent_chats([answered, ambient]) == (
        "- 2026-07-20  Q: what is a daemon\n    A: A companion."
    )
    assert _render_recent_chats([ambient]) == "(no recent chats)"


def test_observer_prompt_omits_answerless_rows(monkeypatch) -> None:
    """End of the line: the string the model actually reads."""
    from my_daemon.llm import agents
    from my_daemon.models import StructuralReport

    captured: dict = {}

    def _fake_call(client, *, system, user, model, max_tokens=1500):  # noqa: ANN001
        captured["user"] = user
        return "letter"

    monkeypatch.setattr(agents, "_call", _fake_call)

    agents.observer_letter(
        FakeLLM(),
        structural=StructuralReport(
            snapshot_id="snap",
            generated_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
            note_count=1,
            tag_count=0,
            edge_count=0,
            community_count=0,
        ),
        evolution=None,
        recent_feedback=[
            FeedbackEvent(
                timestamp=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
                query="what is a daemon",
                retrieval_summary={},
                answer="A companion.",
                latency_ms=10,
            ),
            FeedbackEvent(
                timestamp=datetime(2026, 7, 20, 9, 1, tzinfo=UTC),
                query="an ambient turn",
                retrieval_summary={},
                answer="",
                latency_ms=4,
            ),
        ],
        prior_letters=[],
        snapshot_id="snap",
    )

    assert "what is a daemon" in captured["user"]
    assert "an ambient turn" not in captured["user"]
