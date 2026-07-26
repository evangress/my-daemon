# SPDX-License-Identifier: Apache-2.0
"""Emergent themes — clusters of query fingerprints, named during the dream phase.

The hard part is not clustering. It is *stability*: a theme whose label changes
every night is worse than no theme at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.analysis.themes import cluster_fingerprints, reconcile_themes
from my_daemon.stores.activations import Activation, ActivationLedger
from my_daemon.stores.themes import ThemeStore

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
D = "dddddddd-0000-4000-8000-000000000004"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def ledger(db: Path) -> ActivationLedger:
    return ActivationLedger(db_path=db)


@pytest.fixture
def themes(db: Path) -> ThemeStore:
    return ThemeStore(db_path=db)


def _ask(ledger: ActivationLedger, text: str, notes, *, surface="cli") -> int:
    return ledger.record(
        text=text,
        surface=surface,
        ts=datetime.now(UTC),
        activations=[
            Activation(note_uuid=u, source="vector_seed", rank=i)
            for i, u in enumerate(notes, start=1)
        ],
    )


def _two_populations(ledger: ActivationLedger) -> None:
    """Two clearly separate concerns, four queries each."""
    for i in range(4):
        _ask(ledger, f"memory question {i}", [A, B])
    for i in range(4):
        _ask(ledger, f"cooking question {i}", [C, D])


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_separate_concerns_form_separate_clusters(ledger: ActivationLedger):
    _two_populations(ledger)

    clusters = cluster_fingerprints(ledger, min_cluster_size=3)

    assert len(clusters) == 2


def test_a_cluster_carries_the_notes_that_define_it(ledger: ActivationLedger):
    _two_populations(ledger)

    clusters = cluster_fingerprints(ledger, min_cluster_size=3)

    note_sets = [set(c.centroid) for c in clusters]
    assert {A, B} in note_sets


def test_a_cluster_carries_representative_queries_for_naming(ledger: ActivationLedger):
    _two_populations(ledger)

    clusters = cluster_fingerprints(ledger, min_cluster_size=3)

    assert all(c.representative_queries for c in clusters)


def test_most_queries_should_not_get_a_theme(ledger: ActivationLedger):
    """Forcing every query into a cluster is how you get meaningless themes."""
    _two_populations(ledger)
    _ask(ledger, "a one-off", ["ffffffff-0000-4000-8000-000000000009"])

    clusters = cluster_fingerprints(ledger, min_cluster_size=3)

    clustered = {q for c in clusters for q in c.query_ids}
    assert len(clustered) == 8


def test_a_burst_of_two_related_queries_does_not_mint_a_theme(
    ledger: ActivationLedger,
):
    _ask(ledger, "q1", [A, B])
    _ask(ledger, "q2", [A, B])

    assert cluster_fingerprints(ledger, min_cluster_size=3) == []


def test_an_empty_ledger_clusters_to_nothing(ledger: ActivationLedger):
    assert cluster_fingerprints(ledger, min_cluster_size=3) == []


def test_ambient_prefetches_are_excluded_by_default(ledger: ActivationLedger):
    for i in range(6):
        _ask(ledger, f"ambient {i}", [A, B], surface="hermes_prefetch")

    assert cluster_fingerprints(ledger, min_cluster_size=3) == []


# ---------------------------------------------------------------------------
# Stability — the thing that makes or breaks this
# ---------------------------------------------------------------------------


def test_a_returning_cluster_reuses_its_theme_id_and_label(
    ledger: ActivationLedger, themes: ThemeStore
):
    _two_populations(ledger)
    clusters = cluster_fingerprints(ledger, min_cluster_size=3)
    first = reconcile_themes(themes, clusters, snapshot_id="s1")
    for theme_id, _cluster in first.matched + first.created:
        themes.set_label(theme_id, label="Named By Human", summary="", locked=True)

    second = reconcile_themes(themes, clusters, snapshot_id="s2")

    assert second.created == []
    assert {t for t, _ in second.matched} == {t for t, _ in first.created}
    assert all(themes.get(t).label == "Named By Human" for t, _ in second.matched)


def test_a_returning_cluster_needs_no_llm_call(
    ledger: ActivationLedger, themes: ThemeStore
):
    """Only unmatched clusters get named — steady state is zero calls a night."""
    _two_populations(ledger)
    clusters = cluster_fingerprints(ledger, min_cluster_size=3)
    reconcile_themes(themes, clusters, snapshot_id="s1")

    assert reconcile_themes(themes, clusters, snapshot_id="s2").needs_naming == []


def test_an_accepted_label_is_never_overwritten(themes: ThemeStore):
    theme_id = themes.create(slug="t", label="auto", centroid={A: 1.0}, snapshot_id="s1")
    themes.set_label(theme_id, label="Mine", summary="", locked=True)

    themes.set_label(theme_id, label="the LLM's new idea", summary="")

    assert themes.get(theme_id).label == "Mine"


def test_a_theme_that_stops_appearing_goes_dormant_not_deleted(
    ledger: ActivationLedger, themes: ThemeStore
):
    _two_populations(ledger)
    clusters = cluster_fingerprints(ledger, min_cluster_size=3)
    reconcile_themes(themes, clusters, snapshot_id="s1")

    for run in range(3):
        reconcile_themes(themes, [], snapshot_id=f"s{run + 2}")

    all_themes = themes.all()
    assert all_themes and all(t.status == "dormant" for t in all_themes)


def test_a_dormant_theme_revives_if_it_comes_back(
    ledger: ActivationLedger, themes: ThemeStore
):
    _two_populations(ledger)
    clusters = cluster_fingerprints(ledger, min_cluster_size=3)
    reconcile_themes(themes, clusters, snapshot_id="s1")
    for run in range(3):
        reconcile_themes(themes, [], snapshot_id=f"s{run + 2}")

    reconcile_themes(themes, clusters, snapshot_id="s9")

    assert all(t.status != "dormant" for t in themes.all())


def test_churn_is_reported_so_unsettled_themes_can_say_so(
    ledger: ActivationLedger, themes: ThemeStore
):
    _two_populations(ledger)
    clusters = cluster_fingerprints(ledger, min_cluster_size=3)
    reconcile_themes(themes, clusters, snapshot_id="s1")

    result = reconcile_themes(themes, clusters, snapshot_id="s2")

    assert result.churn == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Inside the dream phase
# ---------------------------------------------------------------------------


def test_consolidate_clusters_themes_without_the_letter_depending_on_it(
    tmp_path: Path, monkeypatch
):
    """A clustering failure must not cost the user their observer letter."""
    from my_daemon.config import Settings
    from my_daemon.pipeline import agent_observe

    stats = agent_observe.ObserveStats(snapshot_id="s1")
    settings = Settings()
    settings.feedback.db_path = tmp_path / "state.db"

    def _boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("sklearn exploded")

    monkeypatch.setattr("my_daemon.analysis.themes.cluster_fingerprints", _boom)

    result, letter_themes = agent_observe._cluster_themes(
        settings, object(), "s1", dry_run=False, progress=None, stats=stats
    )

    assert result is None
    # The letter still gets written — with no themes in it, not with none at all.
    assert letter_themes == []
    assert any("theme clustering failed" in e for e in stats.errors)


def test_a_dry_run_names_nothing_and_writes_nothing(tmp_path: Path):
    from my_daemon.config import Settings
    from my_daemon.pipeline import agent_observe

    settings = Settings()
    settings.feedback.db_path = tmp_path / "state.db"
    ledger = ActivationLedger(db_path=settings.feedback.db_path)
    for i in range(4):
        _ask(ledger, f"q{i}", [A, B])

    result, letter_themes = agent_observe._cluster_themes(
        settings,
        object(),
        "s1",
        dry_run=True,
        progress=None,
        stats=agent_observe.ObserveStats(snapshot_id="s1"),
    )

    assert result is None
    assert letter_themes == []
    assert ThemeStore(db_path=settings.feedback.db_path).all() == []


def test_clustering_can_be_switched_off(tmp_path: Path):
    from my_daemon.config import Settings
    from my_daemon.pipeline import agent_observe

    settings = Settings()
    settings.feedback.db_path = tmp_path / "state.db"
    settings.consolidation.cluster_themes = False

    assert agent_observe._cluster_themes(
        settings, object(), "s1", dry_run=False, progress=None,
        stats=agent_observe.ObserveStats(snapshot_id="s1"),
    ) == (None, [])
