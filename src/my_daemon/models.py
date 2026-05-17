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


# ---------------------------------------------------------------------------
# Structural analysis (M3) — pure-Python reports over a snapshot.
# ---------------------------------------------------------------------------


class CommunitySummary(BaseModel):
    """One Louvain community, ranked by size."""

    community_id: int
    size: int
    members: list[str] = Field(default_factory=list)         # note rel-paths, capped
    top_tags: list[tuple[str, int]] = Field(default_factory=list)


class BridgingNote(BaseModel):
    """A note that connects otherwise-distant parts of the graph (high BC)."""

    note_path: str
    betweenness: float


class BridgeEdge(BaseModel):
    """A note↔note link whose removal would disconnect the component (``nx.bridges``)."""

    src: str
    dst: str
    kind: str        # wikilink / tag / mixed if more than one parallel edge
    weight: float


class WarmEdge(BaseModel):
    """A reinforced edge — the graph's currently 'hot' connections."""

    src: str
    dst: str
    kind: str
    weight: float
    last_reinforced_at: str | None = None


class DanglingTarget(BaseModel):
    """A wikilink target that doesn't (yet) have its own note."""

    target: str
    incoming_links: int  # how many notes reference this target


class StructuralReport(BaseModel):
    """Snapshot-frozen view of what the graph has learned so far.

    Produced by :func:`my_daemon.analysis.structural.compute_report`. No LLM
    in the loop — every number here is reproducible from the snapshot.
    """

    snapshot_id: str | None = None
    generated_at: datetime
    note_count: int
    tag_count: int
    edge_count: int
    community_count: int
    communities: list[CommunitySummary] = Field(default_factory=list)
    bridging_notes: list[BridgingNote] = Field(default_factory=list)
    bridge_edges: list[BridgeEdge] = Field(default_factory=list)
    orphan_notes: list[str] = Field(default_factory=list)
    dangling_targets: list[DanglingTarget] = Field(default_factory=list)
    warm_edges: list[WarmEdge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Hypothetical weight evolution (M3 — what would happen if we replayed
# recent feedback against the snapshot, without touching live state).
# ---------------------------------------------------------------------------


class EdgeWeightDelta(BaseModel):
    """One edge's weight change after replaying feedback events on a copy."""

    src: str
    dst: str
    kind: str
    before: float
    after: float
    delta: float


class NoteWeightDelta(BaseModel):
    """Aggregated |delta| over all of a note's incident edges."""

    note_path: str
    total_delta: float
    edges_changed: int


class WeightEvolutionReport(BaseModel):
    """What would shift if we replayed N days of selections on the snapshot."""

    snapshot_id: str | None = None
    generated_at: datetime
    lookback_days: int
    events_replayed: int
    events_skipped: int      # selections without enough info to replay (no seed path, etc.)
    top_edges: list[EdgeWeightDelta] = Field(default_factory=list)
    top_notes: list[NoteWeightDelta] = Field(default_factory=list)
