# SPDX-License-Identifier: Apache-2.0
"""Team-draft interleaving of the seed and expansion rankings.

Why this exists: a click is a joint observation of *examination* and
*relevance*, and rank-1 gets examined far more than rank-8 regardless of
quality. Training on raw picks therefore converges on the presentation policy
rather than on relevance (Joachims et al., WSDM 2017). Team-draft interleaving
(Radlinski, Kurup & Joachims, CIKM 2008) sidesteps the propensity estimate
entirely: because each team contributes equally often and the order is decided
by a coin, a pick is attributable to a *policy* rather than to a position.
"""

from __future__ import annotations

from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.retrieval.interleave import EXPANSION_TEAM, SEED_TEAM, multileave


def _team_draft(seeds, expanded, *, rng):
    """The old two-team entry point, now a thin test helper over ``multileave``.

    ``team_draft`` itself was removed (IV.18) — the n-team generaliser is the
    only implementation now. With exactly two teams, ``multileave`` breaks
    ties with the same ``rng.random() < 0.5`` coin the original used, in the
    same call position, so this reproduces the old output exactly and these
    tests are unchanged in substance: only the import and this call wrapper
    are new.
    """

    return multileave({SEED_TEAM: seeds, EXPANSION_TEAM: expanded}, rng=rng)


class _ScriptedCoin:
    """A stand-in for ``random.Random`` whose tosses are known.

    Only ``random()`` is used for a two-team tie-break; scripting it is the
    only way to assert on draft *order* rather than on a distribution.
    """

    def __init__(self, *values: float) -> None:
        self._values = list(values)
        self._index = 0

    def random(self) -> float:
        value = self._values[self._index % len(self._values)]
        self._index += 1
        return value


def _chunk(chunk_id: str, score: float, *, distance: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            id=chunk_id,
            note_uuid=f"uuid-{chunk_id}",
            note_path=f"{chunk_id}.md",
            heading_path=[],
            text=chunk_id,
            chunk_index=0,
        ),
        graph_distance=distance,
        combined_score=score,
    )


def _seeds(n: int) -> list[RetrievedChunk]:
    return [_chunk(f"s{i}", 1.0 - i * 0.01, distance=0) for i in range(n)]


def _expanded(n: int) -> list[RetrievedChunk]:
    return [_chunk(f"e{i}", 0.5 - i * 0.01, distance=1.0) for i in range(n)]


ALWAYS_SEED_FIRST = _ScriptedCoin(0.0)
ALWAYS_EXPANSION_FIRST = _ScriptedCoin(0.99)


def test_seed_ranking_leads_when_it_wins_the_toss():
    drafted = _team_draft(_seeds(3), _expanded(3), rng=_ScriptedCoin(0.0))

    assert [rc.chunk.id for rc in drafted] == ["s0", "e0", "s1", "e1", "s2", "e2"]


def test_expansion_ranking_leads_when_it_wins_the_toss():
    drafted = _team_draft(_seeds(3), _expanded(3), rng=_ScriptedCoin(0.99))

    assert [rc.chunk.id for rc in drafted] == ["e0", "s0", "e1", "s1", "e2", "s2"]


def test_each_candidate_is_attributed_to_the_ranking_it_came_from():
    drafted = _team_draft(_seeds(2), _expanded(2), rng=_ScriptedCoin(0.0))

    attribution = {rc.chunk.id: rc.team for rc in drafted}

    assert attribution == {
        "s0": SEED_TEAM,
        "s1": SEED_TEAM,
        "e0": EXPANSION_TEAM,
        "e1": EXPANSION_TEAM,
    }


def test_team_sizes_never_differ_by_more_than_one():
    """The fairness invariant that makes position bias cancel.

    If one team could get two picks in a row while the other waited, the teams
    would occupy systematically different positions and the whole point of the
    design would be lost.
    """

    drafted = _team_draft(_seeds(5), _expanded(5), rng=_ScriptedCoin(0.0, 0.99, 0.0, 0.99))

    for cut in range(1, len(drafted) + 1):
        prefix = drafted[:cut]
        seeds = sum(1 for rc in prefix if rc.team == SEED_TEAM)
        expansions = sum(1 for rc in prefix if rc.team == EXPANSION_TEAM)
        assert abs(seeds - expansions) <= 1, f"unfair after {cut} picks"


def test_every_candidate_is_drafted_exactly_once():
    drafted = _team_draft(_seeds(4), _expanded(7), rng=_ScriptedCoin(0.0, 0.99))

    ids = [rc.chunk.id for rc in drafted]

    assert sorted(ids) == sorted([f"s{i}" for i in range(4)] + [f"e{i}" for i in range(7)])
    assert len(ids) == len(set(ids))


def test_an_exhausted_ranking_lets_the_other_finish():
    """Four seeds and one expansion must still surface all four seeds."""

    drafted = _team_draft(_seeds(4), _expanded(1), rng=_ScriptedCoin(0.0))

    assert [rc.chunk.id for rc in drafted] == ["s0", "e0", "s1", "s2", "s3"]


def test_an_empty_expansion_yields_the_seed_ranking_unchanged():
    drafted = _team_draft(_seeds(3), [], rng=_ScriptedCoin(0.0))

    assert [rc.chunk.id for rc in drafted] == ["s0", "s1", "s2"]
    assert all(rc.team == SEED_TEAM for rc in drafted)


def test_a_chunk_present_in_both_rankings_is_drafted_once():
    """Defensive: expansion skips seed notes today, but the draft must not
    depend on that invariant holding forever."""

    shared = _chunk("shared", 0.9, distance=0)
    drafted = _team_draft([shared], [shared], rng=_ScriptedCoin(0.0))

    assert [rc.chunk.id for rc in drafted] == ["shared"]


def test_ranking_order_within_a_team_is_preserved():
    """A team always drafts its own best remaining candidate."""

    drafted = _team_draft(_seeds(3), _expanded(3), rng=_ScriptedCoin(0.0, 0.99, 0.0))

    seed_order = [rc.chunk.id for rc in drafted if rc.team == SEED_TEAM]
    expansion_order = [rc.chunk.id for rc in drafted if rc.team == EXPANSION_TEAM]

    assert seed_order == ["s0", "s1", "s2"]
    assert expansion_order == ["e0", "e1", "e2"]
