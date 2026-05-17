# Architecture

A bird's-eye view of how a markdown file becomes a cited answer.

## The pipeline, in one picture

```
  Obsidian vault (.md files)
            │
            ▼
  ┌──────────────────────────────┐
  │  vault/reader   parser       │  walk dir; parse frontmatter, wikilinks, tags
  │  vault/chunker               │  split on H1/H2/H3, then by token budget
  └──────────────────────────────┘
            │
            ├──► chunks: text + heading_path + tags + wikilinks (per chunk)
            ▼
  ┌──────────────────────────────┐
  │  embeddings/embedder         │  BGE-small (dense, 384-d)
  │  embeddings/sparse           │  BM42 (sparse, attention-weighted)
  └──────────────────────────────┘
            │
            ▼
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │  stores/vector  (Qdrant)     │        │  stores/graph (NetworkX)      │
  │   named slots: dense+sparse   │        │   notes ↔ wikilinks ↔ tags    │
  └──────────────────────────────┘        └──────────────────────────────┘
            │                                          │
            └────────────────┬─────────────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  retrieval/seed              │  top-K via Qdrant RRF
              │  retrieval/expand            │  BFS depth N, score *= decay^d
              │  retrieval/orchestrator      │  dedupe → rank → trim to budget
              └──────────────────────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  llm/client  +  llm/prompts  │  Claude synthesis, with citations
              └──────────────────────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  stores/feedback (SQLite)    │  log every query for Phase 4
              └──────────────────────────────┘
```

## Why two stores

The vector store excels at *semantic* recall — "things that sound like what I
asked." The graph excels at *associative* recall — "things you yourself
connected to this." Combining them captures both how you mean a question and
how you previously organized the answer.

Hybrid retrieval inside the vector store layers a third signal: a BM25-style
sparse encoder (BM42) catches proper nouns, project names, and other
exact-token recall that dense embeddings tend to blur. Dense and sparse
rankings are fused server-side in Qdrant via Reciprocal Rank Fusion.

## Data flow detail

### Ingest

1. `VaultReader.read_all()` walks the vault root, skipping configured
   directories (`.obsidian`, `.trash`, `templates`, `Agent`).
2. Each file is parsed into a [`Note`](modules/vault.md) — title, body,
   frontmatter, wikilinks (raw target strings), tags (frontmatter + inline).
3. A second pass builds a `{lowercased_title → relative_path}` index so
   wikilinks resolve to real notes; dangling targets are kept as
   placeholder graph nodes.
4. `chunk_note()` splits each note on headings (H1/H2/H3), then secondary-splits
   any oversized piece by token budget with overlap. Chunk ids are a stable
   SHA-1 of `(note_path, heading_path, chunk_index, content)`.
5. New/changed chunks are embedded (dense + sparse if hybrid) and upserted
   into Qdrant. The graph store gets an updated note node with its chunk ids
   plus edges for each wikilink target and tag.
6. A manifest at `./data/manifest.json` records `{relative_path: mtime,
   chunk_ids}` so the next run can skip unchanged notes. Notes that disappear
   from disk are deleted from both stores.

### Query

1. `seed_search()` embeds the query (dense + sparse if hybrid) and asks
   Qdrant for top-K. Qdrant runs RRF on the two prefetches server-side.
2. `expand_from_seeds()` looks up each seed's parent note in the graph and
   does a BFS to depth `graph.expansion_depth` (default 2). For each visited
   note, the expander pulls *all* its chunks via a Qdrant payload filter on
   `note_path`. Each expanded chunk's score = `seed_score × decay^distance`.
3. `RetrievalOrchestrator.retrieve()` merges seeds + expanded, keeps the best
   score for any chunk reachable multiple ways, sorts by combined score, and
   trims to `retrieval.context_token_budget` while honoring a minimum
   candidate floor (`retrieval.candidate_pool`, default 3).
4. `LLMClient.synthesize()` (or `synthesize_stream()` for the chat UI) sends
   the ranked context to Claude with the "you are the user's daemon" system
   prompt and instructions to cite by `note_path › heading`.
5. `FeedbackStore.log()` writes a row containing the query, retrieval
   summary, answer, and latency. A future `daemon select` will attach a
   `candidate_selected` signal to that row.

### Background writeback (when `agent.enabled: true`)

The three writeback jobs share one safety module:
`vault/writer.py`. Every write goes through:

- **Containment check** — the resolved path must live inside `vault.path` (no
  `../../etc/passwd`).
- **Agent-folder guard** — never touch `<vault>/Agent/` (the daemon owns it).
- **Opt-out** — frontmatter `daemon: ignore` disables writes for that note.
- **Grace period** — skip files modified within the last
  `agent.write_grace_minutes` (default 30).
- **Snapshot backup** — copy to `<vault>/Agent/backups/<rel>.<ts>.md` before
  every write.
- **Atomic replace** — write to a temp file and `os.replace()` to the target.

The pipeline modules (`pipeline/agent_extract.py`, `agent_link.py`,
`agent_reflect.py`) wrap the writer with LLM-driven content generation and
per-job dedup state in `data/feedback.db` (the same SQLite file the query log
lives in, since the user only ever needs to back up *one* state file).

## Why these decisions

- **NetworkX, not Neo4j.** A personal vault fits in RAM. The graph
  abstraction is small enough that swapping to Neo4j later is a
  same-week migration if it ever becomes necessary.
- **Qdrant, not FAISS.** Payload filtering ("scroll all chunks for this
  `note_path`") is the operation the expander hammers on every query.
  Qdrant gives it for free; FAISS would have meant a sidecar index.
- **SQLite for feedback.** One file. Zero ops. The query log needs to
  outlive Qdrant resets.
- **Lazy model loading.** The embedder loads on first use, then stays warm.
  That's why the first chat send takes a few seconds and the rest are fast.
- **Streaming synthesis in the chat.** Felt cold without it. The CLI is
  fine being one-shot.
