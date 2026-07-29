# SPDX-License-Identifier: Apache-2.0
"""Cluster query fingerprints into emergent themes.

Runs offline, inside ``daemon consolidate``, against a frozen snapshot — the
same dream phase that writes the observer letter. Nothing here touches the hot
path.

The hard part is not the clustering. It is **stability**: a theme whose label
changes every night is worse than no theme at all. Hence centroid matching,
label locking, blended centroids, dormancy instead of deletion, and an honest
churn metric the letter can admit to.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from my_daemon.models import is_daemon_authored_tag
from my_daemon.stores.activations import INTENTIONAL_SURFACES, ActivationLedger
from my_daemon.stores.themes import ThemeStore

# Historical alias. One implementation, or the exclusions drift apart.
is_self_referential_tag = is_daemon_authored_tag

# Cosine similarity above which a new cluster is judged to be the *same* theme
# as an existing one, and inherits its id and label.
DEFAULT_MATCH_THRESHOLD = 0.60

# A query must actually resemble its own cluster's centroid to belong to it.
# HDBSCAN's noise label is necessary but not sufficient: on a small corpus the
# mutual-reachability distances degenerate, and a query sharing *zero* notes
# with a cluster still gets absorbed. Since "these queries lit up the same
# notes" is the whole meaning of a theme, enforce it rather than trusting the
# clusterer's edge-case behaviour.
DEFAULT_MIN_MEMBER_SIMILARITY = 0.10
_TOP_CENTROID_NOTES = 12


@dataclass
class FingerprintCluster:
    query_ids: list[int]
    centroid: dict[str, float]
    representative_queries: list[str] = field(default_factory=list)


@dataclass
class ReconcileResult:
    matched: list[tuple[int, FingerprintCluster]] = field(default_factory=list)
    created: list[tuple[int, FingerprintCluster]] = field(default_factory=list)
    dormant: list[int] = field(default_factory=list)
    # Only *new* clusters need an LLM label — steady state is zero a night.
    needs_naming: list[tuple[int, FingerprintCluster]] = field(default_factory=list)
    churn: float = 0.0


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    dot = sum(a[k] * b[k] for k in shared)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def cluster_fingerprints(
    ledger: ActivationLedger,
    *,
    min_cluster_size: int = 3,
    min_member_similarity: float = DEFAULT_MIN_MEMBER_SIMILARITY,
    limit: int = 4000,
    since=None,  # noqa: ANN001
    until=None,  # noqa: ANN001 — right edge; only the tuning replay needs it
    surfaces: tuple[str, ...] = INTENTIONAL_SURFACES,
) -> list[FingerprintCluster]:
    """HDBSCAN over cosine distance between query fingerprints.

    HDBSCAN's noise label matters: **most queries should not get a theme.**
    Forcing every query into a cluster is how you get meaningless ones.
    """

    rows = ledger.query_fingerprints(limit=limit, since=since, until=until, surfaces=surfaces)
    if len(rows) < min_cluster_size:
        return []

    import numpy as np
    from sklearn.cluster import HDBSCAN

    query_ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    fingerprints = [r[2] for r in rows]

    n = len(fingerprints)
    distance = np.zeros((n, n), dtype=np.float32)
    for i in range(n):
        for j in range(i + 1, n):
            d = 1.0 - cosine(fingerprints[i], fingerprints[j])
            distance[i, j] = distance[j, i] = max(d, 0.0)

    # `allow_single_cluster` stays off: letting HDBSCAN call the whole corpus
    # one cluster is precisely how a homogeneous vault produces one meaningless
    # mega-theme. `copy=True` leaves the caller's matrix alone (and silences a
    # sklearn 1.10 deprecation).
    labels = HDBSCAN(
        metric="precomputed", min_cluster_size=min_cluster_size, copy=True
    ).fit_predict(distance.astype(np.float64))

    grouped: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        if label < 0:  # noise — deliberately left themeless
            continue
        grouped.setdefault(int(label), []).append(index)

    clusters: list[FingerprintCluster] = []
    for members in grouped.values():
        if len(members) < min_cluster_size:
            continue
        # Leave-one-out: a member must resemble the cluster *without* its own
        # contribution. Scoring against a centroid it helped build would let an
        # orphan drag the centroid toward itself and then pass on the strength
        # of its own pull.
        members = [
            i
            for i in members
            if cosine(
                fingerprints[i],
                _centroid([fingerprints[j] for j in members if j != i]),
            )
            >= min_member_similarity
        ]
        if len(members) < min_cluster_size:
            continue
        centroid = _centroid([fingerprints[i] for i in members])
        ranked = sorted(members, key=lambda i: cosine(fingerprints[i], centroid), reverse=True)
        clusters.append(
            FingerprintCluster(
                query_ids=[query_ids[i] for i in members],
                centroid=centroid,
                representative_queries=[texts[i] for i in ranked[:3]],
            )
        )
    return clusters


def _centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    total: dict[str, float] = {}
    for v in vectors:
        for note_uuid, weight in v.items():
            total[note_uuid] = total.get(note_uuid, 0.0) + weight
    top = dict(sorted(total.items(), key=lambda kv: -kv[1])[:_TOP_CENTROID_NOTES])
    magnitude = math.sqrt(sum(v * v for v in top.values()))
    return {k: v / magnitude for k, v in top.items()} if magnitude else top


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "theme"


def reconcile_themes(
    store: ThemeStore,
    clusters: list[FingerprintCluster],
    *,
    snapshot_id: str | None = None,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> ReconcileResult:
    """Match this run's clusters onto existing themes by optimal assignment.

    A match reuses the theme's id, label, and summary — no LLM call and no
    churn in what the user sees. That is what makes the *labels* far more
    stable than the underlying partitions, which over a mutating vault are
    inherently unstable.

    The pairing is a maximum-weight bipartite matching, solved exactly by the
    Hungarian algorithm (Kuhn, 1955) via ``scipy.optimize.linear_sum_assignment``.
    It used to be greedy best-first, which is not merely approximate but can be
    arbitrarily worse: one high-scoring pair claiming a theme can strand a
    cluster whose only viable partner it just took, and every stranding is
    user-visible *twice* — as a theme that appears out of nowhere and another
    that goes dormant for no reason the user can see. Optimising the total
    rather than the first pick removes that failure mode entirely.

    The threshold is applied *after* assignment, so a pairing the user would
    not accept is never smuggled in just because it improved the sum.
    """

    result = ReconcileResult()
    existing = store.all()
    before = {t.id: set(t.centroid) for t in existing}

    claimed_clusters: set[int] = set()
    claimed_themes: set[int] = set()
    for score, ci, theme_id in _optimal_pairs(clusters, existing):
        if score < match_threshold:
            continue
        claimed_clusters.add(ci)
        claimed_themes.add(theme_id)
        cluster = clusters[ci]
        store.touch(
            theme_id,
            centroid=cluster.centroid,
            query_count=len(cluster.query_ids),
            snapshot_id=snapshot_id,
        )
        store.set_notes(theme_id, cluster.centroid)
        result.matched.append((theme_id, cluster))

    for ci, cluster in enumerate(clusters):
        if ci in claimed_clusters:
            continue
        # Placeholder label until the observer names it; slug stays unique.
        provisional = ", ".join(cluster.representative_queries[:1]) or "unnamed"
        theme_id = store.create(
            slug=f"{slugify(provisional)[:40]}-{abs(hash(tuple(sorted(cluster.centroid)))) % 10_000}",
            label=provisional[:80],
            centroid=cluster.centroid,
            snapshot_id=snapshot_id,
            query_count=len(cluster.query_ids),
        )
        store.set_notes(theme_id, cluster.centroid)
        result.created.append((theme_id, cluster))
        result.needs_naming.append((theme_id, cluster))

    for theme in existing:
        if theme.id not in claimed_themes:
            store.mark_missing(theme.id)
            result.dormant.append(theme.id)

    result.churn = _churn(before, result)
    return result


def _optimal_pairs(
    clusters: list[FingerprintCluster],
    existing: list,  # noqa: ANN001 — list[Theme], avoiding a circular import
) -> list[tuple[float, int, int]]:
    """(similarity, cluster_index, theme_id) for the best overall pairing.

    Returns at most ``min(len(clusters), len(existing))`` pairs, since each
    cluster and each theme can be claimed once. Scores are returned unfiltered;
    the caller applies the acceptance threshold.
    """

    if not clusters or not existing:
        return []

    import numpy as np
    from scipy.optimize import linear_sum_assignment

    similarity = np.array(
        [[cosine(cluster.centroid, theme.centroid) for theme in existing] for cluster in clusters],
        dtype=np.float64,
    )
    # linear_sum_assignment minimises, and we want the maximum-weight matching.
    rows, cols = linear_sum_assignment(-similarity)
    return [
        (float(similarity[row, col]), int(row), existing[col].id)
        for row, col in zip(rows, cols, strict=True)
    ]


def _churn(before: dict[int, set[str]], result: ReconcileResult) -> float:
    """1 - mean Jaccard of theme membership against the previous run.

    Reported honestly in the observer letter: above roughly 0.5 the themes
    aren't settled, and the letter should say so rather than present noise as
    insight.
    """

    if not before:
        return 0.0
    scores = []
    for theme_id, cluster in result.matched:
        old, new = before.get(theme_id, set()), set(cluster.centroid)
        union = old | new
        scores.append(len(old & new) / len(union) if union else 1.0)
    for _theme_id in result.dormant:
        scores.append(0.0)
    return 1.0 - (sum(scores) / len(scores)) if scores else 1.0
