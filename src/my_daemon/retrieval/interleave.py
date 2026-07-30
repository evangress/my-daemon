# SPDX-License-Identifier: Apache-2.0
"""Team-draft interleaving of the seed and expansion rankings.

The problem this solves is the one the maturity evaluation left open: seeds
dominate the candidate pool, and a seed pick reinforces nothing because
``apply_selection`` no-ops when seed == selected. Tuning the reinforcement
constants cannot fix that, because the signal itself is confounded — a click
observes *examination* and *relevance* jointly, and rank-1 is examined far more
often than rank-8 whatever its quality. Learn from the raw signal and the
system converges on reproducing its own presentation order (Joachims,
Swaminathan & Schnabel, *Unbiased Learning-to-Rank with Biased Feedback*,
WSDM 2017).

There are two published ways out. Inverse-propensity weighting needs an
estimate of the examination probability at each rank, which needs either
result randomization or a lot of data; at one user we have neither, and
Radlinski, Kurup & Joachims (*How does clickthrough data reflect retrieval
quality?*, CIKM 2008) showed that absolute click metrics do not reliably track
quality at realistic sample sizes anyway. Their alternative — team-draft
interleaving — is what this module implements: two rankings alternate picks,
a coin decides who goes first, and each surfaced candidate remembers which
ranking drafted it. Because both teams contribute equally often and their
positions are symmetric in expectation, a pick becomes attributable to a
*policy* rather than to a position, with no propensity model at all.

The payoff for this codebase specifically: a seed pick stops being a no-op. It
may still have no graph path to reinforce, but it is evidence that the seed
policy beat the expansion policy on that query — which is recorded by
:mod:`my_daemon.stores.policy` and is the thing the loop was missing.

Extended 2026-07-30 to n rankings — team-draft *multileaving* (Schuth, Sietsma,
Whiteson, Lefortier & de Rijke, *Multileaved Comparisons for Fast Online
Evaluation*, CIKM 2014), which generalises the two-team draft while preserving
the per-team position symmetry that makes a pick attributable without a
propensity model. The third team is a cross-encoder's ordering of the whole
pool (§IV.18), so teams now *overlap* rather than partition the pool; the
``seen`` set already handled that.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from my_daemon.models import RetrievedChunk

#: The competing rankings. Stored on ``RetrievedChunk.team`` and persisted in
#: the retrieval summary, so a much later ``daemon select`` can still tell
#: which policy earned the pick.
SEED_TEAM = "seed"
EXPANSION_TEAM = "expansion"
RERANK_TEAM = "rerank"


def multileave(
    rankings: Mapping[str, Sequence[RetrievedChunk]],
    *,
    rng: random.Random,
) -> list[RetrievedChunk]:
    """Interleave n rankings by team draft, tagging each pick with its team.

    Every ranking is consumed in the order given — each team always drafts its
    own best remaining candidate — so the caller is responsible for having
    sorted them. Whichever team(s) have drafted the fewest candidates so far
    pick next; a tie among exactly two teams is broken the way the original
    two-team draft always was, by a coin (``rng.random() < 0.5``), so that a
    seeded ``rng`` reproduces the old ``team_draft`` output exactly. A tie
    among three or more teams is broken by ``rng.choice`` over *all* of them —
    not the first two, and not a deterministic pick — because "first tied team
    wins" would systematically hand one policy the earlier position and
    reintroduce exactly the position bias the draft exists to cancel.

    A chunk id already drafted is skipped rather than drafted twice. This is
    what makes overlapping rankings safe: a reranker's ordering of the whole
    pool will contain every id the other teams already hold, not a disjoint
    slice, and the draft must not double-surface a candidate just because two
    teams both wanted it.
    """

    queues = {team: list(items) for team, items in rankings.items()}
    cursors = dict.fromkeys(queues, 0)
    counts = dict.fromkeys(queues, 0)
    drafted: list[RetrievedChunk] = []
    seen: set[str] = set()

    def _exhausted(team: str) -> bool:
        return cursors[team] >= len(queues[team])

    def _draft(team: str) -> bool:
        """Take ``team``'s best undrafted candidate. False if it had none left.

        The return value is what makes termination structural rather than
        argued: this runs on every query, and a dispatch to an already-exhausted
        team would otherwise spin forever and freeze the daemon.
        """

        moved = False
        while not _exhausted(team):
            candidate = queues[team][cursors[team]]
            cursors[team] += 1
            moved = True
            if candidate.chunk.id in seen:
                continue
            seen.add(candidate.chunk.id)
            # A copy, so the caller's source lists keep their own identity —
            # `RetrievalResult` holds seed/expanded separately from the
            # presented pool, and the team tag belongs to the latter.
            drafted.append(candidate.model_copy(update={"team": team}))
            counts[team] += 1
            return True
        return moved

    while True:
        live = [t for t in queues if not _exhausted(t)]
        if not live:
            return drafted
        fewest = min(counts[t] for t in live)
        tied = [t for t in live if counts[t] == fewest]

        if len(tied) == 1:
            choice = tied[0]
        elif len(tied) == 2:
            # Bit-for-bit the original two-team coin: same rng method, same
            # call site, so `multileave` with two teams reproduces the old
            # `team_draft` output exactly for a seeded `rng`.
            choice = tied[0] if rng.random() < 0.5 else tied[1]
        else:
            # Uniform over ALL tied teams, not the first of them — see the
            # docstring. This is the case the n-team generalisation actually
            # adds; two teams never reach this branch.
            choice = rng.choice(tied)

        if not _draft(choice):
            return drafted  # pragma: no cover — structural insurance
