# My Daemon — Project Scaffold

A personal AI memory system that turns an Obsidian vault into a graph-augmented retrieval system: vector search seeds, graph traversal expands, an LLM interprets, and a nightly consolidation phase looks at the whole structure from above.

This document is the spec for the **first draft**. It is intentionally a scaled-down version of the larger architecture — the goal is to ship a working v0.1 in a few focused sessions, then layer the harder pieces (adaptive weighting, dreaming, observer LLM) on top of something that already works end-to-end.

---

## 1. MVP Scope (v0.1)

**In:**
- Read a configured Obsidian vault directory (recursive `.md` discovery).
- Parse YAML frontmatter, wikilinks (`[[Note Name]]`), tags (`#tag`).
- Chunk markdown into retrievable units (preserve heading context).
- Embed chunks with a local sentence-transformer model.
- Store vectors + metadata in Qdrant (runs locally in a Docker container or embedded mode).
- Build an in-memory NetworkX graph from wikilinks + tags; persist as gpickle.
- Seed-and-expand retrieval: vector top-K → graph expansion → return ranked context.
- LLM answer synthesis via Claude API.
- A `daemon` CLI: `ingest`, `query`, `status`.
- A SQLite feedback log (query, retrieved chunks, latency, user signal placeholder). No adaptive weighting yet — just record.

**Deferred (later phases, not v0.1):**
- Adaptive edge weighting / learning-to-rank.
- Nightly consolidation job (snapshot + classical graph algos + observer LLM).
- Multi-embedding spaces (emotional, entity-based).
- Obsidian plugin / live file watching.
- Local LLM for synthesis.
- Cross-encoder re-ranking.

The single discipline that matters here: **resist building phase 2 inside phase 1.** Get the boring pipeline working end-to-end before adding intelligence on top.

---

## 2. Tech Stack (decisions already made — no need to debate)

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best ML/RAG ecosystem |
| Vector store | Qdrant (local) | Fast, easy local setup, rich payload filtering, clean Python SDK |
| Graph | NetworkX (in-memory, pickled) | A personal vault fits in RAM. Migrate to Neo4j later if needed. |
| Feedback log | SQLite | Single file, zero ops |
| Embeddings | `BAAI/bge-small-en-v1.5` (via sentence-transformers) | 384 dim, fast on CPU, strong quality |
| LLM | Anthropic Claude API (`claude-opus-4-7` or `claude-sonnet-4-6`) | Best synthesis; skip local LLM for v0.1 |
| Markdown parsing | `python-frontmatter` + custom regex for wikilinks/tags | Lightweight |
| Chunking | `langchain-text-splitters` MarkdownHeaderTextSplitter | Preserves heading context |
| CLI | `typer` + `rich` | Modern, ergonomic |
| Config | `pydantic-settings` + `config.yaml` + `.env` | Type-safe, layered |
| Package mgmt | `uv` | Fastest |
| Testing | `pytest` | Standard |

**Migration path notes for later:** NetworkX → Neo4j when graph operations get expensive. Qdrant stays. SQLite stays.

---

## 3. Project Structure

```
my-daemon/
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── config.example.yaml
├── docker-compose.yaml          # Qdrant service
├── src/
│   └── my_daemon/
│       ├── __init__.py
│       ├── cli.py               # Typer entry point
│       ├── config.py            # Pydantic Settings
│       ├── models.py            # Shared dataclasses (Chunk, Node, Edge, etc.)
│       │
│       ├── vault/
│       │   ├── __init__.py
│       │   ├── reader.py        # Walk vault dir, yield raw Note objects
│       │   ├── parser.py        # Parse frontmatter, wikilinks, tags
│       │   └── chunker.py       # Markdown → Chunk[]
│       │
│       ├── embeddings/
│       │   ├── __init__.py
│       │   └── embedder.py      # sentence-transformers wrapper, batched
│       │
│       ├── stores/
│       │   ├── __init__.py
│       │   ├── vector.py        # Qdrant client wrapper
│       │   ├── graph.py         # NetworkX wrapper + persistence
│       │   └── feedback.py      # SQLite feedback log
│       │
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── seed.py          # Vector top-K search
│       │   ├── expand.py        # Graph BFS expansion with depth/decay
│       │   └── orchestrator.py  # Combines seed + expand → ranked context
│       │
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py        # Anthropic client wrapper
│       │   └── prompts.py       # Synthesis prompt templates
│       │
│       └── pipeline/
│           ├── __init__.py
│           ├── ingest.py        # Full ingestion pipeline
│           └── query.py         # Full query pipeline
│
├── scripts/
│   ├── reset.py                 # Wipe local state (dev convenience)
│   └── inspect.py               # Dump graph stats, vector counts
│
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   └── sample_vault/        # Tiny test vault: 5–10 notes with wikilinks
│   ├── test_parser.py
│   ├── test_chunker.py
│   ├── test_graph.py
│   ├── test_retrieval.py
│   └── test_e2e.py
│
└── data/                        # gitignored
    ├── qdrant/                  # Qdrant storage
    ├── graph.gpickle            # NetworkX pickle
    └── feedback.db              # SQLite
```

