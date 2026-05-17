# My Daemon

*A personal knowledge and memory system over an Obsidian vault. Vector seeds,
graph expansion, LLM synthesis — the kind of memory companion that grows with
you over time.*

---

## Why "Daemon"?

A triple pun:

- Philip Pullman's *daemon* — the external soul-companion in *His Dark
  Materials* that knows its human completely.
- Socrates' *daimonion* — the inner advisory voice.
- The Unix sense — a background process that runs quietly on your behalf.

The intent here is all three: not a tool you operate, but a companion that
grows with you, sometimes whispering, sometimes working quietly while you
sleep.

## What the project does

My Daemon ingests your Obsidian vault and builds two complementary
representations of it:

| Layer | What it stores | What it's good at |
|---|---|---|
| **Vector store** (Qdrant) | Dense + sparse embeddings of every chunk | Paraphrased / conceptual recall, exact-token recall (BM42) |
| **Graph** (NetworkX) | Note ↔ note (wikilinks) and note ↔ tag edges | Following the structure you already gave your notes |

When you ask a question, the daemon:

1. Embeds your query and finds top-K seed chunks (hybrid: dense + sparse RRF).
2. BFS-expands each seed in the graph to depth N, with distance decay.
3. De-duplicates, ranks by combined score, trims to a token budget.
4. Sends the ranked context to Claude with a "you are the user's daemon" prompt.
5. Logs every query, the candidates surfaced, and the latency to a SQLite
   feedback log for future adaptive weighting.

Beyond that, three optional background-agent jobs let the daemon **write back
into the vault** rather than just read from it:

- `daemon extract` — appends an `## Agent Notes` section to recently-changed
  notes.
- `daemon link` — auto-applies high-confidence wikilinks and tags, queues
  borderline ones for review.
- `daemon reflect` — maintains themed memory files in `<vault>/Agent/` that
  distill who you are over time.

## Where to start

- [Getting Started](getting-started.md) — install, configure, ingest, ask.
- [Architecture](architecture.md) — the data flow from a `.md` file to a
  cited answer.
- [CLI Reference](cli.md) — every `daemon` subcommand.
- [Configuration](configuration.md) — every key in `config.yaml`.
- [Background Agents](background-agents.md) — the writeback jobs, the safety
  discipline, and how to schedule them.
- [Roadmap](roadmap.md) — what's done, what's next, what's been intentionally
  deferred.

## Status

v0.1. The end-to-end pipeline (ingest → retrieve → synthesize → log) works
against a real vault. Background-agent writeback is wired up but gated behind
`agent.enabled: false` until you confirm thresholds with `--dry-run`. Adaptive
edge weighting, nightly consolidation, and the observer LLM are deliberate
Phase 4+ deferrals — see [`MY-DAEMON-SCAFFOLD.md`](https://github.com/evangress/my-daemon/blob/master/MY-DAEMON-SCAFFOLD.md)
for the full phasing.
