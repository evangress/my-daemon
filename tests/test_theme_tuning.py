# SPDX-License-Identifier: Apache-2.0
"""Sweeping the theme match threshold against real ledger history.

`DEFAULT_MATCH_THRESHOLD = 0.60` is the highest-leverage untuned constant in
the system: it decides whether tonight's cluster inherits an existing theme's
id and label or mints a new one, so it directly controls how much the themes
the user sees churn from night to night. It was a judgement call. This module
turns it into a measurement, by replaying the ledger in sequential windows —
the same way consolidate would have seen it — at each candidate threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_daemon.analysis.theme_tuning import ThresholdReport, sweep_match_threshold
from my_daemon.stores.activations import Activation, ActivationLedger

START = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


@pytest.fixture
def ledger(tmp_path: Path) -> ActivationLedger:
    return ActivationLedger(db_path=tmp_path / "state.db")


def _seed_two_stable_concerns(ledger: ActivationLedger, *, days: int = 12) -> None:
    """Two note-groups, queried in alternation for `days` days.

    A well-chosen threshold should recover two persistent themes and almost no
    churn; a badly-chosen one should either fragment them or merge them.
    """

    groups = [[_uuid(1), _uuid(2), _uuid(3)], [_uuid(10), _uuid(11), _uuid(12)]]
    for day in range(days):
        for index, notes in enumerate(groups):
            for repeat in range(3):
                ledger.record(
                    query_uid=f"q-{day}-{index}-{repeat}",
                    text=f"question about group {index} on day {day}",
                    surface="cli",
                    ts=START + timedelta(days=day, minutes=repeat),
                    activations=[
                        Activation(note_uuid=u, source="vector_seed", rank=rank + 1)
                        for rank, u in enumerate(notes)
                    ],
                )


def test_a_sweep_reports_one_row_per_threshold(ledger: ActivationLedger):
    _seed_two_stable_concerns(ledger)

    reports = sweep_match_threshold(ledger, thresholds=(0.4, 0.6, 0.8), windows=3)

    assert [r.threshold for r in reports] == [0.4, 0.6, 0.8]
    assert all(isinstance(r, ThresholdReport) for r in reports)


def test_an_empty_ledger_sweeps_to_nothing(tmp_path: Path):
    """A fresh vault must get an honest "no data" rather than a crash or a
    confidently-wrong recommendation."""

    empty = ActivationLedger(db_path=tmp_path / "empty.db")

    reports = sweep_match_threshold(empty, thresholds=(0.6,), windows=3)

    assert all(r.themes_final == 0 for r in reports)
    assert all(r.windows_evaluated == 0 for r in reports)


def test_a_threshold_of_one_never_matches_so_every_window_churns(ledger: ActivationLedger):
    """The degenerate high end: nothing is ever similar enough to inherit a
    label, so each window mints new themes and abandons the old."""

    _seed_two_stable_concerns(ledger)

    (report,) = sweep_match_threshold(ledger, thresholds=(1.01,), windows=3)

    assert report.mean_churn == pytest.approx(1.0)
    assert report.themes_matched == 0


def test_a_threshold_of_zero_matches_everything(ledger: ActivationLedger):
    """The degenerate low end: every cluster inherits some existing theme, so
    nothing is ever created after the first window."""

    _seed_two_stable_concerns(ledger)

    (report,) = sweep_match_threshold(ledger, thresholds=(0.0,), windows=3)

    assert report.themes_matched > 0


def test_the_sweep_leaves_the_live_theme_store_untouched(ledger: ActivationLedger, tmp_path: Path):
    """This runs against the user's real ledger. It must not mint themes."""

    from my_daemon.stores.themes import ThemeStore

    _seed_two_stable_concerns(ledger)
    live = ThemeStore(db_path=ledger.db_path)

    sweep_match_threshold(ledger, thresholds=(0.5, 0.6), windows=3)

    assert live.all() == []


def test_each_threshold_is_evaluated_independently(ledger: ActivationLedger):
    """A shared store between thresholds would let the first one's themes seed
    the second, and the sweep would measure order rather than threshold."""

    _seed_two_stable_concerns(ledger)

    forward = sweep_match_threshold(ledger, thresholds=(0.4, 0.9), windows=3)
    backward = sweep_match_threshold(ledger, thresholds=(0.9, 0.4), windows=3)

    by_threshold = {r.threshold: r.mean_churn for r in forward}
    reversed_by_threshold = {r.threshold: r.mean_churn for r in backward}

    assert by_threshold == pytest.approx(reversed_by_threshold)


def test_the_recommendation_prefers_the_least_churn_that_still_finds_themes(
    ledger: ActivationLedger,
):
    from my_daemon.analysis.theme_tuning import recommend

    _seed_two_stable_concerns(ledger)
    reports = sweep_match_threshold(ledger, thresholds=(0.3, 0.6, 1.01), windows=3)

    best = recommend(reports)

    assert best is not None
    assert best.themes_final > 0
    assert best.threshold != 1.01


def test_a_sweep_that_found_no_themes_anywhere_recommends_nothing():
    """Better to say "not enough history" than to name a threshold picked from
    a table of zeroes."""

    from my_daemon.analysis.theme_tuning import recommend

    barren = [
        ThresholdReport(
            threshold=t,
            windows_evaluated=0,
            themes_final=0,
            themes_created=0,
            themes_matched=0,
            themes_dormant=0,
            mean_churn=1.0,
            mean_cluster_size=0.0,
        )
        for t in (0.4, 0.6)
    ]

    assert recommend(barren) is None


def test_the_first_window_is_not_counted_as_stable(ledger: ActivationLedger):
    """The opening reconciliation has no previous partition to differ from.

    Counting its churn of 0.0 as stability would make the same ledger score
    better at `--windows 2` than at `--windows 8`, which would make the whole
    sweep a measurement of the window count.
    """

    _seed_two_stable_concerns(ledger)

    (few,) = sweep_match_threshold(ledger, thresholds=(1.01,), windows=2)
    (many,) = sweep_match_threshold(ledger, thresholds=(1.01,), windows=4)

    assert few.mean_churn == pytest.approx(many.mean_churn)