---

## 4. Core Data Models

Define these in `src/my_daemon/models.py`. Everything else imports from here.

```python
from datetime import datetime
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

# --- Vault layer ---

class Note(BaseModel):
    """A raw markdown file from the vault."""
    path: Path                    # absolute path
    relative_path: str            # path relative to vault root, used as stable ID
    title: str                    # filename without .md, or H1 if present
    body: str                     # full markdown text after frontmatter
    frontmatter: dict             # parsed YAML frontmatter
    wikilinks: list[str]          # [[Target]] references, normalized to relative_paths where resolvable
    tags: list[str]               # both #inline and frontmatter tags
    mtime: datetime
    word_count: int

class Chunk(BaseModel):
    """A retrievable piece of a Note."""
    id: str                       # stable hash of (note.relative_path + heading_path + chunk_index)
    note_path: str                # relative_path of parent Note
    heading_path: list[str]       # e.g. ["Designing AI Memory", "RAG vs Fine-tuning"]
    text: str
    chunk_index: int              # position within note
    tags: list[str]               # inherited from parent note
    wikilinks: list[str]          # wikilinks found within this chunk specifically

# --- Graph layer ---

# NetworkX node attributes:
#   type: "note" | "tag"
#   title: str
#   chunk_ids: list[str]          # which chunks belong to this note (if note)
#   mtime: datetime (if note)
#
# NetworkX edge attributes:
#   kind: "wikilink" | "tag" | "co-occurrence"
#   weight: float (default 1.0; phase 2 will mutate this)

# --- Retrieval layer ---

class RetrievedChunk(BaseModel):
    chunk: Chunk
    vector_score: float | None    # cosine similarity, if surfaced by vector
    graph_distance: int | None    # hops from a seed, if surfaced by graph
    seed_chunk_id: str | None     # which seed chunk this was expanded from
    combined_score: float         # final ranking score

class RetrievalResult(BaseModel):
    query: str
    seeds: list[RetrievedChunk]   # the direct vector hits
    expanded: list[RetrievedChunk]  # graph neighbors
    ranked: list[RetrievedChunk]  # final ordered list passed to LLM

# --- Feedback layer ---

class FeedbackEvent(BaseModel):
    id: int | None = None
    timestamp: datetime
    query: str
    retrieval_result_summary: dict   # chunk_ids + scores, compact
    answer: str
    latency_ms: int
    signal: Literal["explicit_up", "explicit_down", "follow_up", "no_follow_up", "rephrase"] | None = None
    signal_captured_at: datetime | None = None
```

---

## 5. Module Responsibilities

### `vault/reader.py`
- Walk vault root (configurable, default `~/Documents/Obsidian/<vault>`).
- Skip hidden dirs (`.obsidian`, `.trash`).
- Yield `Note` objects lazily.
- Resolve wikilinks: for each `[[Foo]]`, find the corresponding file in the vault; if found, store `relative_path`; if not (dangling link), store the raw target.

### `vault/parser.py`
- `python-frontmatter` for YAML.
- Regex for wikilinks: `\[\[([^\]|]+)(?:\|[^\]]+)?\]\]` (handles `[[target|alias]]`).
- Regex for inline tags: `(?<![\w/])#([A-Za-z][\w/-]*)` (avoids `#` in URLs and code).
- Merge inline tags with frontmatter tags.

### `vault/chunker.py`
- Use `MarkdownHeaderTextSplitter` to split on H1/H2/H3.
- Secondary split for any chunk over a token threshold (start at 512 tokens, 50 overlap).
- Each chunk inherits parent note's tags. Wikilinks recomputed per-chunk so graph expansion can be chunk-aware later.

### `embeddings/embedder.py`
- Lazy-load model on first use.
- Batched encoding (batch size 32).
- Normalize to unit vectors (lets us use dot product = cosine).

### `stores/vector.py`
- Qdrant collection `chunks`, 384-dim, cosine distance.
- Payload: `note_path`, `heading_path`, `tags`, `chunk_index`, `text` (truncated).
- Methods: `upsert(chunks, vectors)`, `search(vector, top_k, filter)`, `delete_by_note(note_path)`, `count()`.

