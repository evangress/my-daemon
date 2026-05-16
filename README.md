# My Daemon

A personal knowledge and memory system over an Obsidian vault. Vector seeds, graph expansion, LLM synthesis — the kind of memory companion that grows with you over time.

This is the **v0.1 first draft** following [`MY-DAEMON-SCAFFOLD.md`](MY-DAEMON-SCAFFOLD.md). The boring end-to-end pipeline works; adaptive weighting, nightly consolidation, and the observer LLM (Phase 4+) are intentionally deferred.

## What's here

```
src/my_daemon/
├── cli.py               # `daemon` Typer entry point
├── config.py            # Pydantic Settings (yaml + env)
├── models.py            # Note, Chunk, RetrievedChunk, FeedbackEvent
├── vault/               # reader, parser, chunker — markdown → Chunks
├── embeddings/          # sentence-transformers wrapper
├── stores/              # Qdrant vectors, NetworkX graph, SQLite feedback
├── retrieval/           # seed → expand → orchestrate
├── llm/                 # Anthropic client + synthesis prompt
└── pipeline/            # ingest_vault, QueryEngine
```

## Quickstart

### 1. Install

```bash
uv sync                                  # or: pip install -e .[dev]
```

### 2. Start Qdrant (one-shot)

```bash
docker compose up -d
```

### 3. Initialize config

```bash
daemon init --vault ~/Documents/Obsidian/MyVault
```

This creates `config.yaml` and `.env`. Open `.env` and set your `ANTHROPIC_API_KEY`.

### 4. Ingest

```bash
daemon ingest -v
```

Re-running is incremental — only modified notes get re-embedded.

### 5. Ask your daemon

```bash
daemon query "what was I working through about graph-augmented retrieval"
```

You'll see the synthesized answer plus at least three ranked candidate sources. Every query is logged to `data/feedback.db` so a later phase can learn from which one you picked.

## Useful commands

| Command | What it does |
|---|---|
| `daemon status` | Vault path, note count, vector chunk count, graph stats |
| `daemon graph stats` | Top-PageRank notes and top tags |
| `daemon search "phrase"` | Vector-only debug search (no graph, no LLM) |
| `daemon query "..." --no-llm` | Show ranked context without calling Claude |
| `daemon query "..." -v` | Show seed/expanded counts, latency, feedback id |
| `daemon ingest --full` | Force a full rebuild |
| `daemon reset` | Wipe `data/`. The vault is never touched. |

## Configuration

Edit `config.yaml`. Any value can be overridden by an env var with the `MY_DAEMON_` prefix and double-underscore nesting:

```bash
export MY_DAEMON_LLM__MODEL=claude-sonnet-4-6
export MY_DAEMON_GRAPH__EXPANSION_DEPTH=3
```

## Development

```bash
pip install -e .[dev]
ruff check .
pytest                              # unit tests (no external services needed)
MY_DAEMON_E2E=1 pytest tests/test_e2e.py  # full pipeline; needs Qdrant
```

## What's deferred (Phase 4+)

- Adaptive edge weighting from feedback signals
- Nightly snapshot consolidation
- Observer LLM that interprets graph structure
- Multi-embedding spaces (emotional, entity-based)
- Obsidian plugin / live file watching
- Local LLM for synthesis

See `MY-DAEMON-SCAFFOLD.md` for the full design and phasing.

## Why "Daemon"?

From Pullman's *His Dark Materials* — a daemon is the external soul-companion that knows you completely. Also Socrates' *daimonion*, the inner advisory voice. Both are companions that know the self. That's the intent here: not a tool you operate, but a companion that grows with you.
