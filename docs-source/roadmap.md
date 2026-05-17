# Roadmap

## Status

v0.1 is feature-complete against `MY-DAEMON-SCAFFOLD.md`'s Phase 0–3 spec
and the project-level acceptance criteria. The end-to-end pipeline (ingest
→ retrieve → synthesize → log) works against a real Obsidian vault. The
three background-agent writeback jobs ship but are gated behind
`agent.enabled: false` until you confirm their thresholds with `--dry-run`.

## Done

- **Phase 0 — Skeleton.** Repo, `pyproject.toml`, uv setup, ruff + pytest,
  `config.py`, `models.py`, sample test vault, `docker-compose.yaml` for
  Qdrant.
- **Phase 1 — Ingest pipeline.** Vault reader / parser / chunker with
  tests; sentence-transformers embedder; Qdrant + NetworkX stores with
  persistence; `daemon ingest` and `daemon status`.
- **Phase 2 — Retrieval + query.** Seed / expand / orchestrator,
  Anthropic client, synthesis prompt, `daemon query`, SQLite feedback log.
- **Phase 3 — Incremental + polish.** Manifest-based incremental ingest,
  `daemon graph stats` (PageRank + tag degree), README, end-to-end test.
- **Hybrid retrieval (2026-05-16).** Sparse BM42 vectors stored alongside
  dense, fused server-side in Qdrant via RRF. Toggle with
  `embeddings.hybrid` in `config.yaml`.
- **Background-agent writeback (2026-05-16).** Three new commands:
  `daemon extract`, `daemon link`, `daemon reflect`. Shared safety
  discipline in `vault/writer.py`. Defaults to Haiku 4.5 via
  `llm.batch_model`. Tkinter setup window with a Windows Task Scheduler
  checkbox for daily reflection.
- **NiceGUI chat (`daemon chat`).** Streaming responses, brand-aligned dark
  theme, lazy retrieval pipeline.
- **Setup window (`daemon setup`).** Vault picker + API-key persistence
  (`.env` always; on Windows also `setx`).

## Next

In priority order, drawn from the project's "Next Steps" list:

1. **Adaptive edge weighting from feedback signals.** The feedback DB
   already records every query and ranked candidate. A `daemon select
   <id> <#>` command writes a `candidate_selected` signal; a background
   pass mutates `weight` on the graph edges that connected the chosen
   candidate. The pieces are all in place — what's deferred is the *shape*
   of the signal (per-chunk? per-note? does picking candidate #2
   down-weight #1?).
2. **Snapshot-based nightly consolidation.** A scheduled job that snapshots
   the graph, runs classical graph algorithms (Louvain communities, edge
   betweenness, the rest of `python-louvain`), and writes summaries to a
   consolidation log. Distinct from reflection in that it operates on the
   *graph*, not the prose.
3. **Observer LLM that interprets graph structure.** A higher-order pass
   that reads the consolidation log + recent reflection memory and writes
   higher-level pattern files ("you keep returning to X every spring,"
   "this cluster of notes feels load-bearing in your project").
4. **Multi-embedding spaces.** A second embedding alongside the semantic
   one — emotional valence, entity-based, code-vs-prose. Hybrid retrieval
   already proves the multi-signal plumbing; this just adds another slot.
5. **Obsidian plugin / file watcher.** Live ingest as you save. Currently
   re-ingest is a manual or scheduled step.
6. **Local LLM for synthesis.** For users who want fully-offline operation
   end-to-end. The streaming surface already abstracts the provider; a
   local-model client implementing `synthesize` / `synthesize_stream`
   slots in.

## Deferred indefinitely (for a reason)

- **Heavy auth / multi-user.** The daemon is intentionally a single-person
  tool that runs on your machine against your vault.
- **A SaaS frontend.** Same reason. The brand says "companion," not
  "service."
- **Vault rewriting beyond `## Agent Notes` and tags.** The agent edits a
  bounded surface and snapshots before every change. Rewriting prose
  itself would cross the line from companion to ghostwriter.

## Open agent-suggestions (deliberately not implemented)

From [`PROJECT_MANAGEMENT.md`](https://github.com/evangress/my-daemon/blob/master/PROJECT_MANAGEMENT.md):

- **`daemon select <feedback_id> <#>` for candidate-selection feedback.**
  One CLI flag away. Held back to nail down the signal shape first.
- **`daemon query "..." --skip <feedback_id>` for "more like these."**
  A second-three-results request. Useful in CLI but the eventual home is
  the GUI; may not be worth investing twice.
- **`daemon graph todos` from dangling wikilinks.** The graph already
  keeps dangling targets as placeholder nodes. A command that surfaces
  them is "notes you mean to write" — a feature, not a bug.
- **Seed the daemon's own vault with a "purpose" note.** So when the daemon
  ingests its own vault, it can answer "why am I building this" in the
  user's voice.