### `stores/graph.py`
- NetworkX `MultiDiGraph` (allow multiple edge types between same nodes).
- Nodes: one per Note, one per Tag.
- Edges:
  - Note → Note for each resolved wikilink (kind=`wikilink`).
  - Note → Tag for each tag (kind=`tag`).
- Persist to `data/graph.gpickle` via `nx.write_gpickle` / `nx.read_gpickle`.
- Methods: `add_note(note)`, `remove_note(path)`, `neighbors(node, depth)`, `save()`, `load()`, `stats()`.

### `stores/feedback.py`
- Single table for v0.1:
  ```sql
  CREATE TABLE feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    query TEXT NOT NULL,
    retrieval_summary TEXT NOT NULL,  -- JSON
    answer TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    signal TEXT,
    signal_captured_at TEXT
  );
  ```
- Insert on every query; update if a follow-up signal is captured.

### `retrieval/seed.py`
- Embed query → Qdrant search → top-K (default 8) chunks. Return as `RetrievedChunk` with `vector_score`.

### `retrieval/expand.py`
- For each seed chunk, look up the parent note in the graph.
- BFS to depth D (default 2). For each visited note, pull its chunks from Qdrant by `note_path` filter (or maintain a chunk-by-note index in memory).
- Apply graph-distance decay to combined score: `combined = vector_score * (decay ** distance)`, decay default 0.5.

### `retrieval/orchestrator.py`
- Run seed → expand → dedupe by chunk_id → sort by `combined_score` → trim to context budget (default ~6000 tokens of chunk text).
- Return `RetrievalResult`.

### `llm/client.py`
- Thin wrapper over `anthropic.Anthropic()`.
- One method: `synthesize(query: str, context_chunks: list[RetrievedChunk]) -> str`.
- Loads model from config.

### `llm/prompts.py`
- One synthesis prompt for v0.1. Should:
  - Frame the model as the user's "daemon" — a memory companion with access to their journal.
  - Instruct it to cite chunk sources by `note_path` + `heading_path`.
  - Be honest when context doesn't contain the answer.
  - Avoid making up details.

### `pipeline/ingest.py`
- `ingest(vault_path, full_rebuild=False)`:
  1. Walk vault.
  2. For each note, compare `mtime` against stored state (a small `manifest.json` keyed by `relative_path` → `mtime` + `chunk_ids`).
  3. New/changed notes: chunk, embed, upsert to Qdrant, update graph.
  4. Deleted notes: remove chunks from Qdrant, remove nodes from graph.
  5. Save graph + manifest.

### `pipeline/query.py`
- `query(text) -> {answer, retrieval_result, feedback_event_id}`.
- Logs to feedback DB.
- Returns `feedback_event_id` so the CLI can later attach a signal.

---

## 6. CLI Surface (v0.1)

Implement with `typer`. The binary is `daemon`.

```bash
# First-time setup
daemon init                          # Generates config.yaml, .env from examples; prompts for vault path
daemon ingest                        # Full ingestion (or incremental on subsequent runs)
daemon ingest --full                 # Force rebuild

# Daily use
daemon query "what was I working through about the Q3 roadmap"
daemon ask "..."                     # alias for query

# Inspection
daemon status                        # vault path, note count, chunk count, graph stats, last ingest time
daemon graph stats                   # PageRank top-N, degree distribution, tag frequency
daemon search "literal phrase"       # vector-only search, no LLM synthesis (debug)

# Maintenance
daemon reset                         # wipe all local state (data/*); confirm prompt
```

Output should use `rich` for readable formatting. Query output shows the answer followed by a compact source list (note path + heading path + first ~80 chars of the chunk).

---

## 7. Configuration

`config.example.yaml`:

```yaml
vault:
  path: ~/Documents/Obsidian/MyVault
  exclude_dirs: [".obsidian", ".trash", "templates"]

chunking:
  max_tokens: 512
  overlap_tokens: 50
  split_on_headers: [h1, h2, h3]

embeddings:
  model: BAAI/bge-small-en-v1.5
  batch_size: 32
  device: auto  # auto | cpu | cuda | mps

vector_store:
  backend: qdrant
  qdrant:
    url: http://localhost:6333
    collection: chunks

graph:
  path: ./data/graph.gpickle
  expansion_depth: 2
  distance_decay: 0.5

retrieval:
  seed_top_k: 8
  context_token_budget: 6000

llm:
  provider: anthropic
  model: claude-opus-4-7
  max_tokens: 2048
  temperature: 0.3

feedback:
  db_path: ./data/feedback.db

logging:
  level: INFO
```

