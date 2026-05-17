"""Structural-pattern analysis over a snapshot's graph + feedback.

Two passes — both pure-Python, no LLM:

- :func:`compute_report` walks the graph in the snapshot bundle and produces a
  :class:`StructuralReport`: Louvain communities, sampled betweenness
  (bridging notes), ``nx.bridges`` (load-bearing links), orphans, dangling
  wikilink targets, and the warmest reinforced edges. This is the M3
  deliverable: a structured view of what the graph has *learned* so far.
- :func:`simulate_evolution` replays recent ``candidate_selected`` events
  against a deep copy of the snapshot graph and reports the hypothetical
  weight deltas. Useful as a "what would the next decay/reinforce cycle do
  to the graph" preview without touching live state.

Reports persist as JSON under ``settings.consolidation.out_dir/<snapshot_id>/``.
"""

from __future__ import annotations

import copy
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import networkx as nx

from my_daemon.models import (
    BridgeEdge,
    BridgingNote,
    CommunitySummary,
    DanglingTarget,
    EdgeWeightDelta,
    FeedbackEvent,
    NoteWeightDelta,
    StructuralReport,
    WarmEdge,
    WeightEvolutionReport,
)
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores.graph import GraphStore
from my_daemon.stores.snapshot import SnapshotBundle, open_readonly

_NOTE_PREFIX = "note::"
_TAG_PREFIX = "tag::"

# Default cutoff for "warm" edges in the report. Edges above this are surfaced
# as the graph's hot regions. Reinforcement starts at 1.0 and asymptotes at
# DEFAULT_CEILING == 5.0, so 1.5 is a meaningful "has been touched more than
# trivially" threshold.
DEFAULT_WARM_THRESHOLD = 1.5


# ---------------------------------------------------------------------------
# compute_report
# ---------------------------------------------------------------------------


def compute_report(
    graph_store: GraphStore,
    *,
    snapshot_id: str | None = None,
    max_communities: int = 8,
    max_bridging_notes: int = 10,
    max_bridge_edges: int = 20,
    max_orphans: int = 20,
    max_dangling: int = 20,
    max_warm_edges: int = 20,
    warm_threshold: float = DEFAULT_WARM_THRESHOLD,
    betweenness_sample_k: int = 200,
    now: datetime | None = None,
) -> StructuralReport:
    """Compute every structural metric in one pass.

    Operates on the snapshot's graph, never live. Returns a fully-populated
    :class:`StructuralReport`. Empty/degenerate graphs yield a report with
    zeroes — the caller decides whether that's worth narrating.
    """

    moment = now or datetime.now(UTC)
    g = graph_store.graph
    undirected = g.to_undirected(as_view=False)  # mutable view for algorithms

    note_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("type") == "note" and not d.get("dangling")
    }
    tag_nodes = {n for n, d in g.nodes(data=True) if d.get("type") == "tag"}
    dangling_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("type") == "note" and d.get("dangling")
    }

    communities = _louvain_communities(g, undirected, max_communities=max_communities)
    bridging = _bridging_notes(
        undirected, note_nodes, k=betweenness_sample_k, limit=max_bridging_notes
    )
    bridge_edges = _bridge_edges(g, undirected, note_nodes, limit=max_bridge_edges)
    orphans = _orphan_notes(undirected, note_nodes, limit=max_orphans)
    dangling = _dangling_targets(g, dangling_nodes, limit=max_dangling)
    warm = _warm_edges(g, threshold=warm_threshold, limit=max_warm_edges)

    return StructuralReport(
        snapshot_id=snapshot_id,
        generated_at=moment,
        note_count=len(note_nodes),
        tag_count=len(tag_nodes),
        edge_count=g.number_of_edges(),
        community_count=len(communities),
        communities=communities,
        bridging_notes=bridging,
        bridge_edges=bridge_edges,
        orphan_notes=orphans,
        dangling_targets=dangling,
        warm_edges=warm,
    )


