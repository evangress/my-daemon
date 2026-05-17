# `pipeline/` — End-to-end pipelines

Source: `src/my_daemon/pipeline/`.

Five entry points, all stateless top-level functions or thin classes. The CLI
is the primary caller; nothing here knows about Typer or Rich.

## `ingest.ingest_vault`

```python
ingest_vault(settings, embedder, vector_store, graph_store,
             sparse_embedder=None, full_rebuild=False,
             progress=None) -> IngestStats
```

The full ingestion flow:

1. Build a `VaultReader` from `settings.vault`.
2. If `full_rebuild`, clear the in-memory graph and start with an empty
   manifest. Otherwise load both from disk.
3. `ensure_collection()` on the vector store (creates schema, or detects and
   drops a legacy single-vector collection).
4. Read every note. Compare each against the manifest's stored mtime.
5. **Deletions:** anything in the manifest no longer on disk gets removed
   from both stores.
6. **New/changed:** chunk → embed → upsert vectors → update graph. The
   manifest is updated with the new mtime and chunk ids.
7. Save the graph pickle and the manifest.

`progress(i, total, rel_path)` is called once per processed note for CLI
status output.

Returns an `IngestStats` dataclass with `notes_scanned`,
`notes_new_or_updated`, `notes_deleted`, `chunks_upserted`,
`skipped_unchanged`, and an `errors` list (per-note failures don't abort the
run — they accumulate).

### Incremental dedup keys

A note is "unchanged" iff its current mtime ISO string matches the stored
one. Hash-based dedup is intentionally avoided: hashing every body on every
run would defeat the speedup. If you suspect false hits, `--full` is the
escape valve.

## `query.QueryEngine`

```python
QueryEngine(settings, embedder, vector_store, graph_store, feedback_store,
            llm_client, sparse_embedder=None)
  .ask(query, synthesize=True) -> QueryResponse
```

`ask`:

1. Run the orchestrator → `RetrievalResult`.
2. If `synthesize`, call `llm.synthesize(query, ranked)` → answer string.
   With `synthesize=False`, the answer is `""` and only the retrieval is
   returned (the `--no-llm` CLI flag).
3. Build a compact `retrieval_summary` dict (chunk ids, scores, counts).
4. Log a `FeedbackEvent` to the feedback DB; capture the row id.
5. Return `QueryResponse(answer, retrieval, feedback_event_id, latency_ms)`.

The chat UI doesn't use `QueryEngine` directly — it builds the orchestrator
and streams synthesis itself, then logs a feedback event from the UI handler.
Same data, different control flow because the chat needs streaming.

## `agent_extract.run_extract`

```python
run_extract(settings, state, llm, *, all_=False, only_note=None,
            dry_run=False, progress=None) -> ExtractStats
```

Eligibility: notes with `word_count >= agent.extract_min_word_count` whose
mtime is newer than the last recorded extract run (unless `--all` or
`--note` overrides). For each eligible note, call
`extract_note_observations` (Haiku by default), render the markdown body
via `render_agent_notes_body`, and call `vault.writer.write_agent_section`.

A no-op write (sentinel-bounded content already correct) increments
`notes_skipped`; an actual change increments `notes_processed` and records a
row in `agent_extract_runs` with the note's mtime and a hash of the
payload's summary + key_points + themes.

## `agent_link.run_link`

```python
run_link(settings, state, embedder, vector_store, graph_store, llm=None,
         *, only_note=None, dry_run=False, progress=None) -> LinkStats
```

For each note (or the one named):

1. **Link candidates.** Embed the note body, ask Qdrant for top ~40 chunks,
   keep the best per target note, split into auto / suggest by
   `agent.link_apply_cosine` and `agent.link_suggest_cosine`. The strict
   apply gate also requires the target title to appear verbatim in the
   source body.
2. **Tag candidates.** Graph-BFS depth 2, count tags carried by neighbors
   but missing from the source, keep those with count ≥
   `agent.tag_apply_min_neighbor_count`.
3. **LLM second opinion** (optional). If `llm` is provided and there are
   borderline candidates, call `propose_links` on the top ~10 candidates.
4. **Writeback.** Skip if `dry_run`. Otherwise call `insert_wikilinks` and
   `add_tags`. Borderline candidates always accumulate into the daily
   suggestions file.
5. Record a row in `agent_link_runs`.

After the loop, write `Agent/link-suggestions-YYYY-MM-DD.md` if there's
anything to suggest.

## `agent_reflect.run_reflect`

```python
run_reflect(settings, state, feedback, llm, *, only_theme=None,
            dry_run=False, progress=None) -> ReflectStats
```

For each configured theme:

1. Compute `sig = sha256(recent_notes ids+mtimes, recent_chats ids+timestamps)`.
2. If the prior reflect row for this theme has the same `sig` and not a dry
   run, skip — no new evidence.
3. Read the prior memory file (body only; the frontmatter is stripped before
   feeding to the model).
4. `update_memory(...)` returns the new body. The prompt explicitly tells
   Claude to preserve user edits as ground truth.
5. Snapshot the prior file, compose a new file (frontmatter +
   user-facing preamble + new body), and `write_atomic`.
6. Record the new `sig` and chat count in `agent_reflect_runs`.

After the loop, append a one-block summary to `Agent/memory-rolling.md`.

## Why these are separate functions

The three writeback jobs share `vault.writer`, share state in
`AgentStateStore`, and share `llm.agents` — but they don't share each other.
A bug in the linker can't corrupt the reflection run. Each is also
schedulable on its own cron line (the Windows Task Scheduler integration only
schedules `reflect`; you can add `extract` and `link` separately).
