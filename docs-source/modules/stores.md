# `stores/` — Persistence layer

Source: `src/my_daemon/stores/`. Three persistence concerns, three files.

## `VectorStore` (Qdrant)

`stores/vector.py`. Wraps `qdrant_client.QdrantClient` for the `chunks`
collection.

### Schema

Hybrid mode (default) uses **named vector slots** on each point:

| Slot | Type | Purpose |
|---|---|---|
| `dense` | 384-dim float, cosine distance | Semantic recall |
| `sparse` | `SparseVectorParams()` | BM25-style exact-token recall |

Each point's id is `uuid5(NAMESPACE_URL, "my-daemon/chunk/" + chunk_id)` —
deterministic, so re-ingesting the same chunk lands on the same point.

Payload (display + filter fields):

```
chunk_id, note_path, heading_path, chunk_index, tags, wikilinks,
text  (truncated to 4000 chars; full text isn't stored here)
```

Dense-only mode keeps the old single-slot schema. The two are not
compatible; `ensure_collection()` detects a schema mismatch on startup, drops
the legacy collection, and prints a one-liner telling you to
`daemon ingest --full`.

### Public surface

```python
VectorStore(url, collection, dim, hybrid=True)
  .ensure_collection()
  .upsert(chunks, vectors, sparse_vectors=None)
  .search(vector, top_k=8)                          # dense-only
  .hybrid_search(dense_vec, (idx, val), top_k=8)    # server-side RRF
  .delete_by_note(note_path)
  .count()
```

`hybrid_search` issues a single `query_points` with two `Prefetch`es (one
dense, one sparse) and a `FusionQuery(fusion=Fusion.RRF)` — Qdrant does the
rank fusion server-side. This is why the daemon's hybrid query is one round
trip, not two.

`delete_by_note` uses a payload filter on `note_path`, which is also the
mechanism the retrieval expander uses to pull all chunks of a neighbor note.

## `GraphStore` (NetworkX)

`stores/graph.py`. Pickle-persisted `nx.MultiDiGraph`.

### Node shapes

| Node id | type | Attributes |
|---|---|---|
| `note::<relative_path>` | `note` | `title`, `mtime`, `chunk_ids`, optional `dangling=True` |
| `tag::<tag>` | `tag` | `title` |

Edges:

| From | To | `kind` | `weight` |
|---|---|---|---|
| note | note | `wikilink` | `1.0` (Phase 4 will mutate) |
| note | tag | `tag` | `1.0` |

A dangling wikilink (`[[Note That Doesn't Exist Yet]]`) becomes a
`note::...` node with `dangling=True`. Stats and the retrieval expander
filter these out, but they're kept in place so a later ingest can promote
them when the target note is created. This is the seed of the "notes you
mean to write" feature in the roadmap.

### Public surface

```python
GraphStore(path)
  .load()
  .save()
  .add_note(note, chunk_ids)          # first ingest of a note
  .update_note(note, chunk_ids)       # every subsequent ingest — see below
  .remove_note(rel_path)
  .chunk_ids_for(rel_path) -> list[str]
  .neighbors_within(rel_path, depth=2) -> dict[str, int]
  .stats() -> GraphStats     # note_count, tag_count, edge_count, top_pagerank, top_tags
```

`neighbors_within` runs a BFS over the **undirected** projection of the
multi-digraph so it traverses both wikilink directions and the note↔tag↔note
path. Returns `{neighbor_relative_path: distance}` for note nodes only.

### `update_note` — differential re-ingest

A note **owns its outgoing wikilink and tag edges, and nothing else.**
`update_note` refreshes the node's attributes, adds the edges the author
introduced, drops the ones they deleted, and leaves every surviving edge
completely untouched — so the `weight` and `last_reinforced_at` that
`retrieval.weights` accumulated on it carry across the edit.

!!! danger "Never re-ingest with `remove_note` + `add_note`"
    This is what the pipeline used to do, and it silently destroyed data two
    ways on **every note save**:

    1. `remove_node` drops all incident edges in *both* directions, so editing
       note `B` deleted every other note's wikilink *to* `B`. The edge did not
       come back until the linking note happened to be re-ingested.
    2. `add_note` recreates edges at `weight=1.0`, discarding everything the
       adaptive-weighting loop had learned about them.

    Both `ingest_vault` and `ingest_note` now call `update_note`. Regression
    cover lives in `tests/test_graph_update.py` and
    `tests/test_ingest_incremental.py`.