def _louvain_communities(
    g: nx.MultiDiGraph,
    undirected: nx.Graph,
    *,
    max_communities: int,
) -> list[CommunitySummary]:
    """Louvain on the full undirected projection (tags carry useful signal).

    Members reported are note-only — tags are clustering glue, not output.
    Communities of size 1 (singleton notes) are pruned: they're already
    visible in the orphans list and only add noise here.
    """

    if undirected.number_of_nodes() == 0:
        return []
    try:
        raw = nx.community.louvain_communities(undirected, seed=0)
    except Exception:  # noqa: BLE001 — degenerate graphs can raise; degrade gracefully
        return []

    summaries: list[CommunitySummary] = []
    for idx, comm in enumerate(sorted(raw, key=len, reverse=True)):
        note_members = sorted(
            n.removeprefix(_NOTE_PREFIX)
            for n in comm
            if n.startswith(_NOTE_PREFIX) and not g.nodes[n].get("dangling")
        )
        if len(note_members) < 2:
            continue
        tag_counts = Counter(
            n.removeprefix(_TAG_PREFIX) for n in comm if n.startswith(_TAG_PREFIX)
        )
        summaries.append(
            CommunitySummary(
                community_id=idx,
                size=len(note_members),
                members=note_members[:20],
                top_tags=tag_counts.most_common(5),
            )
        )
        if len(summaries) >= max_communities:
            break
    return summaries


def _bridging_notes(
    undirected: nx.Graph,
    note_nodes: set[str],
    *,
    k: int,
    limit: int,
) -> list[BridgingNote]:
    """Top notes by betweenness centrality, sampled for tractability.

    Sampled BC because the exact O(VE) form costs too much on real vaults.
    """

    if undirected.number_of_nodes() < 3:
        return []
    sample = min(k, undirected.number_of_nodes())
    try:
        bc = nx.betweenness_centrality(undirected, k=sample, seed=0)
    except Exception:  # noqa: BLE001
        return []
    ranked = sorted(
        ((n.removeprefix(_NOTE_PREFIX), s) for n, s in bc.items() if n in note_nodes and s > 0),
        key=lambda x: x[1],
        reverse=True,
    )[:limit]
    return [BridgingNote(note_path=name, betweenness=score) for name, score in ranked]


def _bridge_edges(
    g: nx.MultiDiGraph,
    undirected: nx.Graph,
    note_nodes: set[str],
    *,
    limit: int,
) -> list[BridgeEdge]:
    """Note-to-note edges whose removal would split a component."""

    out: list[BridgeEdge] = []
    try:
        edges = list(nx.bridges(undirected))
    except nx.NetworkXNotImplemented:
        return []
    for u, v in edges:
        if u not in note_nodes or v not in note_nodes:
            # Skip note-tag bridges; they're noisy ("this note is the only one
            # tagged X" is not the structural insight we want here).
            continue
        kind, weight = _summarize_parallel_edges(g, u, v)
        out.append(
            BridgeEdge(
                src=u.removeprefix(_NOTE_PREFIX),
                dst=v.removeprefix(_NOTE_PREFIX),
                kind=kind,
                weight=weight,
            )
        )
        if len(out) >= limit:
            break
    return out


def _orphan_notes(
    undirected: nx.Graph,
    note_nodes: set[str],
    *,
    limit: int,
) -> list[str]:
    """Notes with at most one unique neighbor — candidates to link or archive.

    Degree is measured against the *undirected* projection of the full graph
    (so a note tagged once but with no wikilinks still registers as
    poorly-connected). Dangling targets are excluded from the input set.
    """

    out: list[str] = []
    for n in sorted(note_nodes):
        if undirected.degree(n) <= 1:
            out.append(n.removeprefix(_NOTE_PREFIX))
            if len(out) >= limit:
                break
    return out


