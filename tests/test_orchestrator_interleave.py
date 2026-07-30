# SPDX-License-Identifier: Apache-2.0
"""The orchestrator's candidate pool, with and without team-draft interleaving.

The behaviour under test is the one the maturity evaluation flagged: an
expanded chunk scores ``seed_score * decay**distance`` and therefore can never
outrank the seed that produced it, so a pure score sort hands the whole
candidate pool to seeds and the expansion policy never gets examined.
"""

from __future__ import annotations

import random

from my_daemon.config import Settings
from my_daemon.retrieval.interleave import EXPANSION_TEAM, SEED_TEAM
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


def _settings(*, interleave: bool, budget: int) -> Settings:
    settings = Settings()
    settings.embeddings.hybrid = False
    settings.retrieval.interleave = interleave
    settings.retrieval.context_token_budget = budget
    return settings


def _orchestrator(*, interleave: bool, budget: int = 10) -> RetrievalOrchestrator:
    return RetrievalOrchestrator(
        _settings(interleave=interleave, budget=budget),
        _FakeEmbedder(),
        _FakeVectorStore(),
        _FakeGraphStore(),
        rng=random.Random(0),
    )


def test_interleaving_is_enabled_by_default():
    """The learning loop is dead without it, so it is not opt-in."""

    assert Settings().retrieval.interleave is True


def test_score_order_starves_expansion_of_every_pool_slot():
    """Pins the behaviour interleaving exists to fix.

    Every expanded chunk is a decayed copy of its seed's score, so a score sort
    puts all three seeds ahead of every expansion. The candidate floor is three,
    so the pool the user is asked to pick from is seeds and nothing else.
    """

    result = _orchestrator(interleave=False).retrieve("q")
    floor = Settings().retrieval.candidate_pool

    assert [rc.chunk.id for rc in result.ranked[:floor]] == ["seed-0", "seed-1", "seed-2"]


def test_interleaving_gives_expansion_half_the_pool():
    result = _orchestrator(interleave=True).retrieve("q")

    teams = [rc.team for rc in result.ranked]

    assert EXPANSION_TEAM in teams
    assert abs(teams.count(SEED_TEAM) - teams.count(EXPANSION_TEAM)) <= 1


def test_every_pooled_candidate_carries_its_team():
    result = _orchestrator(interleave=True).retrieve("q")

    assert all(rc.team in (SEED_TEAM, EXPANSION_TEAM) for rc in result.ranked)


def test_score_order_leaves_the_team_unset():
    """No interleaving means no attribution — and a missing team must stay
    missing rather than defaulting to one of the policies."""

    result = _orchestrator(interleave=False).retrieve("q")

    assert all(rc.team is None for rc in result.ranked)


def test_the_candidate_floor_still_holds_under_interleaving():
    result = _orchestrator(interleave=True, budget=1).retrieve("q")

    assert len(result.ranked) >= Settings().retrieval.candidate_pool


def test_seeds_and_expanded_keep_their_own_untagged_identity():
    """`RetrievalResult.seeds` is the seed *ranking*, not the presented pool.
    Tagging it would make the two indistinguishable downstream."""

    result = _orchestrator(interleave=True).retrieve("q")

    assert all(rc.team is None for rc in result.seeds)
    assert all(rc.team is None for rc in result.expanded)
