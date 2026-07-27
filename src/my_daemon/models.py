# SPDX-License-Identifier: Apache-2.0
"""Shared data models. Everything else imports from here."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Every daemon-authored theme tag carries this prefix. Two reasons: the user
# can see at a glance which tags they wrote and which the daemon proposed, and
# retrieval can refuse to walk them — otherwise theme tags create graph edges,
# which change expansion, which change fingerprints, which mint new themes, and
# the system converges on its own reflection.
THEME_TAG_PREFIX = "theme/"


def is_daemon_authored_tag(tag: str) -> bool:
    """Did the daemon write this tag, rather than the user?

    The single place that decision lives. Daemon-authored tags stay real —
    visible in Obsidian, present in the graph, counted in `stats()` — but they
    are excluded everywhere the daemon would read its own conclusion back as
    independent evidence: graph expansion, Louvain communities, the observer's
    top tags and warm edges, and the linker's tag propagation.
    """

    return tag.startswith(THEME_TAG_PREFIX)


class Note(BaseModel):
    """A raw markdown file from the vault."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    relative_path: str
    title: str
    body: str
    frontmatter: dict = Field(default_factory=dict)
    # Display targets: a vault-relative path when the link resolved, the raw
    # `[[text]]` when it didn't.
    wikilinks: list[str] = Field(default_factory=list)
    # Identities of the links that resolved to a real note, and the raw text of
    # the ones that didn't. The graph keys the first as `note::<uuid>` and the
    # second as `dangling::<text>` — a target with no note has no identity to
    # borrow, so overloading one namespace for both would be a lie.
    wikilink_uuids: list[str] = Field(default_factory=list)
    dangling_wikilinks: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mtime: datetime
    word_count: int
    # Stable identity, read from frontmatter. None means the note has not been
    # stamped yet — ingest assigns a path-derived fallback so it can still
    # participate, and records that the identity is not rename-stable.
    uuid: str | None = None
    uuid_source: str | None = None


class NoteRecord(BaseModel):
    """A row in the note registry — SQLite's view of a note's identity.

    Derived state. Frontmatter is the source of truth; when they disagree the
    frontmatter wins and this is corrected.
    """

    uuid: str
    rel_path: str
    title: str = ""
    mtime: datetime | None = None
    body_sha256: str | None = None
    frontmatter_sha256: str | None = None
    tags: list[str] = Field(default_factory=list)
    word_count: int = 0
    chunk_count: int = 0
    # assigned | adopted:<key> | derived:<key> | derived_path | restored
    uuid_source: str = "assigned"
    # False means the identity lives only here, so it is NOT rename-stable.
    in_frontmatter: bool = False
    # active | ignored | unwritable | orphan_graph_only | missing
    status: str = "active"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    deleted_at: datetime | None = None


class RegistryCoverage(BaseModel):
    """How much of the vault carries a rename-stable identity."""

    total: int = 0
    in_frontmatter: int = 0
    derived_path: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


class Chunk(BaseModel):
    """A retrievable piece of a Note."""

    id: str
    # The join key across all three stores. `note_path` beside it is the
    # *display* string — prompts, citations, GUI source lists and observer
    # letters all render it, and a raw UUID would make them unreadable.
    note_uuid: str = ""
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
    # Weighted (Dijkstra-over-1/weight) distance, so it is *fractional* the
    # moment any edge on the way has been reinforced by feedback. It was typed
    # `int` while the graph only ever had uniform weights; the first
    # `daemon select` turned every query near that note into a ValidationError.
    graph_distance: float | None = None
    seed_chunk_id: str | None = None
    combined_score: float = 0.0


def is_seed_distance(graph_distance: float | None) -> bool:
    """Is this distance the one a *seed* carries?

    Seeds are stamped ``graph_distance=0`` by :mod:`my_daemon.retrieval.seed`;
    ``None`` means no distance was ever recorded (a hand-built chunk, or a
    pre-expansion historical row). Everything else came out of graph expansion.

    The boundary is exact, not approximate: an expanded chunk's distance is a
    sum of ``1 / weight`` edge costs, and weights are capped at
    ``weights.DEFAULT_CEILING``, so the smallest distance expansion can ever
    produce is ``1 / ceiling`` — comfortably above zero. No epsilon needed.
    """

    return graph_distance is None or graph_distance == 0


class RetrievalResult(BaseModel):
    query: str
    seeds: list[RetrievedChunk] = Field(default_factory=list)
    expanded: list[RetrievedChunk] = Field(default_factory=list)
    ranked: list[RetrievedChunk] = Field(default_factory=list)
    # Set by the activation recorder, so whoever synthesizes an answer can
    # back-link its feedback row to this retrieval.
    query_uid: str | None = None


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
    selected_note_uuid: str | None = None


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
    members: list[str] = Field(default_factory=list)  # note rel-paths, capped
    top_tags: list[tuple[str, int]] = Field(default_factory=list)


class BridgingNote(BaseModel):
    """A note that connects otherwise-distant parts of the graph (high BC)."""

    note_path: str
    betweenness: float


class BridgeEdge(BaseModel):
    """A note↔note link whose removal would disconnect the component (``nx.bridges``)."""

    src: str
    dst: str
    kind: str  # wikilink / tag / mixed if more than one parallel edge
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
    events_skipped: int  # selections without enough info to replay (no seed path, etc.)
    top_edges: list[EdgeWeightDelta] = Field(default_factory=list)
    top_notes: list[NoteWeightDelta] = Field(default_factory=list)