def _dangling_targets(
    g: nx.MultiDiGraph,
    dangling_nodes: set[str],
    *,
    limit: int,
) -> list[DanglingTarget]:
    """Wikilink targets without a backing note, ranked by how many notes name them."""

    ranked = sorted(
        ((n.removeprefix(_NOTE_PREFIX), g.in_degree(n)) for n in dangling_nodes),
        key=lambda x: x[1],
        reverse=True,
    )[:limit]
    return [DanglingTarget(target=name, incoming_links=count) for name, count in ranked]


def _warm_edges(
    g: nx.MultiDiGraph,
    *,
    threshold: float,
    limit: int,
) -> list[WarmEdge]:
    """Reinforced edges above ``threshold``, ranked by weight descending."""

    ranked: list[WarmEdge] = []
    for u, v, edata in g.edges(data=True):
        weight = float(edata.get("weight", 1.0))
        if weight <= threshold:
            continue
        ranked.append(
            WarmEdge(
                src=_strip_prefix(u),
                dst=_strip_prefix(v),
                kind=str(edata.get("kind", "unknown")),
                weight=weight,
                last_reinforced_at=edata.get("last_reinforced_at"),
            )
        )
    ranked.sort(key=lambda e: e.weight, reverse=True)
    return ranked[:limit]


def _summarize_parallel_edges(
    g: nx.MultiDiGraph, u: str, v: str
) -> tuple[str, float]:
    """Collapse parallel edges between u and v into one (kind, max_weight) pair.

    ``nx.bridges`` returns undirected pairs; the underlying MultiDiGraph may
    have edges in either direction (and multiple kinds in parallel). We
    summarize by listing kinds and reporting the heaviest weight, which is
    what the bridge effectively contributes to the structure.
    """

    kinds: set[str] = set()
    max_weight = 0.0
    for src, dst in ((u, v), (v, u)):
        if not g.has_edge(src, dst):
            continue
        for data in g[src][dst].values():
            kinds.add(str(data.get("kind", "unknown")))
            max_weight = max(max_weight, float(data.get("weight", 1.0)))
    if not kinds:
        return "unknown", 1.0
    kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
    return kind, max_weight


def _strip_prefix(node: str) -> str:
    if node.startswith(_NOTE_PREFIX):
        return node.removeprefix(_NOTE_PREFIX)
    if node.startswith(_TAG_PREFIX):
        return node.removeprefix(_TAG_PREFIX)
    return node


# ---------------------------------------------------------------------------
# simulate_evolution
# ---------------------------------------------------------------------------


def simulate_evolution(
    bundle: SnapshotBundle,
    *,
    lookback_days: int = 7,
    max_edges: int = 20,
    max_notes: int = 20,
    now: datetime | None = None,
) -> WeightEvolutionReport:
    """Replay ``candidate_selected`` events on a copy of the snapshot graph.

    A hypothetical-evolution preview: we never touch live state, and we
    never touch the snapshot graph either — we work on a ``copy.deepcopy``.
    The diff is between the post-replay copy and the snapshot baseline.

    Events without enough information to reconstruct the seed→selected
    path (older feedback rows that pre-date the M1 schema upgrade) are
    counted in ``events_skipped`` but otherwise ignored.
    """

    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=lookback_days)

    handle = open_readonly(bundle)
    try:
        snapshot_graph = handle.graph.graph
        # Shadow store wraps a deepcopy so apply_selection's GraphStore API
        # works unchanged. The shadow's .save() would still raise (read_only
        # propagates), but we don't call it — this is in-memory only.
        shadow = GraphStore(path=Path("/dev/null"), read_only=True)
        shadow.graph = copy.deepcopy(snapshot_graph)

        events = handle.feedback.selections_since(cutoff)
        replayed = 0
        skipped = 0
        for ev in events:
            seed_path, selected_path = _seed_and_selected(ev)
            if not seed_path or not selected_path:
                skipped += 1
                continue
            apply_selection(
                shadow,
                seed_note_path=seed_path,
                selected_note_path=selected_path,
                now=ev.timestamp,
            )
            replayed += 1

        edge_deltas, note_deltas = _diff_graphs(
            snapshot_graph, shadow.graph,
            max_edges=max_edges, max_notes=max_notes,
        )
    finally:
        handle.close()

    return WeightEvolutionReport(
        snapshot_id=bundle.id,
        generated_at=moment,
        lookback_days=lookback_days,
        events_replayed=replayed,
        events_skipped=skipped,
        top_edges=edge_deltas,
        top_notes=note_deltas,
    )


