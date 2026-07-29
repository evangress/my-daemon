# SPDX-License-Identifier: Apache-2.0
"""Dangling wikilinks as the notes you keep meaning to write.

A `[[Name]]` with nothing behind it is not a broken link. It is a decision the
user already made — *this deserves its own note* — that has not been carried
out. The graph has kept these as `dangling::` placeholder nodes since the
scaffold, and the structural report has counted them for the observer letter,
but they were never reachable as something a person could sit down and act on.

This is the one place the daemon touches **prospective memory**: remembering to
do a thing you decided to do. It is a distinct system from the episodic and
semantic memory the rest of the pipeline serves, it degrades early in normal
ageing and in mild cognitive impairment, and nothing else here addresses it.
Ranking by how many notes are waiting on a target is the closest available proxy
for how much the user's own vault keeps reaching for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from my_daemon.stores.graph import GraphStore

_DANGLING_PREFIX = "dangling::"


@dataclass(frozen=True)
class DanglingTodo:
    target: str
    incoming_links: int
    #: Vault-relative paths of the notes that name this target. The list is what
    #: makes the entry actionable — "three notes were reaching for this, here
    #: they are" is a prompt to write; a bare noun is not.
    wanted_by: list[str] = field(default_factory=list)


def graph_todos(graph_store: GraphStore, *, limit: int = 20) -> list[DanglingTodo]:
    """Unwritten wikilink targets, most-wanted first."""

    g = graph_store.graph
    dangling = [
        n for n, d in g.nodes(data=True) if n.startswith(_DANGLING_PREFIX) and d.get("dangling")
    ]
    todos = [
        DanglingTodo(
            target=str(g.nodes[node].get("title") or node.removeprefix(_DANGLING_PREFIX)),
            incoming_links=g.in_degree(node),
            # `rel_path` rather than `title`: this is meant to be opened, and a
            # vault-relative path is what Obsidian and the rest of the reports
            # speak. Falls back to the title for nodes written before the
            # attribute existed.
            wanted_by=sorted(
                {
                    str(g.nodes[source].get("rel_path") or g.nodes[source].get("title") or source)
                    for source in g.predecessors(node)
                    if g.nodes[source].get("type") == "note" and not g.nodes[source].get("dangling")
                }
            ),
        )
        for node in dangling
    ]
    # Ties broken alphabetically so the list is stable between runs — a todo
    # list that reshuffles itself is one you stop trusting.
    todos.sort(key=lambda t: (-t.incoming_links, t.target))
    return todos[:limit]
