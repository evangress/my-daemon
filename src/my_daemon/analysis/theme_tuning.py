# SPDX-License-Identifier: Apache-2.0
"""Turn the theme match threshold from a guess into a measurement.

``themes.DEFAULT_MATCH_THRESHOLD`` decides whether tonight's cluster inherits
an existing theme's id and label or mints a new one. It is therefore the knob
that directly controls how much the themes a user sees churn from night to
night — a theme whose name changes every morning is worse than no theme — and
it shipped as a judgement call with no way to check it.

This module replays the user's own ledger the way ``daemon consolidate`` would
have seen it: split the query history into sequential windows, and for each
candidate threshold run cluster → reconcile through those windows in order
against a throwaway store. What comes back is, per threshold, how many themes
survived, how many were created versus matched, and the mean churn — the same
``1 - mean Jaccard`` the observer letter already reports.

Two disciplines the implementation is careful about:

*Never touch live state.* The sweep runs against the real ledger, so each
threshold gets its own temporary database. A tuning run that minted themes
would change the thing it was measuring.

*Never share state between thresholds.* Each is evaluated from an empty store,
or the first threshold's themes seed the second and the sweep measures
evaluation order instead of the parameter.

The windowing is cumulative rather than disjoint — window *n* clusters
everything up to its end date, which is what consolidate actually does, since
it re-clusters the whole lookback every night rather than only the new queries.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from my_daemon.analysis.themes import cluster_fingerprints, reconcile_themes
from my_daemon.stores.activations import INTENTIONAL_SURFACES, ActivationLedger
from my_daemon.stores.themes import ThemeStore

#: Swept by default: coarse enough to run in seconds on a real ledger, fine
#: enough to see the shape of the curve around the shipped 0.60.
DEFAULT_MATCH_THRESHOLD_SWEEP: tuple[float, ...] = (0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9)

#: Below this the churn number is being computed from too few reconciliations
#: to mean anything, and `recommend` says so by declining to answer.
_MIN_WINDOWS_FOR_A_RECOMMENDATION = 2


@dataclass(frozen=True)
class ThresholdReport:
    threshold: float
    windows_evaluated: int
    themes_final: int
    themes_created: int
    themes_matched: int
    themes_dormant: int
    mean_churn: float
    mean_cluster_size: float


def sweep_match_threshold(
    ledger: ActivationLedger,
    *,
    thresholds: tuple[float, ...] = DEFAULT_MATCH_THRESHOLD_SWEEP,
    windows: int = 4,
    min_cluster_size: int = 3,
    limit: int = 4000,
    surfaces: tuple[str, ...] = INTENTIONAL_SURFACES,
) -> list[ThresholdReport]:
    """Replay the ledger in `windows` sequential cuts at each threshold."""

    cuts = _window_cuts(ledger, windows=windows, surfaces=surfaces)
    return [
        _evaluate(
            ledger,
            threshold=threshold,
            cuts=cuts,
            min_cluster_size=min_cluster_size,
            limit=limit,
            surfaces=surfaces,
        )
        for threshold in thresholds
    ]


def recommend(reports: list[ThresholdReport]) -> ThresholdReport | None:
    """The lowest-churn threshold that still finds themes at all.

    Churn alone would recommend the degenerate low end, where every cluster
    matches something and nothing ever changes because nothing is ever
    distinguished. Requiring surviving themes rules that out; ties break toward
    the higher threshold, which is the more conservative reading of "these two
    clusters are the same concern".

    Returns ``None`` when no threshold found a theme, or when there were too
    few windows to have measured churn — an honest "not enough history yet"
    beats a number picked out of a table of zeroes.
    """

    usable = [
        r
        for r in reports
        if r.themes_final > 0 and r.windows_evaluated >= _MIN_WINDOWS_FOR_A_RECOMMENDATION
    ]
    if not usable:
        return None
    return min(usable, key=lambda r: (r.mean_churn, -r.threshold))


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _window_cuts(
    ledger: ActivationLedger,
    *,
    windows: int,
    surfaces: tuple[str, ...],
) -> list[datetime]:
    """End timestamps for each cumulative window, earliest first."""

    span = _timespan(ledger, surfaces=surfaces)
    if span is None or windows < 1:
        return []
    first, last = span
    if last <= first:
        return [last]
    step = (last - first) / windows
    # `+ step` so the first cut already contains a window's worth of history
    # rather than a single instant, and the last cut lands on (or past) `last`.
    return [first + step * (i + 1) for i in range(windows)]


def _timespan(
    ledger: ActivationLedger, *, surfaces: tuple[str, ...]
) -> tuple[datetime, datetime] | None:
    clause = f"WHERE surface IN ({','.join('?' * len(surfaces))})" if surfaces else ""
    with ledger._connect() as conn:  # noqa: SLF001 — same package, read-only probe
        row = conn.execute(
            f"SELECT MIN(ts) AS first, MAX(ts) AS last FROM queries {clause}",
            list(surfaces) if surfaces else [],
        ).fetchone()
    if row is None or row["first"] is None:
        return None
    return datetime.fromisoformat(row["first"]), datetime.fromisoformat(row["last"])


def _evaluate(
    ledger: ActivationLedger,
    *,
    threshold: float,
    cuts: list[datetime],
    min_cluster_size: int,
    limit: int,
    surfaces: tuple[str, ...],
) -> ThresholdReport:
    if not cuts:
        return ThresholdReport(
            threshold=threshold,
            windows_evaluated=0,
            themes_final=0,
            themes_created=0,
            themes_matched=0,
            themes_dormant=0,
            mean_churn=1.0,
            mean_cluster_size=0.0,
        )

    churns: list[float] = []
    sizes: list[int] = []
    created = matched = dormant = 0
    reconciliations = 0

    with tempfile.TemporaryDirectory(prefix="my-daemon-theme-sweep-") as scratch:
        store = ThemeStore(db_path=Path(scratch) / "sweep.db")
        for cut in cuts:
            clusters = _cluster_up_to(
                ledger,
                cut=cut,
                min_cluster_size=min_cluster_size,
                limit=limit,
                surfaces=surfaces,
            )
            if not clusters:
                continue
            result = reconcile_themes(store, clusters, match_threshold=threshold)
            created += len(result.created)
            matched += len(result.matched)
            dormant += len(result.dormant)
            sizes.extend(len(c.query_ids) for c in clusters)
            # The first reconciliation has no previous partition, so
            # `reconcile_themes` reports churn 0.0 — which means "nothing to
            # compare against", not "perfectly stable". Averaging it in would
            # flatter every threshold, and by an amount that shrinks with the
            # window count, so the same ledger would score differently at
            # `--windows 2` and `--windows 8`.
            if reconciliations:
                churns.append(result.churn)
            reconciliations += 1
        themes_final = len([t for t in store.all() if t.status != "dormant"])

    return ThresholdReport(
        threshold=threshold,
        windows_evaluated=reconciliations,
        themes_final=themes_final,
        themes_created=created,
        themes_matched=matched,
        themes_dormant=dormant,
        # No reconciliation ran means nothing was held stable, which is a churn
        # of 1.0 — not 0.0, which would read as perfect stability.
        mean_churn=sum(churns) / len(churns) if churns else 1.0,
        mean_cluster_size=sum(sizes) / len(sizes) if sizes else 0.0,
    )


def _cluster_up_to(
    ledger: ActivationLedger,
    *,
    cut: datetime,
    min_cluster_size: int,
    limit: int,
    surfaces: tuple[str, ...],
):  # noqa: ANN202 — list[FingerprintCluster], imported lazily by cluster_fingerprints
    """Cluster exactly the queries the run at `cut` would have seen.

    The right edge has to be pushed down into the SQL. Clustering the whole
    corpus and discarding clusters that reach past the cut gives a different —
    and wrong — answer, because the partition itself was computed with
    knowledge of queries that had not happened yet.
    """

    return cluster_fingerprints(
        ledger,
        min_cluster_size=min_cluster_size,
        limit=limit,
        until=cut,
        surfaces=surfaces,
    )
