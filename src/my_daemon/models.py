"""Shared data models. Everything else imports from here."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Note(BaseModel):
    """A raw markdown file from the vault."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    relative_path: str
    title: str
    body: str
    frontmatter: dict = Field(default_factory=dict)
    wikilinks: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mtime: datetime
    word_count: int


class Chunk(BaseModel):
    """A retrievable piece of a Note."""

    id: str
    note_path: str
    heading_path: list[str] = Field(default_factory=list)
    text: str
    chunk_index: int
    tags: list[str] = Field(default_factory=list)
    wikilinks: list[str] = Field(default_factory=list)


class RetrievedChunk(BaseModel):
    """A chunk surfaced during retrieval, with provenance and score."""

    chunk: Chunk
    vector_score: float | None = None
    graph_distance: int | None = None
    seed_chunk_id: str | None = None
    combined_score: float = 0.0


class RetrievalResult(BaseModel):
    query: str
    seeds: list[RetrievedChunk] = Field(default_factory=list)
    expanded: list[RetrievedChunk] = Field(default_factory=list)
    ranked: list[RetrievedChunk] = Field(default_factory=list)


FeedbackSignal = Literal[
    "explicit_up",
    "explicit_down",
    "follow_up",
    "no_follow_up",
    "rephrase",
    "candidate_selected",
]


class FeedbackEvent(BaseModel):
    id: int | None = None
    timestamp: datetime
    query: str
    retrieval_summary: dict
    answer: str
    latency_ms: int
    signal: FeedbackSignal | None = None
    signal_captured_at: datetime | None = None
    # Set when signal == "candidate_selected" — the picked candidate's rank
    # (1-based, matching how candidates are displayed) and chunk id. These
    # let the weights module reconstruct which path through the graph
    # the user endorsed.
    selected_rank: int | None = None
    selected_chunk_id: str | None = None
    selected_note_path: str | None = None


class GraphStats(BaseModel):
    note_count: int
    tag_count: int
    edge_count: int
    top_pagerank: list[tuple[str, float]] = Field(default_factory=list)
    top_tags: list[tuple[str, int]] = Field(default_factory=list)
