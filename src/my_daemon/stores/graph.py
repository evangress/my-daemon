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

    def __init__(self, path: Path) -> None:
        self.path = path
        self.graph: nx.MultiDiGraph = nx.MultiDiGraph()

    def load(self) -> None:
        if self.path.is_file():
            with self.path.open("rb") as fh:
                self.graph = pickle.load(fh)
        else:
            self.graph = nx.MultiDiGraph()

    def save(self) -> None:
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

    def remove_note(self, rel_path: str) -> None:
        node = _note_node(rel_path)
        if node in self.graph:
            self.graph.remove_node(node)

    def chunk_ids_for(self, rel_path: str) -> list[str]:
        node = _note_node(rel_path)
        if node not in self.graph:
            return []
        return list(self.graph.nodes[node].get("chunk_ids", []))

    def neighbors_within(self, rel_path: str, depth: int) -> dict[str, int]:
        """BFS from a note, returning {neighbor_note_relative_path: distance} up to ``depth``.

        Walks both wikilink (note→note) and tag (note→tag→note) edges. Distance is
        measured in hops in the undirected projection so a sibling note via a shared
        tag is at distance 2.
        """

        start = _note_node(rel_path)
        if start not in self.graph:
            return {}

        # Undirected view for BFS so we can traverse both incoming and outgoing edges.
        ug = self.graph.to_undirected(as_view=True)
        seen = {start: 0}
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            d = seen[current]
            if d >= depth:
                continue
            for nb in ug.neighbors(current):
                if nb in seen:
                    continue
                seen[nb] = d + 1
                queue.append(nb)

        notes_only: dict[str, int] = {}
        for node, dist in seen.items():
            if node == start:
                continue
            if self.graph.nodes[node].get("type") != "note":
                continue
            if self.graph.nodes[node].get("dangling"):
                continue
            rel = node.removeprefix("note::")
            notes_only[rel] = dist
        return notes_only

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
