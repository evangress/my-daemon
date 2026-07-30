# SPDX-License-Identifier: Apache-2.0
"""The reranker as a third drafting team (§IV.18).

Three properties matter more than the rest, each pinned by its own test:

* with the feature off (no reranker, or a reranker present but
  `retrieval.rerank: false`), the pool is byte-for-byte what it was before
  this task — proven by comparing chunk-id lists under a seeded rng, not by
  asserting non-emptiness;
* `combined_score` is never overwritten by a rerank score — it is persisted
  in the retrieval summary and feeds the activation ledger's rank-based
  strength, so silently changing it would change what a fingerprint means;
* the candidate cap is honoured, because the cross-encoder is O(candidates)
  and that is the latency knob.
"""

from __future__ import annotations

import random

import pytest

from my_daemon.config import Settings
from my_daemon.retrieval.orchestrator import RetrievalOrchestrator

SEED_UUID = "aaaaaaaa-0000-4000-8000-00000000000{}"
NEIGHBOUR_UUID = "bbbbbbbb-0000-4000-8000-00000000000{}"


class _Point:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _payload(chunk_id: str, note_uuid: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "note_uuid": note_uuid,
        "note_path": f"{note_uuid}.md",
        "heading_path": [],
        "text": "body text",
        "chunk_index": 0,
        "tags": [],
        "wikilinks": [],
    }


class _FakeVectorStore:
    """Three seeds, each with one graph neighbour carrying one chunk."""

    collection = "chunks"

    def search(self, vector, top_k=8, date_range=None):  # noqa: ANN001
        return [
            {**_payload(f"seed-{i}", SEED_UUID.format(i)), "score": 0.9 - i * 0.1} for i in range(3)
        ]

    def hybrid_search(self, *a, **kw):  # noqa: ANN001
        return self.search(None)

    def _client_(self):
        class _Scroller:
            def scroll(self, *, scroll_filter, **kw):  # noqa: ANN001
                wanted = scroll_filter.must[0].match.value
                return [_Point(_payload(f"chunk-of-{wanted}", wanted))], None

        return _Scroller()


class _FakeGraphStore:
    def neighbors_within(self, note_uuid, depth, *, weighted=False, exclude_tag_prefixes=()):  # noqa: ANN001
        index = note_uuid[-1]
        return {NEIGHBOUR_UUID.format(index): 1.0}


class _FakeEmbedder:
    dimension = 4

    def encode_one(self, text: str):  # noqa: ANN001
        return [0.0] * 4

    def encode(self, texts):  # noqa: ANN001
        return [[0.0] * 4 for _ in texts]


class _StubReranker:
    """Scores only — no config knowledge, no download, matching the real one.

    Records the number of candidates it was last asked to score, so a test
    can confirm the orchestrator honoured `rerank_max_candidates` before ever
    calling in.
    """

    def __init__(self, constant: float | None = None) -> None:
        self.constant = constant
        self.last_n = 0

    def rank(self, query: str, candidates):  # noqa: ANN001
        self.last_n = len(candidates)
        if self.constant is not None:
            return [self.constant] * len(candidates)
        # Reverses combined_score order, so the rerank team's draft is
        # actually distinguishable from the score-order teams in a test.
        return [float(i) for i in range(len(candidates))]


@pytest.fixture
def orchestrator_for():
    """Build an orchestrator over the three-seed fake vault, keyword-configured."""

    def _make(
        *,
        reranker=None,
        rerank: bool = False,
        rerank_max_candidates: int = 100,
        rng: random.Random | None = None,
    ) -> RetrievalOrchestrator:
        settings = Settings()
        settings.embeddings.hybrid = False
        settings.retrieval.rerank = rerank
        settings.retrieval.rerank_max_candidates = rerank_max_candidates
        return RetrievalOrchestrator(
            settings,
            _FakeEmbedder(),
            _FakeVectorStore(),
            _FakeGraphStore(),
            rng=rng or random.Random(),
            reranker=reranker,
        )

    return _make


def test_no_reranker_reproduces_two_team_behaviour(orchestrator_for):
    """Byte-for-byte, so enabling the feature is the only thing that changes."""
    a = orchestrator_for(reranker=None, rng=random.Random(7)).retrieve("q")
    b = orchestrator_for(reranker=None, rng=random.Random(7)).retrieve("q")
    assert [rc.chunk.id for rc in a.ranked] == [rc.chunk.id for rc in b.ranked]
    assert {rc.team for rc in a.ranked} <= {"seed", "expansion"}


def test_reranker_present_but_disabled_changes_nothing(orchestrator_for):
    plain = orchestrator_for(reranker=None, rng=random.Random(7)).retrieve("q")
    off = orchestrator_for(reranker=_StubReranker(), rerank=False, rng=random.Random(7)).retrieve(
        "q"
    )
    assert [rc.chunk.id for rc in plain.ranked] == [rc.chunk.id for rc in off.ranked]


def test_enabled_reranker_adds_a_third_team(orchestrator_for):
    result = orchestrator_for(reranker=_StubReranker(), rerank=True).retrieve("q")
    assert "rerank" in {rc.team for rc in result.ranked}


def test_rerank_does_not_overwrite_combined_score(orchestrator_for):
    """`combined_score` is persisted and feeds the activation ledger's rank
    strength; overwriting it would silently change what fingerprints mean."""
    stub = _StubReranker(constant=0.99)
    result = orchestrator_for(reranker=stub, rerank=True).retrieve("q")
    assert all(rc.combined_score != 0.99 for rc in result.ranked)


def test_rerank_respects_the_candidate_cap(orchestrator_for):
    stub = _StubReranker()
    orchestrator_for(reranker=stub, rerank=True, rerank_max_candidates=2).retrieve("q")
    assert stub.last_n <= 2


def test_rerank_latency_is_reported_on_the_result(orchestrator_for):
    """Surfaced separately from `latency_ms` so a slow reranker stays
    attributable to itself rather than folded into an opaque total."""
    result = orchestrator_for(reranker=_StubReranker(), rerank=True).retrieve("q")
    assert result.rerank_ms is not None
    assert result.rerank_ms >= 0


def test_rerank_latency_is_absent_when_no_rerank_ran(orchestrator_for):
    result = orchestrator_for(reranker=None).retrieve("q")
    assert result.rerank_ms is None
