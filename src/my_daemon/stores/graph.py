# SPDX-License-Identifier: Apache-2.0
"""NetworkX MultiDiGraph wrapper with pickle persistence."""

from __future__ import annotations

import pickle
from collections import Counter, deque
from pathlib import Path

import networkx as nx

from my_daemon.models import GraphStats, Note


def _tag_node(tag: str) -> str:
    return f"tag::{tag}"


def _note_node(rel_path: str) -> str:
    return f"note::{rel_path}"


class GraphStore:
    """Notes, tags, and the wikilink/tag edges between them.

    Nodes carry a ``type`` attribute (``"note"`` or ``"tag"``) so callers can filter
    cleanly. Chunk ids belonging to each note are tracked on the node so retrieval
    expansion can pull all chunks of a neighbor without a separate index.
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self.graph: nx.MultiDiGraph = nx.MultiDiGraph()

    def load(self) -> None:
        if self.path.is_file():
            with self.path.open("rb") as fh:
                self.graph = pickle.load(fh)
        else:
            self.graph = nx.MultiDiGraph()

    def save(self) -> None:
        if self.read_only:
            raise RuntimeError("GraphStore is read-only (opened from a snapshot bundle)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("wb") as fh:
            pickle.dump(self.graph, fh)

    def add_note(self, note: Note, chunk_ids: list[str]) -> None:
        node = _note_node(note.relative_path)
        self.graph.add_node(
            node,
            type="note",
            title=note.title,
            mtime=note.mtime.isoformat(),
            chunk_ids=chunk_ids,
        )
        # If this node was previously a wikilink placeholder, promote it now.
        self.graph.nodes[node].pop("dangling", None)

        for target in note.wikilinks:
            target_node = _note_node(target)
            if target_node not in self.graph:
                # Dangling wikilinks become placeholder note nodes — keeps the graph
                # complete, and a later ingest can fill the target in.
                self.graph.add_node(target_node, type="note", title=target, dangling=True)
            self.graph.add_edge(node, target_node, kind="wikilink", weight=1.0)

        for tag in note.tags:
            tnode = _tag_node(tag)
            if tnode not in self.graph:
                self.graph.add_node(tnode, type="tag", title=tag)
            self.graph.add_edge(node, tnode, kind="tag", weight=1.0)

    def update_note(self, note: Note, chunk_ids: list[str]) -> None:
        """Re-ingest a note *differentially*, preserving what it doesn't own.

        A note owns its outgoing wikilink and tag edges and nothing else. This
        refreshes the node's attributes, adds the edges the author introduced,
        removes the ones they deleted, and — crucially — leaves surviving edges
        completely untouched, so the ``weight`` and ``last_reinforced_at`` that
        ``retrieval.weights`` accumulated on them carry across the edit.

        Prefer this over ``remove_note`` + ``add_note``: ``remove_node`` drops
        all incident edges in *both* directions, which deletes every other
        note's links *to* this one, and ``add_note`` recreates edges at
        ``weight=1.0``, discarding everything M1 has learned.

        A link the author deletes and later restores correctly starts over at
        1.0 — it genuinely left the set.
        """

        node = _note_node(note.relative_path)
        if node not in self.graph:
            self.add_note(note, chunk_ids)
            return

        self.graph.add_node(
            node,
            type="note",
            title=note.title,
            mtime=note.mtime.isoformat(),
            chunk_ids=chunk_ids,
        )
        self.graph.nodes[node].pop("dangling", None)

        desired: dict[tuple[str, str], str] = {
            (_note_node(target), "wikilink"): target for target in note.wikilinks
        }
        desired.update({(_tag_node(tag), "tag"): tag for tag in note.tags})

        existing: dict[tuple[str, str], list] = {}
        for _src, dst, key, edata in list(
            self.graph.out_edges(node, keys=True, data=True)
        ):
            kind = edata.get("kind")
            if kind not in ("wikilink", "tag"):
                continue  # not ours to manage — leave any other edge kind alone
            existing.setdefault((dst, kind), []).append(key)

        for slot, keys in existing.items():
            if slot not in desired:
                for key in keys:
                    self.graph.remove_edge(node, slot[0], key)

        for slot, label in desired.items():
            if slot in existing:
                continue  # survives the diff — its learned weight stays as-is
            dst, kind = slot
            if dst not in self.graph:
                if kind == "wikilink":
                    self.graph.add_node(dst, type="note", title=label, dangling=True)
                else:
                    self.graph.add_node(dst, type="tag", title=label)
            self.graph.add_edge(node, dst, kind=kind, weight=1.0)

    def remove_note(self, rel_path: str) -> None:
        node = _note_node(rel_path)
        if node in self.graph:
            self.graph.remove_node(node)

    def chunk_ids_for(self, rel_path: str) -> list[str]:
        node = _note_node(rel_path)
        if node not in self.graph:
            return []
        return list(self.graph.nodes[node].get("chunk_ids", []))

    def neighbors_within(
        self,
        rel_path: str,
        depth: int,
        *,
        weighted: bool = False,
    ) -> dict[str, float]:
        """Distances from a note to reachable neighbor notes, up to ``depth`` hops.

        Walks both wikilink (note→note) and tag (note→tag→note) edges. With
        ``weighted=False`` distance is integer hops in the undirected projection.
        With ``weighted=True`` it's a Dijkstra distance over ``1/edge.weight`` —
        so reinforced edges feel shorter and neighbors behind them rank higher
        after the decay multiplier in ``retrieval.expand``. The hop budget
        (``depth``) is still applied as an integer-hop cap so reinforcement
        can't pull a chunk in from arbitrarily far away.
        """

        start = _note_node(rel_path)
        if start not in self.graph:
            return {}

        ug = self.graph.to_undirected(as_view=True)

        # Hop budget first — bounds the reachable set independent of weight.
        hop_seen = {start: 0}
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            d = hop_seen[current]
            if d >= depth:
                continue
            for nb in ug.neighbors(current):
                if nb in hop_seen:
                    continue
                hop_seen[nb] = d + 1
                queue.append(nb)

        if weighted:
            # On a MultiGraph, Dijkstra hands the weight callback a dict of
            # parallel-edge data: ``{edge_key: {"weight": ..., ...}, ...}``.
            # We take the strongest (highest weight) of the parallel edges, so
            # a reinforced edge cancels out any weaker parallel siblings.
            def _edge_cost(_u: str, _v: str, edata: dict) -> float:
                weights = [
                    float(d.get("weight", 1.0))
                    for d in edata.values()
                    if isinstance(d, dict)
                ] or [float(edata.get("weight", 1.0))]
                return 1.0 / max(max(weights), 0.1)

            try:
                weighted_dist = nx.single_source_dijkstra_path_length(
                    ug.subgraph(hop_seen.keys()),
                    start,
                    weight=_edge_cost,
                )
            except nx.NodeNotFound:
                weighted_dist = {start: 0.0}
            distances: dict[str, float] = {n: float(weighted_dist.get(n, hop_seen[n])) for n in hop_seen}
        else:
            distances = {n: float(d) for n, d in hop_seen.items()}

        notes_only: dict[str, float] = {}
        for node, dist in distances.items():
            if node == start:
                continue
            if self.graph.nodes[node].get("type") != "note":
                continue
            if self.graph.nodes[node].get("dangling"):
                continue
            rel = node.removeprefix("note::")
            notes_only[rel] = dist
        return notes_only

    def shortest_note_path(self, rel_from: str, rel_to: str) -> list[str] | None:
        """Return the shortest hop path between two notes as a list of node ids.

        Used by ``retrieval.weights.apply_selection`` to walk the edges it
        should reinforce. Returns ``None`` if either endpoint is missing or
        no path exists.
        """

        a, b = _note_node(rel_from), _note_node(rel_to)
        if a not in self.graph or b not in self.graph:
            return None
        ug = self.graph.to_undirected(as_view=True)
        try:
            return nx.shortest_path(ug, a, b)
        except nx.NetworkXNoPath:
            return None

    def stats(self) -> GraphStats:
        notes = [n for n, d in self.graph.nodes(data=True) if d.get("type") == "note" and not d.get("dangling")]
        tag_nodes = [n for n, d in self.graph.nodes(data=True) if d.get("type") == "tag"]
        try:
            pr = nx.pagerank(self.graph) if self.graph.number_of_nodes() else {}
        except nx.PowerIterationFailedConvergence:
            pr = {}
        top_pr = sorted(
            ((n.removeprefix("note::"), s) for n, s in pr.items() if self.graph.nodes[n].get("type") == "note"),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        tag_counts = Counter(
            t.removeprefix("tag::") for t in tag_nodes
        )
        # rank tags by note-degree
        tag_degree = sorted(
            (
                (t.removeprefix("tag::"), self.graph.in_degree(t) + self.graph.out_degree(t))
                for t in tag_nodes
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        _ = tag_counts  # reserved for future use; currently degree-based
        return GraphStats(
            note_count=len(notes),
            tag_count=len(tag_nodes),
            edge_count=self.graph.number_of_edges(),
            top_pagerank=top_pr,
            top_tags=tag_degree,
        )
