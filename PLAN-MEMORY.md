# PLAN-MEMORY.md — UUID Identity, the Note Registry, and the Activation Ledger

> **One line:** Give every note a name that survives being moved, then remember
> *which notes light up* for every question — so the daemon accumulates a model
> of what you keep circling back to, not just what you wrote.

This is a design + milestone plan, not yet code. It is grounded in the modules
that already exist here (`RetrievalOrchestrator`, `FeedbackStore`,
`GraphStore`, `vault/writer.py`, `pipeline.agent_observe.run_observe`) and in
two data-loss bugs found while reading them (§0.3), both of which change the
staging order.

Companion plans: [PLAN-HERMES.md](PLAN-HERMES.md) (the memory *interface*),
[librarian/LIBRARIAN-PLAN.md](librarian/LIBRARIAN-PLAN.md) (the vault
*gardener*, which is a direct consumer of §1's identity work).

---

## 0. Why

### 0.1 The problem with paths

My Daemon identifies every note by its **vault-relative path**. That string is
load-bearing in four places:

| Site | Form |
|---|---|
| Graph node ids | `note::<rel_path>` — `stores/graph.py:19` |
| Ingest manifest keys | `manifest[note.relative_path]` — `pipeline/ingest.py` |
| Qdrant payload filter | `note_path` — `stores/vector.py`, `retrieval/expand.py` |
| SQLite primary keys | `note_path TEXT PRIMARY KEY` — `stores/agent_state.py` |

It works today. It stops working in exactly the direction this project is
heading: the Librarian's whole job is to **classify and move notes between
folders**, and every move currently amnesias that note's identity in all four
places at once — including the edge weights M1 spent months learning.

A UUID in frontmatter isn't just a join key for the memory work below. It is a
prerequisite for the Librarian not erasing the graph every time it tidies up.

### 0.2 What we're building on top of it

1. **Three aspects, one key.** SQLite owns identity (registry, hashes, status),
   Qdrant owns semantics (chunk embeddings), networkx owns relations (wikilinks,
   tags, learned weights). All three keyed by `note_uuid`.
2. **An activation ledger.** Every query records which notes fired, via which
   source, at what strength.
3. **Fingerprints.** A query's activation pattern is a sparse vector over
   note-UUID space. Two questions worded completely differently that light up
   the same notes are *the same underlying concern* — and cosine similarity
   over fingerprints finds that, where embedding the query text would not.
4. **Emergent themes.** Cluster fingerprints during the nightly dream phase,
   have an LLM name each cluster, propose those names as tags.
5. **Recall.** Past queries and their notes surface again — into the LLM's
   context, and to you.

Two things make this much cheaper than it looks:

- **`feedback.retrieval_summary` is already a proto-activation record.**
  `build_retrieval_summary()` (`pipeline/query.py:26`) already emits
  `{chunk_id, note_path, score, vector_score, graph_distance, seed_chunk_id}`
  per candidate, and there is real history in the table. §3.5 backfills it.
- **`RetrievalOrchestrator.retrieve()` is a genuine 100%-coverage chokepoint.**
  CLI, GUI, `QueryEngine`, `DaemonCore`, and all five Hermes hooks pass through
  it. Only the debug `daemon search` bypasses it, and it should keep bypassing.

### 0.3 Two bugs found while planning — both verified in the code

**A. Re-ingesting a note destroys inbound edges and resets learned weights.**

`pipeline/ingest.py:101-103`:

```python
if note.relative_path in manifest:
    vector_store.delete_by_note(note.relative_path)
    graph_store.remove_note(note.relative_path)
...
graph_store.add_note(note, chunk_ids=[c.id for c in chunks])
```

`GraphStore.remove_note` → `nx.MultiDiGraph.remove_node`, which drops **all**
incident edges in both directions. `add_note` (`graph.py:50-74`) only recreates
that note's *outgoing* wikilink/tag edges, hardcoded to `weight=1.0`. So:

- Editing note `A` permanently deletes every `C → A` wikilink edge in the graph.
  It does not come back until `C` itself is re-ingested or `--full` runs.
- Every edge weight learned by M1 (`retrieval/weights.py`) on any edge touching
  `A` resets to `1.0`.

**The adaptive memory loop shipped in M1 has been leaking its learning on every
save.** The fix — a differential `GraphStore.update_note()` — needs no UUID at
all and ships as its own early milestone (M-mem-1b). It has to land *before* the
graph relabel, or "preserve learned weights across the relabel" is protecting an
empty set.

**B. `_atomic_write_text` forces `newline="\n"`** (`vault/writer.py:103`). On a
CRLF vault — anything synced from Windows — a whole-vault write would rewrite
every line of every file. Must be parameterized before §1 touches the vault.

### 0.4 Decisions locked in (from the planning Q&A, 2026-07-26)

| Question | Decision |
|---|---|
| Where does the UUID live? | **Frontmatter `uuid:`**, one-shot backfill migration |
| When are themes computed? | **Offline, inside `daemon consolidate`** — the existing snapshot-isolated dream phase |
| How does recall surface? | **Both** — injected into LLM context *and* shown to the user; independently toggleable |
| Do theme tags get written to notes? | **Proposed**; frontmatter written only on confirmation |

### 0.5 Verified API assumptions

- `QdrantClient.set_payload(collection, payload, points=Filter(...))` accepts a
  `Filter` → **`note_uuid` lands on every existing point with no re-embedding
  and no point-id churn.** `create_payload_index` is available.
- `scipy` 1.17.1 / `scikit-learn` 1.8.0 are already installed (transitively via
  `sentence-transformers`); `sklearn.cluster.HDBSCAN` exists. Both are
  **BSD-3-Clause → Apache-2.0 compatible**. They must be declared explicitly in
  `pyproject.toml` rather than riding a transitive pin.

---

## 1. Identity

```
                    ┌─────────────────────────────────┐
                    │  Obsidian note                  │
                    │  ---                            │
                    │  uuid: 9f3c1a7e-4b21-…          │  ← source of truth
                    │  ---                            │
                    └───────────────┬─────────────────┘
                                    │ note_uuid
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────┐        ┌────────────────────┐      ┌────────────────────┐
│ SQLite        │        │ Qdrant             │      │ networkx           │
│ IDENTITY      │        │ SEMANTICS          │      │ RELATIONS          │
│ registry,     │        │ chunk embeddings,  │      │ wikilinks, tags,   │
│ hashes, tags, │        │ payload.note_uuid  │      │ learned weights    │
│ status        │        │                    │      │ note::<uuid>       │
└───────┬───────┘        └────────────────────┘      └────────────────────┘
        │
        │  ┌──────────────────────────────────────────────────────┐
        └─▶│ ACTIVATION LEDGER  (queries × query_activations)      │
           │ "which notes fired, via which source, how strongly"   │
           └──────────────┬───────────────────────────────────────┘
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
      fingerprint similarity    HDBSCAN clustering
      "you've been here         → themes → tag proposals
       before"                    (in `daemon consolidate`)
```

### 1.1 The invariant

> **Frontmatter is the source of truth. The registry is derived state.**

If the two disagree, frontmatter wins and the registry is corrected. This is
what makes vault sync across machines work, what makes `daemon reset` safe, and
what makes a hand-edited UUID a recoverable situation rather than a corruption.
Every reconciliation rule in §6.4 follows from it.

### 1.2 The migration command

```
daemon migrate assign-uuids                    # dry-run by default, prints a table
daemon migrate assign-uuids --apply [--path-glob 'Projects/**'] [--limit 200]
daemon migrate list-runs
daemon migrate rollback-uuids <run_id> [--mode key-removal|restore] [--force]
```

Per-note decision order: **writability** (reuse `is_writable`, adding
`allow_agent_folder=True` — the `Agent/` exclusion exists to stop the daemon
rewriting its own generated prose, but stamping our own id into our own files is
exactly the daemon's business; `daemon: ignore` and path-escape gates unchanged)
→ **adopt an existing id key** → **mint `uuid4`** → **resolve collisions**.

```yaml
uuid:
  frontmatter_key: uuid
  adopt_keys: [uuid, uid, id, guid, note-id, permanent_id]
```

A value that parses as a UUID is adopted verbatim and canonicalized — and we
never rewrite someone else's `id:` key. A non-UUID opaque string derives
`uuid5(NS, f"my-daemon/adopted/{key}/{value}")`, which is deterministic, so a
re-run on a machine with no registry converges to the same answer. Collisions
(the Obsidian "Make a copy" / sync-conflict path, which *will* happen) resolve
in favor of the file whose `rel_path` matches the registry; the other is
re-minted as `reassigned_duplicate` and surfaced prominently rather than
silently.

### 1.3 Writing the key — do NOT reuse the `add_tags` round-trip

`vault/writer.py:add_tags` does a full `frontmatter.loads` → `frontmatter.dumps`
round-trip. On one note that's fine. Across a whole vault it is not: PyYAML
reorders keys, strips YAML comments, requotes strings, expands flow-style
`tags: [a, b]` into block style, and re-renders dates. **A whole-vault reformat
is a catastrophic first impression for a migration whose only job is to add one
line.**

Add a *textual* single-key setter instead:

```python
def set_frontmatter_key_textual(
    note_path: Path, key: str, value: str, *, vault_root: Path,
    allow_agent_folder: bool = False, grace_minutes: int = 2,
) -> WriteResult:
    """Insert or replace a single scalar `key: value` line in the frontmatter.

    Everything outside that one line is preserved byte-for-byte — comments,
    key order, quoting style, line endings.
    """
```

Detect the opening `---` on the **first** line only (a `---` later in the body
is a horizontal rule, not frontmatter). Preserve the file's dominant line ending
(bug B). Insert at the end of the existing block — least surprising diff. Reuse
`_atomic_write_text` so the vault-safety contract holds.

### 1.4 Backup and rollback

Per-file `snapshot()` is the wrong shape here — thousands of loose files with no
run grouping. Use a batch directory:

```
<vault>/Agent/backups/migrations/<run_id>/
  ├── manifest.jsonl     # {rel_path, uuid, action, sha256_before, sha256_after, uuid_source, reason}
  └── files/<rel_path>.md
```

- `--mode key-removal` (**default**) removes the `uuid:` line textually. Safe
  even on files edited since the migration, because it never restores a body.
- `--mode restore` copies back byte-for-byte, and **refuses** any file whose
  current sha256 ≠ `sha256_after` unless `--force` — restoring would otherwise
  destroy edits made after the migration.

### 1.5 The fallback identity

Some notes legitimately can't get a UUID: `daemon: ignore`, read-only
filesystem, unparseable frontmatter. They still need to participate in
retrieval, the graph, and the ledger.

**Fallback:** `uuid5(NS, f"my-daemon/note-path/{rel_path}")`, recorded as
`uuid_source='derived_path'`, `in_frontmatter=0`.

Stated honestly: deterministic across machines, but **not rename-stable** —
renaming such a note produces a new identity and loses its history, exactly like
today. That's the price of not writing to the file, and it's recorded per-row so
`daemon status` can report the count and the degradation stays visible. There is
no third identity mechanism; everything downstream sees a UUID string, and only
the registry knows whether it's real or derived.

---

## 2. The dual-key transition

| Site | Change | Cost | Reversible |
|---|---|---|---|
| Qdrant payload | add `note_uuid` | **payload-only, no re-embed** | yes |
| Ingest manifest | re-key by uuid, keep `path` in the value | rewrite one JSON file | yes |
| Graph node ids | `note::<path>` → `note::<uuid>` | `nx.relabel_nodes` | **one-way in practice** |
| SQLite path columns | add `note_uuid` alongside | `ALTER TABLE ADD COLUMN` + backfill | yes |

**Chunk ids stay path-derived and opaque.** Re-deriving them from uuid would
invalidate every Qdrant point id → a full re-embed of the vault. The only gain
is rename-stability, but the chunk id already includes `text`, so *any* content
edit changes it regardless. Renames are handled better in §2.2 anyway. This is
the single biggest cost avoided in the whole plan: **no milestone requires a
full re-ingest.**

### 2.1 The graph relabel preserves learned weights for free

`nx.relabel_nodes(copy=True)` carries all node and edge data dicts verbatim,
including `weight` and `last_reinforced_at`. That's the whole reason the rewrite
is safe. Guards required:

- **Pre-flight assert `len(set(uuids)) == len(uuids)`** — duplicates would
  silently *merge* nodes.
- **Dangling placeholders get their own namespace.** Today `note::<target>` is
  overloaded for both real notes and unresolved wikilink targets
  (`graph.py:64-67`). A placeholder has no uuid, so move them to
  `dangling::<raw target>`. This also simplifies `_dangling_targets` in
  `analysis/structural.py`.
- **Graph nodes with no registry entry** (note deleted since last ingest) get
  `uuid5(path)` and a `status='orphan_graph_only'` row. Never leave a
  mixed-namespace graph.
- **Stamp `graph.graph["schema_version"] = 2`** so `load()` can refuse a v1
  pickle with a message pointing at the migration. The pickle is unversioned
  today; this is the missing guardrail that makes the one-way door survivable.
- **`shutil.copy2` a `.gpickle.v1.bak`** first.

**Do not put raw UUIDs in `StructuralReport`.** `CommunitySummary.members`,
`BridgingNote.note_path`, and `WarmEdge.src` are rendered into prose that an LLM
and a human read. Resolve uuid → `rel_path` at report-build time; add parallel
`*_uuid` fields only where a consumer needs to join.

### 2.2 What the uuid buys at ingest: rename without re-embed

Once the manifest is uuid-keyed, ingest classifies each note:

| Case | Action |
|---|---|
| **unchanged** (uuid known, path same, mtime same) | skip, as today |
| **renamed only** (uuid known, path differs, `body_sha256` same) | `set_payload({"note_path": new}, Filter(note_uuid=…))` + registry update. **No embedding, no chunking, no graph churn, no weight loss.** |
| **content changed** | delete by uuid, re-chunk, re-upsert, `graph.update_note()` |
| **gone** | soft-delete the registry row, delete vectors, `graph.remove_note(uuid)` |

Renames are currently a full delete-and-re-embed *plus* silent weight
destruction. This is the most visible single win in the plan.

---

## 3. Schema

All of this lives in the **existing `data/feedback.db`**. A second file would
force `BUNDLE_VERSION` to 2, which makes `open_readonly` reject every snapshot
bundle you already have (`snapshot.py:287`); and the consolidation phase joins
`query_activations × notes × feedback` on a *frozen* snapshot, which is awkward
across `ATTACH` on a read-only connection. Growth is ~55 bytes/activation ≈
33 MB/year at 30 queries/day. The file is really a *state* db now — update the
docstrings, don't rename the path.

### 3.1 Note registry

```sql
CREATE TABLE IF NOT EXISTS notes (
    uuid                TEXT PRIMARY KEY,           -- canonical lowercase hyphenated
    rel_path            TEXT NOT NULL,
    title               TEXT NOT NULL DEFAULT '',
    mtime               TEXT,
    body_sha256         TEXT,                       -- post-frontmatter body only
    frontmatter_sha256  TEXT,
    tags_json           TEXT NOT NULL DEFAULT '[]',
    word_count          INTEGER NOT NULL DEFAULT 0,
    chunk_count         INTEGER NOT NULL DEFAULT 0,
    uuid_source         TEXT NOT NULL DEFAULT 'assigned',
        -- assigned | adopted:<key> | derived_path | reassigned_duplicate | restored
    in_frontmatter      INTEGER NOT NULL DEFAULT 0, -- 0 => identity is NOT rename-stable
    status              TEXT NOT NULL DEFAULT 'active',
        -- active | ignored | unwritable | orphan_graph_only | missing
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    deleted_at          TEXT                        -- soft delete; ledger outlives notes
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_rel_path_live
    ON notes(rel_path) WHERE deleted_at IS NULL;    -- one live note per path
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);

-- Stable small-int handles for sparse-vector indices. NEVER reused, so a
-- deleted note's ordinal stays permanently bound to it and historical
-- fingerprints keep their meaning.
CREATE TABLE IF NOT EXISTS note_ordinals (
    note_uuid TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE, allocated_at TEXT NOT NULL
);
```

### 3.2 The activation ledger

```sql
CREATE TABLE IF NOT EXISTS queries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query_uid        TEXT NOT NULL UNIQUE,          -- uuid4; the handle front-ends carry
    ts               TEXT NOT NULL,
    text             TEXT NOT NULL,
    text_sha256      TEXT NOT NULL,
    surface          TEXT NOT NULL,
        -- cli | gui | hermes_recall | hermes_prefetch | mcp | librarian | internal
    session_id       TEXT,
    origin           TEXT NOT NULL DEFAULT 'live',  -- live | backfill
    feedback_id      INTEGER REFERENCES feedback(id) ON DELETE SET NULL,
    seed_count       INTEGER NOT NULL DEFAULT 0,
    expanded_count   INTEGER NOT NULL DEFAULT 0,
    activation_count INTEGER NOT NULL DEFAULT 0,
    l2_norm          REAL    NOT NULL DEFAULT 0.0,  -- ‖fingerprint‖₂, precomputed
    fingerprint_json TEXT,                          -- set only on compaction (§6.1)
    theme_id         INTEGER REFERENCES themes(id) ON DELETE SET NULL,
    theme_score      REAL,
    latency_ms       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_queries_ts    ON queries(ts);
CREATE INDEX IF NOT EXISTS idx_queries_sha   ON queries(text_sha256);
CREATE INDEX IF NOT EXISTS idx_queries_theme ON queries(theme_id);

CREATE TABLE IF NOT EXISTS query_activations (
    query_id       INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    note_uuid      TEXT    NOT NULL,
    source         TEXT    NOT NULL,
        -- vector_seed | graph_expansion | keyword | recall_injected | selected
    strength       REAL    NOT NULL,                -- normalized contribution, 0..1
    raw_score      REAL,
    rank           INTEGER,
    chunk_id       TEXT,
    graph_distance REAL,
    seed_note_uuid TEXT,
    PRIMARY KEY (query_id, note_uuid, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_qa_note_query ON query_activations(note_uuid, query_id);

CREATE TABLE IF NOT EXISTS note_activation_stats (  -- materialized df: IDF + hub pruning
    note_uuid TEXT PRIMARY KEY, query_count INTEGER NOT NULL DEFAULT 0,
    last_activated_at TEXT, total_strength REAL NOT NULL DEFAULT 0.0
);
```

Four design notes worth defending:

- **Composite PK `(query_id, note_uuid, source)`.** One note routinely fires via
  *both* `vector_seed` and `graph_expansion` in a single query. Those are
  different evidence and both belong; the fingerprint aggregates them.
- **`WITHOUT ROWID`** — pure key plus small payload, always accessed by prefix or
  by the `note_uuid` index. ~25% less disk, one less indirection.
- **`surface` is not decoration.** Hermes `prefetch()` fires a recall on *every
  conversational turn* (`integration/core.py:125`). Those are ambient, not
  intentional. Without `surface`, fingerprint space would be dominated by
  automatic prefetches rather than by questions you actually asked. Clustering
  excludes `hermes_prefetch` by default.
- **No FK from `query_activations.note_uuid` → `notes.uuid`.** The ledger is
  history; it must outlive the note.

### 3.3 Relationship to `feedback` — keep it exactly as it is

`feedback` is the *answer + user-signal* record. `queries` is the *retrieval*
record. They are not the same event:

- A `queries` row is created by the orchestrator **before** an answer exists —
  the GUI streams the answer afterwards (`gui/app.py:544-559`), and Hermes
  prefetch produces no answer at all.
- `daemon search` and future retrieval-only surfaces have no feedback row.

Link one direction via `queries.feedback_id`. **This separation is precisely
what dissolves the GUI-bypasses-`QueryEngine` duplication**: the orchestrator
always writes `queries` + `query_activations`; whoever synthesizes writes
`feedback` and back-links. Add `selected_note_uuid` and `query_uid` to
`feedback` as nullable columns; keep `selected_note_path` (it's a useful
human-readable breadcrumb and `analysis/structural.py:391` reads it).

For the `agent_*_runs` tables: add `note_uuid TEXT` beside `note_path`, backfill,
and change the *lookup* to prefer uuid with a path fallback. Do **not** change
those primary keys — a PK migration in SQLite means a table rebuild, for nil
benefit on idempotency bookkeeping.

### 3.4 Themes

```sql
CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE, label TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
    centroid_json TEXT NOT NULL,                    -- {"<note_uuid>": w, …} top-N, L2-normed
    query_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    runs_seen INTEGER NOT NULL DEFAULT 1, runs_missing INTEGER NOT NULL DEFAULT 0,
    snapshot_id TEXT, model TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',        -- proposed|accepted|rejected|dormant|merged
    merged_into INTEGER REFERENCES themes(id),
    label_locked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS theme_notes (
    theme_id INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    note_uuid TEXT NOT NULL, weight REAL NOT NULL, PRIMARY KEY (theme_id, note_uuid)
);
CREATE TABLE IF NOT EXISTS theme_tag_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    note_uuid TEXT NOT NULL, tag TEXT NOT NULL,
    proposed_at TEXT NOT NULL, decided_at TEXT,
    decision TEXT,                                  -- accepted | rejected | deferred
    write_result TEXT,
    UNIQUE (theme_id, note_uuid, tag)
);
```

### 3.5 Backfilling history from `retrieval_summary`

Worth doing — there's real signal in the existing rows. Map `note_path` → uuid
via `notes.rel_path`; `graph_distance == 0` → `vector_seed`, else
`graph_expansion`; `strength` from `rank` via the *same* function used for live
rows (so backfilled and live fingerprints are directly comparable);
`origin='backfill'`, `surface='unknown'`; `ts` from `feedback.timestamp`.

Honest limitation to surface in the report: a query whose top note was later
renamed gets a *partial* fingerprint, which makes it look less similar to
everything than it should. **Report the drop rate. Above ~20%, the backfill is
arguably more noise than signal and the user should be told.**

---

## 4. The migration mechanism

The current `PRAGMA table_info` → `ALTER TABLE` hack
(`stores/feedback.py:53-59`) doesn't scale to five new tables. Replace with
`PRAGMA user_version` + an ordered ladder in a new `stores/db.py`:

```python
MIGRATIONS: list[tuple[int, str, Migration]] = [
    (1, "baseline_feedback_and_agent_state", _m001_baseline),
    (2, "note_registry_and_ordinals",        _m002_registry),
    (3, "activation_ledger",                 _m003_ledger),
    (4, "uuid_columns_on_legacy_tables",     _m004_uuid_columns),
    (5, "themes_and_proposals",              _m005_themes),
]

def migrate(db_path: Path, *, target: int = SCHEMA_VERSION) -> list[int]:
    """Each step runs in its own BEGIN IMMEDIATE txn that also bumps
    user_version, so a crash mid-ladder leaves a consistent earlier version."""

def open_state_db(db_path: Path, *, read_only: bool = False,
                  min_version: int = SCHEMA_VERSION) -> sqlite3.Connection:
    """Single entry point for every store. Runs the ladder once per process per
    file. read_only (snapshot bundles) NEVER migrates — the file is a frozen
    artifact, possibly on read-only media. Raises SnapshotSchemaTooOld."""
```

**The crux that makes this adoptable:** migration 1 must be an idempotent no-op
against databases in the wild. Every current `feedback.db` has
`user_version == 0` but already contains `feedback` (with its three additively
added columns) and the four `agent_*_runs` tables. So `_m001_baseline` is
*exactly today's code* — both `_SCHEMA` scripts plus the `PRAGMA table_info`
loop — then stamps `user_version = 1`. A fresh db and a five-month-old db
converge at v1 with identical structure. The hack is then retired forever.

Two fixes to land here, required once the ledger triples write volume across
four processes (CLI, GUI, Hermes provider, scheduled `consolidate`):

- **`PRAGMA journal_mode = WAL`** — today a `consolidate` run holding a write
  lock blocks the GUI.
- **`PRAGMA busy_timeout = 5000`** — today a concurrent write raises
  `database is locked` immediately.

Also note `PRAGMA foreign_keys` is **off** on every connection today
(`feedback.py:45`, `agent_state.py:62`), so any existing `REFERENCES` clause is
inert. The helper turns it on.

Old snapshot bundles predate the new schema. Handle by **degrading** —
`SnapshotSchemaTooOld` → skip clustering, still write the letter — rather than
bumping `BUNDLE_VERSION` and invalidating every bundle you have.

CLI: `daemon migrate db` (apply), `daemon migrate status` (current vs target).

---

## 5. Fingerprints

### 5.1 What a fingerprint is

A sparse map `{note_uuid: float}` per query, built from the activation rows:

```python
STRENGTH_BY_SOURCE = {                      # config: fingerprint.source_weights
    "vector_seed": 1.0, "graph_expansion": 0.6, "keyword": 0.5,
    "selected": 1.5,                        # the user confirmed this one
    "recall_injected": 0.3,
}

def _strength(source: str, rank: int | None) -> float:
    positional = 1.0 / math.log2((rank or 1) + 1)   # DCG-style, rank 1 → 1.0
    return STRENGTH_BY_SOURCE[source] * positional
```

Sum per `note_uuid` across sources → multiply by IDF `log(1 + N/(1+df))` →
L2-normalize → persist `l2_norm`. Similarity is cosine.

**Rank-based, not score-based, deliberately.** Raw scores are incomparable
across the dense→hybrid-RRF transition (`stores/vector.py:162-171`), across
embedding-model changes, and across the historical backfill. Rank survives all
three. `raw_score` is stored anyway so a future re-derivation stays possible.

**The IDF factor is the single most important quality knob.** Without it, an
MOC/index note that graph expansion drags into *every* query makes every
fingerprint look identical, and fingerprint similarity collapses into "did you
touch the hub."

### 5.2 Backend: SQLite inverted index, not a second Qdrant collection

A `query_fingerprints` collection with a named sparse vector is the obvious move
and it is the wrong one here:

- **100k queries is ~14 years at 20 queries/day** for a single-user system.
  Designing storage for it now is speculation, paid for in bugs.
- **The clustering phase wants the whole matrix in memory anyway** — a
  `scipy.sparse.csr_matrix`, ~30 MB at 2.5M nnz. That is SQLite's native output
  shape. Qdrant would require pulling every vector back out to build the same
  matrix, so it removes no work from the phase that does the most work.
- **Availability.** Recording an activation must never fail because Qdrant is
  down. The ledger is the system's memory of itself.
- **Snapshots.** `stores/snapshot.py` freezes exactly one collection. A second
  one bumps `BUNDLE_VERSION` and rejects every existing bundle.
- Qdrant sparse similarity is **dot product, not cosine** — you'd have to
  L2-normalize on both upsert and query. Easy to get wrong.

Query cost is driven by **skew, not size**: rows scanned = Σ df over the probe's
terms. Prune probe terms above `max_df_ratio` (default 0.25) using
`note_activation_stats`, add a `lookback_days` window (default 180 —
semantically correct anyway, since old fingerprints reference notes since split,
merged, or deleted), and this is a handful of index scans over a few thousand
rows.

| Ledger size | Qdrant | SQLite+numpy | Call |
|---|---|---|---|
| 1k queries (~25k rows) | ~2 ms, all network | <1 ms | **SQLite**, decisively |
| 10k (~250k rows) | ~2 ms | ~3-8 ms pruned | **SQLite** — the gap is sub-perceptual, the complexity isn't |
| 100k (~2.5M rows) | ~3 ms | ~20-60 ms pruned | Qdrant starts to win |

Keep the escape hatch cheap: populate `note_ordinals` and `queries.l2_norm` from
day one, add `fingerprint.backend: sqlite | qdrant` to config. Switching later is
one backfill command plus one implementation of the same `FingerprintIndex`
protocol. Nothing above the interface changes.

### 5.3 Interface

```python
# src/my_daemon/stores/activations.py

class ActivationLedger:
    def record(self, *, query_uid, text, surface, ts, activations, …) -> int: ...
    def link_feedback(self, query_uid: str, feedback_id: int) -> None: ...
    def fingerprint(self, query_id: int) -> dict[str, float]: ...
    def similar(self, probe, *, top_k=5, since=None, min_score=0.15,
                max_df_ratio=0.25, exclude_query_id=None,
                surfaces=None) -> list[FingerprintHit]: ...
    def matrix(self, *, since=None, max_df_ratio=0.25, surfaces=None,
               limit=5000) -> tuple[csr_matrix, list[int], list[str]]: ...
    def note_df(self, note_uuids) -> dict[str, int]: ...
    def compact(self, *, older_than: datetime) -> CompactionReport: ...
```

### 5.4 Clustering, inside `daemon consolidate`

In `pipeline/agent_observe.run_observe`, after `simulate_evolution` and before
the letter. `ledger.matrix()` → cosine distance on L2-normalized rows (capped at
`limit=4000` → 64 MB dense) → `sklearn.cluster.HDBSCAN(metric="precomputed",
min_cluster_size=3)`.

**HDBSCAN's noise label matters: most queries should not get a theme.** Forcing
every query into a cluster is how you get meaningless themes.

Stability across runs is what makes or breaks this feature:

- Match new clusters to existing `themes` by centroid cosine ≥ 0.60, greedily,
  best-first. A match **reuses the theme id, label, and summary** — no LLM call,
  no churn in what you see. `runs_seen += 1`, centroid EMA-blended (α = 0.3)
  rather than replaced.
- Unmatched new cluster → new theme, `status='proposed'`, gets an LLM label.
- Existing theme with no match → `runs_missing += 1`; dormant at 3. **Never
  deleted**, so a theme that flickers doesn't vanish from your history.
- `label_locked = 1` once you accept a theme; the LLM never renames it after.
- Report a **churn metric** in the observer letter:
  `1 - mean Jaccard(membership now, last run)`. Above ~0.5 the letter should say
  "your themes aren't settled yet" rather than presenting noise as insight.

**Naming cost is bounded by construction.** Only *new* clusters get named,
capped at `max_new_themes_per_run: 5`, on `llm.batch_model` (Haiku), from ~500
input tokens of note **titles** (`notes.title`, never chunk bodies) plus three
representative query strings. Steady state is zero or one call per night —
negligible next to the existing Opus observer letter.

---

## 6. The write seam

`RetrievalOrchestrator.retrieve()` is the only 100%-coverage point, but handing
it a `FeedbackStore` and an `ActivationLedger` makes it a god object and breaks
every test that constructs it. Use an **observer callback**:

```python
# src/my_daemon/retrieval/trace.py
class RetrievalListener(Protocol):
    def on_retrieval(self, trace: RetrievalTrace) -> str | None:
        """Persist/observe. Returns a query_uid, or None. MUST NOT raise."""

# retrieval/orchestrator.py
def __init__(self, …, listeners: Sequence[RetrievalListener] = ()) -> None: ...
def retrieve(self, query: str, *, surface: str = "unknown",
             session_id: str | None = None) -> RetrievalResult: ...
```

- `listeners` defaults to `()`, so every existing construction site and every
  existing test keeps working untouched.
- The trace is built from data `retrieve()` already holds. `note_uuid` comes off
  the Qdrant payload, which `seed_search` and `expand_from_seeds` **already
  fetch** (`with_payload=True`). Add `Chunk.note_uuid` as an optional field and
  populate it in `retrieval/seed.py` and `expand.py`. Zero extra I/O.
- Each listener runs inside `try/except Exception: log.warning(…)`. **A ledger
  failure must never break a query** — the daemon degrades to exactly today's
  behavior.
- `RetrievalResult` gains `query_uid: str | None = None` (additive).

The orchestrator's total new knowledge is one Protocol. It never imports
sqlite, the ledger, or the registry.

**One wiring factory.** The real disease is four independent construction sites
(`cli.py:_build_*` ×~10, `gui/app.py:_build_context`, `QueryEngine.__init__`,
`integration/core.build_core`) that will drift the moment `listeners=` exists.
Add `integration/wiring.py` with `build_stores(settings)` /
`build_orchestrator(stores)`; make the other four thin users of it.

**The GUI moves onto `DaemonCore` in two steps, not one.** It streams
(`gui/app.py:546`) and `DaemonCore` has no streaming method; designing
`ask_stream` under time pressure is scope creep. Instead, add
`DaemonCore.retrieve_only()` and `DaemonCore.log_answer()`, and replace only the
two hand-rolled duplications: `gui/app.py:551-559` → `log_answer`, and
`gui/app.py:178-191` → `DaemonCore.endorse`. Note `endorse` is currently gated
on `hermes.allow_write_back`, which is wrong for the GUI where the user is
clicking a button — split it: `allow_write_back` gates *capture*, a new
`feedback.reinforce_enabled` (default `true`) gates *reinforcement*.

**`daemon search` (`cli.py:289`) keeps bypassing.** It's an explicit debug probe;
recording it would pollute fingerprint space with queries you never meant. Add
`--record` if opt-in is wanted.

---

## 7. Milestones

Each leaves the system working, tested, and shippable.

- [x] **M-mem-0 — Migration framework + DB hygiene — shipped 2026-07-26.**
  `stores/db.py` ladder, `_m001_baseline` idempotent against existing DBs, WAL +
  `busy_timeout` + `foreign_keys`. `daemon migrate db|status`.
  **Win:** concurrent GUI + CLI + consolidate stop throwing `database is locked`.

- [x] **M-mem-1a — UUID backfill — shipped 2026-07-26.**
  `set_frontmatter_key_textual` / `remove_frontmatter_key_textual`,
  `is_writable(allow_agent_folder=)`, `vault/identity.py`, `Note.uuid`,
  migration 2 (`notes` + `note_ordinals`), `stores/registry.py`,
  `daemon migrate assign-uuids | list-runs | rollback-uuids`.
  **Win:** every note has a stable identity; the report and registry count
  the notes left on a fragile path-derived one.

- [x] **M-mem-1b — Differential graph update — shipped 2026-07-26.**
  `GraphStore.update_note()` replacing the `remove_note` + `add_note` pair in
  both `ingest_vault` and `ingest_note`. Fixes bug A (§0.3).
  **Win:** editing a note stops deleting inbound wikilinks and stops resetting
  learned weights. M1's adaptive weighting finally accumulates.
  *Shipped ahead of M-mem-1a, as §0.3 called for — it needs no UUID.*
  Turned up a related defect on the **deletion** path (incremental vs. full
  rebuild diverge when a linked note is deleted); logged in
  PROJECT_MANAGEMENT.md rather than folded in.

- [x] **M-mem-2/3/6 — the identity cutover — shipped 2026-07-26.**
  Collapsed into one change once a full re-ingest became acceptable (see the
  note below). Chunk ids derive from `note_uuid`; graph nodes are
  `note::<uuid>` with the display path carried on the node; unresolved
  wikilinks moved to a `dangling::<text>` namespace; the Qdrant payload gains
  `note_uuid` plus payload indexes on it *and* `note_path`; the manifest is
  uuid-keyed and content-hashed; ingest populates and soft-deletes registry
  rows; `apply_selection` and the whole retrieval path key on identity.
  **Win:** renaming or folder-moving a note costs no embedding and keeps every
  learned edge weight — the Librarian can finally reorganize without amnesia.
  Plus the `note_path` scroll in graph expansion, which ran for every seed of
  every query against **no index**, is now indexed.

- [ ] **M-mem-4 — The ledger and the seam.** *Reversible.* Migration 3,
  `RetrievalListener`, `ActivationRecorder`, `wiring.py`,
  `DaemonCore.retrieve_only`/`log_answer`, GUI de-duplication, historical
  backfill.
  **Win:** `daemon activations <id>` and `daemon notes hot --days 30` — see
  which notes your attention actually lands on.

- [ ] **M-mem-5 — Fingerprints and recall.** *Reversible, config-gated.* IDF,
  cosine, `similar()`. Injection into `LLMClient.synthesize`; a "You've been here
  before" strip in the GUI and `daemon query -v`.
  **Win: the headline feature.** A differently-worded question surfaces "you
  asked something like this on 12 March, and it landed on the same three notes."

- [ ] **M-mem-7 — Themes.** *Reversible.* Migration 5, `cluster_fingerprints`
  inside `run_observe`, centroid matching, LLM naming for new clusters only,
  churn metric, themes section in the observer letter.
  **Win:** the dream phase names what you've been circling.

- [ ] **M-mem-8 — Tag proposals.** *Reversible per-note.* `daemon themes review`
  + a GUI panel; acceptance routes through the existing `vault.writer.add_tags`
  with its snapshot and grace window. Rejections remembered so the same tag isn't
  re-proposed.
  **Win:** the loop closes — emergent themes become durable vault structure, with
  consent.

- [ ] **M-mem-9 — Cleanup.** GUI fully onto `DaemonCore.ask_stream`, drop the
  path-based `delete_by_note`, drop `manifest_version: 1` support.

> **Revision, 2026-07-26.** Evan confirmed the project is pre-production and a
> full re-ingest is acceptable, and asked for the cleanest cut available. That
> removed both constraints the staged dual-key transition existed to satisfy —
> avoiding a re-ingest, and keeping the one-way door late. M-mem-2, -3 and -6
> were therefore collapsed into a single identity cutover, deleting the
> `manifest_version` converter, the `daemon migrate qdrant-payload` command,
> `relabel_to_uuid`, and every path-fallback branch before they were written.
> `daemon ingest --full` is required once after this change.

**Two properties of this ordering.** Nothing before M-mem-6 requires the graph to
change — M-mem-4 and -5 (the entire fingerprint payoff) only need `note_uuid`
resolvable from a Qdrant payload. So the one-way door sits as late as possible
and is justified on its own merits rather than as a prerequisite. And **no
milestone requires a full re-ingest.**

---

## 8. Config additions

```yaml
memory:
  recall_enabled: true
  inject_into_context: true      # into the LLM prompt
  show_to_user: true             # into the CLI/GUI surface
  min_score: 0.15
  top_k: 3
  lookback_days: 180
fingerprint:
  backend: sqlite                # sqlite | qdrant
  max_df_ratio: 0.25             # hub-pruning threshold
  compact_after_days: 365
  prefetch_compact_after_days: 30
  source_weights: {…}
uuid:
  frontmatter_key: uuid
  adopt_keys: [uuid, uid, id, guid, note-id, permanent_id]
consolidation:
  cluster_themes: true
  min_cluster_size: 3
  theme_match_threshold: 0.60
  max_new_themes_per_run: 5
```

---

## 9. Risks

**Ledger growth.** ~33 MB/year at 30 queries/day is trivial — **but** Hermes
`prefetch()` fires on every conversational turn (`integration/core.py:125`),
potentially 10-50× the intentional query rate. A heavy Hermes user could see
500 MB/year, which starts to matter for `conn.backup()` in `create_snapshot`.
Mitigate with **compaction, not deletion**: collapse a query's activation rows
into `queries.fingerprint_json` (top-20, normalized) and drop the detail. The
fingerprint stays comparable forever at reduced fidelity; only per-source
forensics are lost. `surface='hermes_prefetch'` gets a 30-day default — its
per-turn detail is the least valuable data in the system. Second mitigation:
`text_sha256` dedupe, so a repeated identical query updates rather than
inserting a near-duplicate that would then dominate its own similarity results.

**Cluster instability.** Fingerprint clusters over a mutating vault are
inherently unstable — one new hub note can re-partition everything. Defenses, in
order: centroid matching plus label locking (user-visible labels are far more
stable than the underlying partitions), EMA-blended centroids, the honest churn
metric, never deleting themes (only dormancy), and `min_cluster_size ≥ 3` so a
burst of two related queries doesn't mint a theme. Residual risk: after a large
vault reorganization, fingerprints referencing `derived_path` uuids break
wholesale — which is why `daemon status` warns when that count is high.

**Self-reinforcing themes.** The real cost risk isn't tokens, it's a feedback
loop: theme tags → new tag edges in the graph → different expansion → different
fingerprints → new themes. Break it explicitly: confirmed theme tags carry a
`theme/<slug>` prefix, and clustering **ignores graph activations that arrived
via a `theme/*` tag edge**. Without that, the system will confidently converge
on its own reflection.

**Hand-edited UUIDs.** Governed by the §1.1 invariant — frontmatter wins.

| Situation | Policy |
|---|---|
| `uuid:` deleted | **Restore** the registry's uuid for that path; never mint new — that would orphan the note's entire history. `uuid_source='restored'`. |
| `uuid:` is garbage | Treat as absent → row above. |
| Note duplicated (copy / sync conflict) | The file whose `rel_path` matches the registry keeps it; the other is re-minted. Surfaced in the ingest report. |
| Two notes' uuids swapped | Undetectable in general. Accept, with a heuristic warning when `body_sha256` changes >90% *and* `rel_path` changed in the same pass. |
| Vault synced from another machine | Frontmatter wins; registry rebuilt from it. `derived_path` notes diverge across machines — the documented cost of `daemon: ignore`. |
| Rename-with-update-links | Mass mtime bump → mass re-ingest. **Destructive before M-mem-1b** (§0.3) — another reason 1b ships early. |

None of these are silent; every reconciliation writes a line to the ingest
report.

**Privacy.** `queries.text` stores raw query text indefinitely. `feedback.query`
already does, so this isn't new — but the ledger makes the corpus far larger and
the "you've been here before" surface makes it visible. Add
`daemon forget --query <id>` and `--before <date>`, and a line in the README.

**The `surface` default.** `retrieve()`'s new `surface` kwarg defaults to
`"unknown"`. Every call site must be updated or the ledger's most important
discriminator silently degenerates. Grep-able, and worth a test asserting no
`"unknown"` rows appear from shipped call sites.

**Migration disk cost.** The batch backup is a full second copy of the vault
under `Agent/backups/migrations/<run>/`. Check free space and report the size
before applying; `--no-backup` for git-tracked vaults, behind a typed
confirmation.

**The YAML round-trip risk (§1.3).** If `set_frontmatter_key_textual` is skipped
and `add_tags`' round-trip is reused, the migration reformats every frontmatter
block in the vault. This is the single most likely way to make a user distrust
the tool. It deserves a dedicated test asserting byte-identical output outside
the inserted line, over fixtures with comments, flow-style lists, dates, CRLF,
and a BOM.

---

## 10. Open questions

1. **Does `daemon: ignore` deserve a middle setting?** Today it means "never
   write." The UUID case is different in kind from the agent-prose case — a
   `daemon: ignore-prose` that still permits an identity stamp would shrink the
   `derived_path` population, which is the only rename-fragile class left.
2. **Should the identity/"purpose" seed note** (PLAN-HERMES §14, still open) be
   the first note stamped, as a smoke test of the whole migration?
3. **Do queries deserve their own notes?** A confirmed theme could write a
   `<vault>/Agent/themes/<slug>.md` that is itself ingested and recallable —
   making the daemon's model of you browsable in Obsidian. Deliberately out of
   scope for M-mem-8; worth deciding before it.
4. **Negative activations.** PROJECT_MANAGEMENT.md records a standing decision
   that M1 is positive-only. The ledger *records* non-selected candidates, so
   the data for down-weighting will exist for the first time. Whether to use it
   stays a separate decision.
