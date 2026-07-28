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
"""

from __future__ import annotations

import random

from my_daemon.models import RetrievedChunk

#: The two competing rankings. Stored on ``RetrievedChunk.team`` and persisted
#: in the retrieval summary, so a much later ``daemon select`` can still tell
#: which policy earned the pick.
SEED_TEAM = "seed"
EXPANSION_TEAM = "expansion"


def team_draft(
    seeds: list[RetrievedChunk],
    expanded: list[RetrievedChunk],
    *,
    rng: random.Random,
) -> list[RetrievedChunk]:
    """Interleave two rankings by team draft, tagging each pick with its team.

    Both inputs are consumed in the order given — each team always drafts its
    own best remaining candidate — so the caller is responsible for having
    sorted them. Whichever team has drafted fewer candidates picks next; ties
    are broken by a coin toss, which is what keeps the teams' position
    distributions symmetric.

    A chunk id already drafted is skipped rather than drafted twice. Expansion
    currently cannot surface a seed's own note, but the draft should not
    silently depend on that staying true.
    """

    queues = {SEED_TEAM: list(seeds), EXPANSION_TEAM: list(expanded)}
    cursors = {SEED_TEAM: 0, EXPANSION_TEAM: 0}
    counts = {SEED_TEAM: 0, EXPANSION_TEAM: 0}
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
            # A copy, so the caller's seed/expanded lists keep their own
            # identity — `RetrievalResult` holds all three and the team tag
            # belongs to the presented pool, not to the source ranking.
            drafted.append(candidate.model_copy(update={"team": team}))
            counts[team] += 1
            return True
        return moved

    while not (_exhausted(SEED_TEAM) and _exhausted(EXPANSION_TEAM)):
        # Exhaustion is checked before fairness, and in that order, because
        # `_draft` on an exhausted team is a no-op — dispatching to one would
        # leave every counter unchanged and spin this loop forever.
        if _exhausted(SEED_TEAM):
            progressed = _draft(EXPANSION_TEAM)
        elif _exhausted(EXPANSION_TEAM) or counts[SEED_TEAM] < counts[EXPANSION_TEAM]:
            progressed = _draft(SEED_TEAM)
        elif counts[EXPANSION_TEAM] < counts[SEED_TEAM]:
            progressed = _draft(EXPANSION_TEAM)
        else:
            # Level pegging: the coin decides, which is the whole mechanism.
            # A deterministic tie-break here would give one policy the odd
            # position every time and reintroduce the bias.
            progressed = _draft(SEED_TEAM if rng.random() < 0.5 else EXPANSION_TEAM)

        if not progressed:  # pragma: no cover — unreachable, and cheap insurance
            break

    return drafted
