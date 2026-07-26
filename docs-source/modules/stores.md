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
chunk_id, note_uuid, note_path, heading_path, chunk_index, tags, wikilinks,
text  (truncated to 4000 chars; full text isn't stored here)
```

`note_uuid` is the join key; `note_path` beside it is the display string.
Both are covered by keyword payload indexes (`_ensure_payload_indexes`) —
graph expansion filters on `note_uuid` for *every seed of every query*, so an
unindexed field there is pure latency.

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
  .delete_by_note_uuid(note_uuid)
  .set_note_path(note_uuid, rel_path)   # rename: payload-only, no re-embed
  .count()
```

`hybrid_search` issues a single `query_points` with two `Prefetch`es (one
dense, one sparse) and a `FusionQuery(fusion=Fusion.RRF)` — Qdrant does the
rank fusion server-side. This is why the daemon's hybrid query is one round
trip, not two.

`delete_by_note_uuid` uses a payload filter on `note_uuid`, which is also the
mechanism the retrieval expander uses to pull all chunks of a neighbor note.

`set_note_path` is what makes a rename cheap. Chunk ids derive from the note's
UUID, so moving a file changes nothing semantic and leaves every point id
correct — only the display path is stale, and one `set_payload` call fixes it.

## `GraphStore` (NetworkX)

`stores/graph.py`. Pickle-persisted `nx.MultiDiGraph`.

### Node shapes

| Node id | type | Attributes |
|---|---|---|
| `note::<uuid>` | `note` | `title`, `rel_path`, `mtime`, `chunk_ids` |
| `dangling::<raw target>` | `note` | `title`, `dangling=True` |
| `tag::<tag>` | `tag` | `title` |

Edges:

| From | To | `kind` | `weight` |
|---|---|---|---|
| note | note | `wikilink` | `1.0` (Phase 4 will mutate) |
| note | tag | `tag` | `1.0` |

A dangling wikilink (`[[Note That Doesn't Exist Yet]]`) becomes a
`dangling::...` node. Stats and the retrieval expander filter these out, but
they're kept in place as the seed of the "notes you mean to write" feature in
the roadmap. See *Nodes are keyed by identity* below for why they get their own
namespace.

### Public surface

```python
GraphStore(path)
  .load()
  .save()
  .add_note(note, chunk_ids)          # first ingest of a note
  .update_note(note, chunk_ids)       # every subsequent ingest — see below
  .remove_note(note_uuid)
  .chunk_ids_for(note_uuid) -> list[str]
  .neighbors_within(note_uuid, depth=2) -> dict[str, float]
  .stats() -> GraphStats     # note_count, tag_count, edge_count, top_pagerank, top_tags
```

`neighbors_within` runs a BFS over the **undirected** projection of the
multi-digraph so it traverses both wikilink directions and the note↔tag↔note
path. Returns `{neighbor_uuid: distance}` for note nodes only.

### Nodes are keyed by identity

Note nodes are `note::<uuid>`, not `note::<rel_path>`. A rename or a folder move
therefore keeps the node — and every learned edge weight on it — exactly where
it was. Each node carries `rel_path` so reports can render prose without a
registry round-trip; **never put a raw uuid in a `StructuralReport`**, since
every string in one is read by an LLM and by a person.

Unresolved wikilink targets live in a **separate `dangling::<text>` namespace**.
A target with no note behind it has no identity to key on, so overloading
`note::` for both would let a "note you keep meaning to write" collide with a
real note. `Note.wikilink_uuids` holds the links that resolved;
`Note.dangling_wikilinks` holds the ones that didn't. Only `VaultReader` has the
title index needed to tell them apart, so `parse_note` treats every link as
dangling and the reader promotes what it can resolve.

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
    (2, "note_registry_and_ordinals",        _m002_registry),
    (3, "feedback_note_identity",            _m003_feedback_identity),
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

## `NoteRegistry` (SQLite — the identity spine)

`stores/registry.py`, tables from migration 2. Answers three questions the rest
of the system keeps asking: what is this uuid's current path, what lives at this
path, and how much of the vault is still on a fragile identity.

```sql
CREATE TABLE notes (
    uuid TEXT PRIMARY KEY,       -- canonical lowercase hyphenated
    rel_path TEXT NOT NULL,
    title TEXT, mtime TEXT, body_sha256 TEXT, frontmatter_sha256 TEXT,
    tags_json TEXT, word_count INTEGER, chunk_count INTEGER,
    uuid_source TEXT,            -- assigned | adopted:<key> | derived:<key>
                                 -- | derived_path | restored
    in_frontmatter INTEGER,      -- 0 => identity is NOT rename-stable
    status TEXT,                 -- active | ignored | unwritable
                                 -- | orphan_graph_only | missing
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, deleted_at TEXT
);
CREATE UNIQUE INDEX idx_notes_rel_path_live
    ON notes(rel_path) WHERE deleted_at IS NULL;

CREATE TABLE note_ordinals (            -- stable small ints for sparse vectors
    note_uuid TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE,
    allocated_at TEXT NOT NULL
);
```

> **The registry is derived state. Frontmatter is the source of truth.** When
> the two disagree the frontmatter wins and the registry is corrected. That
> invariant is what makes vault sync across machines work, makes `daemon reset`
> safe, and makes a hand-edited UUID recoverable rather than corrupting.

Three details that carry weight:

- **The partial unique index** enforces one live note per path, catching a
  duplicate at *write* time rather than surfacing it as a confusing read later.
- **Deletion is soft.** The activation ledger is history and must outlive the
  note it refers to, so `soft_delete` tombstones and `live()` filters. Only the
  migration rollback hard-deletes, via `forget()`.
- **Ordinals are never reused.** A deleted note's ordinal stays permanently
  bound to it, so historical query fingerprints referring to it keep their
  meaning. Allocation runs under `BEGIN IMMEDIATE` with `UNIQUE(ordinal)` as
  the race guard.

`coverage()` powers the identity section of `daemon status` — notably the count
of `derived_path` notes, whose identity does not survive a rename.

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