def _seed_and_selected(event: FeedbackEvent) -> tuple[str | None, str | None]:
    """Pull (seed_note_path, selected_note_path) out of a logged feedback row.

    Prefers explicit columns when present; falls back to the ranked summary
    that ``build_retrieval_summary`` writes when only ``selected_rank`` is set
    (older M1 rows might lack ``selected_note_path``).
    """

    selected_note = event.selected_note_path
    ranked = (event.retrieval_summary or {}).get("ranked") or []
    seed_note: str | None = None
    if event.selected_rank and 1 <= event.selected_rank <= len(ranked):
        picked = ranked[event.selected_rank - 1] or {}
        seed_note = picked.get("seed_note_path") or picked.get("note_path")
        if not selected_note:
            selected_note = picked.get("note_path")
    return seed_note, selected_note


def _diff_graphs(
    snapshot: nx.MultiDiGraph,
    shadow: nx.MultiDiGraph,
    *,
    max_edges: int,
    max_notes: int,
) -> tuple[list[EdgeWeightDelta], list[NoteWeightDelta]]:
    """Walk the shadow's edges and surface where weights moved.

    Only edges present in both graphs are compared. We treat the snapshot's
    weight (defaulting to 1.0 if a parallel edge somehow appeared without
    one) as the baseline, the shadow's as the post-replay state.
    """

    edge_deltas: list[EdgeWeightDelta] = []
    note_totals: dict[str, list[float]] = {}

    for u, v, key, edata in shadow.edges(keys=True, data=True):
        after = float(edata.get("weight", 1.0))
        if not snapshot.has_edge(u, v, key=key):
            before = 1.0
        else:
            before = float(snapshot[u][v][key].get("weight", 1.0))
        delta = after - before
        if abs(delta) < 1e-6:
            continue
        edge_deltas.append(
            EdgeWeightDelta(
                src=_strip_prefix(u),
                dst=_strip_prefix(v),
                kind=str(edata.get("kind", "unknown")),
                before=before,
                after=after,
                delta=delta,
            )
        )
        # Attribute delta to both endpoints if they're notes.
        for endpoint in (u, v):
            if endpoint.startswith(_NOTE_PREFIX):
                note_totals.setdefault(endpoint, []).append(abs(delta))

    edge_deltas.sort(key=lambda d: abs(d.delta), reverse=True)
    note_summaries = [
        NoteWeightDelta(
            note_path=node.removeprefix(_NOTE_PREFIX),
            total_delta=sum(deltas),
            edges_changed=len(deltas),
        )
        for node, deltas in note_totals.items()
    ]
    note_summaries.sort(key=lambda d: d.total_delta, reverse=True)

    return edge_deltas[:max_edges], note_summaries[:max_notes]


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def persist_reports(
    out_dir: Path,
    *,
    structural: StructuralReport | None = None,
    evolution: WeightEvolutionReport | None = None,
) -> dict[str, Path]:
    """Write the report JSON files under ``out_dir`` (created if missing).

    Returns a mapping of report name → path so the caller can echo them back
    to the user. Either argument may be omitted (useful when one of the two
    passes was skipped).
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    if structural is not None:
        path = out_dir / "structural.json"
        path.write_text(
            structural.model_dump_json(indent=2),
            encoding="utf-8",
        )
        written["structural"] = path
    if evolution is not None:
        path = out_dir / "weight_evolution.json"
        path.write_text(
            evolution.model_dump_json(indent=2),
            encoding="utf-8",
        )
        written["evolution"] = path
    return written


def report_dir_for(snapshot_id: str, *, root: Path) -> Path:
    """The on-disk directory where reports for one snapshot live."""

    return root / snapshot_id
