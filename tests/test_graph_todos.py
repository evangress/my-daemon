# SPDX-License-Identifier: Apache-2.0
"""Dangling wikilinks as prospective memory.

A `[[Name]]` with no note behind it is an intention the user formed and has not
executed — the notes they keep meaning to write. The graph has always kept these
as placeholder nodes and the structural report has always counted them, but they
never escaped into something a person could act on.

Worth being precise about why this is more than a graph statistic: prospective
memory (remembering to do a thing you decided to do) is among the first
capabilities to degrade in normal ageing and in mild cognitive impairment, and
nothing else in the daemon addresses it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.analysis.todos import DanglingTodo, graph_todos
from my_daemon.models import Note
from my_daemon.stores.graph import GraphStore

UUIDS = {name: f"{chr(97 + i) * 8}-0000-4000-8000-00000000000{i}" for i, name in enumerate("ABCD")}


def _note(name: str, *, links=None, unwritten=None) -> Note:
    return Note(
        path=Path("/vault") / f"{name}.md",
        relative_path=f"{name}.md",
        title=name,
        body=f"# {name}",
        tags=[],
        wikilinks=list(unwritten or []),
        dangling_wikilinks=list(unwritten or []),
        wikilink_uuids=[UUIDS[t] for t in (links or [])],
        mtime=datetime(2026, 7, 28, tzinfo=UTC),
        word_count=2,
        uuid=UUIDS[name],
    )


@pytest.fixture
def graph(tmp_path: Path) -> GraphStore:
    return GraphStore(path=tmp_path / "graph.gpickle")


def test_an_unwritten_target_is_surfaced(graph: GraphStore):
    graph.add_note(_note("A", unwritten=["Zettelkasten"]), chunk_ids=["a1"])

    todos = graph_todos(graph)

    assert [t.target for t in todos] == ["Zettelkasten"]


def test_a_written_note_is_not_a_todo(graph: GraphStore):
    """The whole point is the gap between intention and execution."""

    graph.add_note(_note("A", links=["B"]), chunk_ids=["a1"])
    graph.add_note(_note("B"), chunk_ids=["b1"])

    assert graph_todos(graph) == []


def test_todos_rank_by_how_many_notes_are_waiting_on_them(graph: GraphStore):
    """Three notes reaching for the same missing note is a louder intention
    than one."""

    graph.add_note(_note("A", unwritten=["Wanted", "Lonely"]), chunk_ids=["a1"])
    graph.add_note(_note("B", unwritten=["Wanted"]), chunk_ids=["b1"])
    graph.add_note(_note("C", unwritten=["Wanted"]), chunk_ids=["c1"])

    todos = graph_todos(graph)

    assert [t.target for t in todos] == ["Wanted", "Lonely"]
    assert [t.incoming_links for t in todos] == [3, 1]


def test_a_todo_names_the_notes_that_want_it(graph: GraphStore):
    """ "Which of my notes were reaching for this?" is the question that makes
    the todo actionable rather than a bare noun."""

    graph.add_note(_note("A", unwritten=["Wanted"]), chunk_ids=["a1"])
    graph.add_note(_note("B", unwritten=["Wanted"]), chunk_ids=["b1"])

    (todo,) = graph_todos(graph)

    assert sorted(todo.wanted_by) == ["A.md", "B.md"]


def test_the_limit_is_honoured(graph: GraphStore):
    graph.add_note(_note("A", unwritten=["One", "Two", "Three"]), chunk_ids=["a1"])

    assert len(graph_todos(graph, limit=2)) == 2


def test_an_empty_graph_has_no_todos(graph: GraphStore):
    assert graph_todos(graph) == []


def test_a_todo_is_a_dataclass_not_a_tuple(graph: GraphStore):
    graph.add_note(_note("A", unwritten=["Zettelkasten"]), chunk_ids=["a1"])

    assert isinstance(graph_todos(graph)[0], DanglingTodo)