A link the author deletes and later restores correctly starts over at
`weight=1.0` — it genuinely left the set, so there is nothing to carry forward.

### Stats

`stats()` uses NetworkX `pagerank` (graceful fallback to `{}` if
`PowerIterationFailedConvergence`). Tag ranking is by degree in the current
v0.1.

## The state database and its migration ladder

`stores/db.py` owns the schema of `data/feedback.db` — the single SQLite file
shared by `FeedbackStore` and `AgentStateStore`. Every store goes through
`open_state_db()`; no store defines its own DDL any more.

```python
MIGRATIONS: list[tuple[int, str, Migration]] = [
    (1, "baseline_feedback_and_agent_state", _m001_baseline),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]
```

Versions are tracked in `PRAGMA user_version`. Each step runs in its own
`BEGIN IMMEDIATE` transaction that *also* bumps the version, so a crash
part-way up the ladder leaves the file at a consistent earlier version rather
than half-migrated.

**Adoption.** Databases created before this module existed sit at
`user_version == 0` but already contain every table, because the old code ran
`CREATE TABLE IF NOT EXISTS` at store construction. Migration 1 is therefore
*exactly* that old code — including the `PRAGMA table_info` introspection that
adds the `selected_*` columns — and then stamps version 1. A fresh database and
a months-old one converge on identical structure.

**Pragmas.** `open_state_db()` sets `journal_mode = WAL` (without it, a
`daemon consolidate` run holding a write lock blocks the chat UI outright),
`busy_timeout = 5000` (without it, a concurrent write raises
`database is locked` immediately), and `foreign_keys = ON` (off by default in
SQLite, which had silently made every `REFERENCES` clause inert).

**Read-only bundles.** Snapshot bundles are frozen artifacts and may sit on
read-only media, so `open_state_db(read_only=True)` never migrates. It checks
`min_version` and raises `SnapshotSchemaTooOld` if the bundle is too old,
letting callers degrade — skip the analysis that needs the newer tables, carry
on — rather than crash. `min_version` expresses *what the caller needs*, not the
latest version: `FeedbackStore` passes `0`, since the `feedback` table exists
even in unstamped databases.

!!! warning "Never use `executescript` inside a migration"
    `sqlite3.Cursor.executescript` issues an implicit `COMMIT` before it runs,
    which silently ends the transaction `migrate()` opened. Use the
    `exec_script()` helper in the same module, which splits on semicolons and
    executes statements individually. (It cannot handle trigger bodies — a
    migration needing those must issue its own `conn.execute` calls.)

See `daemon migrate db` / `daemon migrate status` in the [CLI reference](../cli.md).

## `FeedbackStore` (SQLite — query log)

`stores/feedback.py`. One table, defined by migration 1:

```sql
CREATE TABLE feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    query TEXT NOT NULL,
    retrieval_summary TEXT NOT NULL,   -- JSON: ranked chunk ids + scores, counts
    answer TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    signal TEXT,                       -- explicit_up / explicit_down / ...
    signal_captured_at TEXT
);
CREATE INDEX idx_feedback_timestamp ON feedback(timestamp);
```

Every `daemon query` and every chat send writes one row at submission. A
future `daemon select <id> <candidate#>` will attach a `candidate_selected`
signal via `attach_signal()`. The `recent()` helper is what
`daemon reflect` reads to find chat events for its context.

## `AgentStateStore` (SQLite — writeback state)

`stores/agent_state.py`. Three tables, all in the same `feedback.db` file
so the user only ever has *one* state file to back up or wipe:

```sql
CREATE TABLE agent_link_runs (
    note_path TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    note_mtime_seen REAL,
    applied_count INTEGER NOT NULL DEFAULT 0,
    suggested_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE agent_extract_runs (
    note_path TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    note_mtime_seen REAL NOT NULL,
    summary_hash TEXT NOT NULL
);
CREATE TABLE agent_reflect_runs (
    theme TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    source_notes_hash TEXT NOT NULL,
    source_chat_count INTEGER NOT NULL DEFAULT 0
);
```

Schemas are additive (`CREATE TABLE IF NOT EXISTS`), so the agent state
appears on top of an existing query-log DB without a migration step.
