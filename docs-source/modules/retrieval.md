# `retrieval/` — Seed, expand, rank

Source: `src/my_daemon/retrieval/`.

Three small functions, one orchestrator. Each is unit-testable in isolation.

## `seed.seed_search`

```python
seed_search(query, embedder, vector_store, top_k=8,
            sparse_embedder=None) -> list[RetrievedChunk]
```

Embed the query (dense, plus sparse if hybrid is on) and ask Qdrant for the
top-K hits. When `sparse_embedder` is provided, calls
`VectorStore.hybrid_search` — Qdrant fuses the two prefetches with RRF
server-side. Otherwise falls back to dense-only `VectorStore.search`.

Each hit becomes a `RetrievedChunk` with:

- `vector_score` = the hit's score (RRF score in hybrid mode, cosine in
  dense-only)
- `graph_distance = 0` (seeds are by definition at distance zero)
- `seed_chunk_id` set to the chunk's own id (seeds are their own seed)
- `combined_score = vector_score`

## `expand.expand_from_seeds`

```python
expand_from_seeds(seeds, graph_store, vector_store,
                  depth=2, decay=0.5) -> list[RetrievedChunk]
```

For each seed:

1. Look up the seed's parent note in the graph.
2. BFS to `depth` hops in the undirected projection (so a sibling via a
   shared tag is at distance 2).
3. For each neighbor note, `scroll` Qdrant with a payload filter on
   `note_path` to pull all its chunks (cap of 64 per note).
4. Score each expanded chunk: `combined = seed.vector_score * decay**distance`.

A chunk reachable from multiple seeds keeps the best score (max-merge,
keyed by chunk id). Chunks whose parent note is already represented by a
seed are skipped to avoid double-counting.

## `orchestrator.RetrievalOrchestrator`

```python
RetrievalOrchestrator(settings, embedder, vector_store, graph_store,
                      sparse_embedder=None)
  .retrieve(query) -> RetrievalResult
```

The composed pipeline:

1. `seed_search` → seeds.
2. `expand_from_seeds` → expanded chunks.
3. Merge seeds + expanded, keeping the best score per chunk id.
4. Sort by `combined_score` descending.
5. **Trim to token budget**, but respect the minimum candidate floor:

   ```python
   for rc in ranked:
       cost = approx_tokens(rc.chunk.text)        # chars // 4
       if kept and used + cost > budget and len(kept) >= min_candidates:
           break
       kept.append(rc); used += cost
   ```

   The candidate floor exists because project policy is to surface at least N
   (`retrieval.candidate_pool`, default 3) candidates to the user even if the
   token budget would otherwise cut off earlier. The first candidate always
   makes it in, regardless of budget — losing all context is worse than
   slightly overshooting.

### Return shape

```python
RetrievalResult(
  query: str,
  seeds: list[RetrievedChunk],     # everything from the vector store
  expanded: list[RetrievedChunk],  # everything walked from the graph
  ranked: list[RetrievedChunk],    # the trimmed list actually sent to the LLM
)
```

The chat UI and CLI both render `ranked` as a candidate table; verbose mode
exposes `len(seeds)` and `len(expanded)` for tuning.

## Why this shape

Hybrid retrieval gives you exact-phrase recall *and* paraphrase recall.
Graph expansion gives you the *structural* recall — the connections you
yourself drew between notes. The token budget keeps Claude's input bounded.
The candidate floor keeps the user in the loop.

The orchestrator is intentionally simple. The Phase 4 plan layers
adaptive edge weighting (mutating those `weight=1.0` defaults based on which
candidates you pick) and an observer LLM on top of this same plumbing — none
of which require changing the retrieval surface above.
