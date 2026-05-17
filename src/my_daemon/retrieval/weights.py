# SPDX-License-Identifier: Apache-2.0
"""Adaptive edge weights from implicit feedback.

The graph starts with every edge at ``weight = 1.0``. When the user picks a
candidate from the retrieved set (a ``candidate_selected`` feedback signal),
we walk the shortest path from the seed note that produced that candidate to
the selected candidate's note and reinforce every edge on the way. Reinforced
edges feel "shorter" to the weighted-Dijkstra traversal in
``stores.graph.GraphStore.neighbors_within``, so future expansions through
those edges rank higher.

The opposite half-life — ``decay_unused_edges`` — pulls weights back toward
1.0 over time so a topic the user has cooled on doesn't dominate forever. It's
called from the nightly consolidation pipeline (Milestone 4), not online.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from my_daemon.stores.graph import GraphStore

DEFAULT_ALPHA = 0.5
DEFAULT_HOP_DECAY = 0.7
DEFAULT_CEILING = 5.0
DEFAULT_HALF_LIFE_DAYS = 30


@dataclass
class ReinforcementResult:
    path: list[str]            # node ids on the reinforced path (start → end)
    edges_reinforced: int      # count of (u, v) edge instances bumped
    total_delta: float         # sum of weight increments applied


@dataclass
class DecayResult:
    edges_visited: int
    edges_decayed: int         # only those whose weight changed by > epsilon
    total_delta: float         # sum of |weight - new_weight| over decayed edges


def apply_selection(
    graph_store: GraphStore,
    *,
    seed_note_path: str,
    selected_note_path: str,
    alpha: float = DEFAULT_ALPHA,
    hop_decay: float = DEFAULT_HOP_DECAY,
    ceiling: float = DEFAULT_CEILING,
    now: datetime | None = None,
) -> ReinforcementResult:
    """Reinforce edges on the shortest path from seed to selected note.

    When the seed *is* the selected note (graph_distance == 0 candidate, the
    most common case for a dense-search hit), there are no edges to reinforce
    and the result is empty — that's fine; the seed already won on its own.
    """

    if now is None:
        now = datetime.now(UTC)
    iso_now = now.isoformat()

    path = graph_store.shortest_note_path(seed_note_path, selected_note_path)
    if not path or len(path) < 2:
        return ReinforcementResult(path=path or [], edges_reinforced=0, total_delta=0.0)

    g = graph_store.graph
    edges_touched = 0
    total_delta = 0.0

    for hop_index, (u, v) in enumerate(zip(path[:-1], path[1:], strict=True)):
        bump = alpha * (hop_decay ** hop_index)
        # The graph is a MultiDiGraph: there can be parallel edges (e.g. a tag
        # edge AND a wikilink between the same nodes if a note both names and
        # tags another), and the original direction may be either u→v or v→u
        # since shortest_path uses the undirected projection.
        for src, dst in ((u, v), (v, u)):
            if not g.has_edge(src, dst):
                continue
            for key in list(g[src][dst].keys()):
                edata = g[src][dst][key]
                current = float(edata.get("weight", 1.0))
                new_w = min(current + bump, ceiling)
                edata["weight"] = new_w
                edata["last_reinforced_at"] = iso_now
                edges_touched += 1
                total_delta += new_w - current

    return ReinforcementResult(
        path=path,
        edges_reinforced=edges_touched,
        total_delta=total_delta,
    )


def decay_unused_edges(
    graph_store: GraphStore,
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: datetime | None = None,
    epsilon: float = 1e-4,
) -> DecayResult:
    """Pull every reinforced edge's weight back toward 1.0 by a half-life curve.

    new_weight = 1.0 + (current - 1.0) * 0.5 ** (days_since / half_life_days)

    Edges that have never been reinforced (no ``last_reinforced_at`` stamp)
    are skipped — they're already at the neutral baseline.
    """

    if now is None:
        now = datetime.now(UTC)

    visited = 0
    decayed = 0
    total_delta = 0.0
    for _u, _v, edata in graph_store.graph.edges(data=True):
        stamp = edata.get("last_reinforced_at")
        if not stamp:
            continue
        visited += 1
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        days = max((now - last).total_seconds() / 86400.0, 0.0)
        factor = math.pow(0.5, days / half_life_days)
        current = float(edata.get("weight", 1.0))
        new_w = 1.0 + (current - 1.0) * factor
        if abs(current - new_w) <= epsilon:
            continue
        edata["weight"] = new_w
        decayed += 1
        total_delta += abs(current - new_w)

    return DecayResult(edges_visited=visited, edges_decayed=decayed, total_delta=total_delta)