`.env.example`:
```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## 8. Implementation Phases

Each phase ends with something usable. Don't start the next until the current one works end-to-end against your real vault.

### Phase 0: Skeleton (1 session)
- Repo, `pyproject.toml`, `uv` setup, ruff, pytest.
- `config.py`, `models.py`, empty module files with docstrings.
- `daemon --help` runs and lists commands (all stubs).
- `docker-compose.yaml` for Qdrant.
- Sample test vault under `tests/fixtures/sample_vault/`.

### Phase 1: Ingest pipeline (1–2 sessions)
- `vault/reader.py`, `vault/parser.py`, `vault/chunker.py` with tests.
- `embeddings/embedder.py`.
- `stores/vector.py`, `stores/graph.py` with persistence.
- `pipeline/ingest.py` + `daemon ingest` command.
- `daemon status` shows real counts.

### Phase 2: Retrieval + Query (1 session)
- `retrieval/seed.py`, `retrieval/expand.py`, `retrieval/orchestrator.py`.
- `llm/client.py`, `llm/prompts.py`.
- `pipeline/query.py` + `daemon query` command.
- `stores/feedback.py` logs every query.

### Phase 3: Incremental ingest + polish (1 session)
- Manifest-based incremental ingestion (only re-embed changed notes).
- `daemon graph stats` with PageRank + community detection (Louvain via `python-louvain`).
- README with quickstart.
- End-to-end test: ingest fixture vault, run a query, assert citation structure.

### Phase 4+ (later — design doc territory)
- Adaptive edge weighting from feedback signals.
- Snapshot-based nightly consolidation job.
- Observer LLM that interprets graph structure.
- Multi-embedding spaces.
- Obsidian plugin / file watcher.

---

## 9. First-Run Acceptance Criteria

v0.1 is done when:

1. [X] `docker-compose up -d` starts Qdrant.
2. [X] `uv sync && daemon init && daemon ingest` succeeds against my real Obsidian vault.
3. [X] `daemon status` reports note count matching `find vault -name '*.md' | wc -l`.
4. [X] `daemon query "..."` returns an answer with cited sources, in under 5 seconds for vaults under ~2k notes.
5. [X] Re-running `daemon ingest` after editing 3 notes only re-embeds those 3 notes.
6. [X] `tests/test_e2e.py` ingests the fixture vault and asserts a query returns expected citations.

---

## 10. Notes for the Coding Agent

- Start with `models.py` and the test fixture vault. Everything downstream is easier when the data shapes are nailed down first.
- Use `pydantic.BaseModel` for all data classes — it gives free validation and `.model_dump_json()` for the feedback log.
- Don't optimize prematurely. The whole pipeline can be slow and synchronous in v0.1.
- Wikilink resolution: build a `{lowercased_title → relative_path}` index once, look up against that. Obsidian wikilinks are case-insensitive on the title side.
- Chunk IDs must be stable across runs (same content → same ID), otherwise incremental ingest will thrash. Hash on `(relative_path, heading_path_tuple, chunk_index, content_hash)`.
- Keep `prompts.py` boring and explicit for now. Resist the urge to engineer a clever persona prompt — get retrieval quality right first, prompt cleverness later.
- Add a single `--verbose` flag on every CLI command that prints the retrieval trace (seeds chosen, graph nodes visited, final ranked list with scores). You will need this constantly while tuning.
- Write the README last, from the perspective of "I am future-me who has forgotten everything." Setup steps, daily commands, troubleshooting.

---

## 11. Reference: Where Each Piece of the Design Doc Lives

| Design doc section | v0.1 location | Phase |
|---|---|---|
| §2 RAG vs fine-tuning | Foundational choice — RAG is the whole system | — |
| §3 Weighting / hybrid search | `retrieval/orchestrator.py` (basic), full version P4 | P2 / P4 |
| §3 Obsidian as graph | `vault/parser.py` + `stores/graph.py` | P1 |
| §4 Seed-and-expand | `retrieval/seed.py` + `retrieval/expand.py` | P2 |
| §4 Adaptive weighting | Deferred — feedback is logged, not yet applied | P4 |
| §4 Exploration budget | Deferred | P4 |
| §5 Classical graph algorithms | `daemon graph stats` (partial) | P3 |
| §5 LLM interpreting structure | Deferred (observer LLM) | P4 |
| §6 Snapshot-isolated dreaming | Deferred | P4 |
| §6 Counterfactual simulation | Deferred | P5 |
| §7 Recommended stack | Adopted with simplifications (NetworkX in place of Neo4j) | — |
