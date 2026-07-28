# SPDX-License-Identifier: Apache-2.0
"""The retrieval-policy ledger — which ranking earns the user's picks.

This is the half of the interleaving work that closes the loop. Team-draft
gives the two rankings symmetric exposure; this records who won, so a seed pick
is evidence about a *policy* even on the very common query where seed and
selection are the same note and there is no graph path to reinforce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.retrieval.interleave import EXPANSION_TEAM, SEED_TEAM
from my_daemon.stores.policy import RetrievalPolicyStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def policy(tmp_path: Path) -> RetrievalPolicyStore:
    return RetrievalPolicyStore(db_path=tmp_path / "state.db")


def test_an_unseen_policy_reports_zeroes(policy: RetrievalPolicyStore):
    stat = policy.get(SEED_TEAM)

    assert (stat.impressions, stat.wins) == (0, 0)


def test_a_win_is_recorded_against_its_own_policy(policy: RetrievalPolicyStore):
    policy.record_win(EXPANSION_TEAM, now=NOW)

    assert policy.get(EXPANSION_TEAM).wins == 1
    assert policy.get(SEED_TEAM).wins == 0


def test_impressions_accumulate_across_retrievals(policy: RetrievalPolicyStore):
    policy.record_impressions({SEED_TEAM: 3, EXPANSION_TEAM: 2})
    policy.record_impressions({SEED_TEAM: 1, EXPANSION_TEAM: 4})

    assert policy.get(SEED_TEAM).impressions == 4
    assert policy.get(EXPANSION_TEAM).impressions == 6


def test_win_rate_is_wins_over_impressions(policy: RetrievalPolicyStore):
    policy.record_impressions({SEED_TEAM: 4})
    policy.record_win(SEED_TEAM, now=NOW)

    assert policy.get(SEED_TEAM).win_rate == pytest.approx(0.25)


def test_win_rate_of_a_policy_never_shown_is_zero_not_a_crash(policy: RetrievalPolicyStore):
    """Division by zero here would take down `daemon policy` on a fresh vault."""

    assert policy.get(SEED_TEAM).win_rate == 0.0


def test_the_last_win_is_timestamped(policy: RetrievalPolicyStore):
    policy.record_win(SEED_TEAM, now=NOW)

    assert policy.get(SEED_TEAM).last_win_at == NOW


def test_stats_survive_a_reopen(tmp_path: Path):
    db = tmp_path / "state.db"
    RetrievalPolicyStore(db_path=db).record_win(SEED_TEAM, now=NOW)

    assert RetrievalPolicyStore(db_path=db).get(SEED_TEAM).wins == 1


def test_stats_lists_every_policy_seen(policy: RetrievalPolicyStore):
    policy.record_impressions({SEED_TEAM: 1, EXPANSION_TEAM: 1})

    assert {s.policy for s in policy.stats()} == {SEED_TEAM, EXPANSION_TEAM}


def test_a_zero_count_impression_is_not_recorded(policy: RetrievalPolicyStore):
    """A retrieval that surfaced no expansion did not *show* the expansion
    policy, and must not dilute its win rate."""

    policy.record_impressions({SEED_TEAM: 3, EXPANSION_TEAM: 0})

    assert {s.policy for s in policy.stats()} == {SEED_TEAM}
