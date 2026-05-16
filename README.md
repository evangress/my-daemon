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
├── gui/                 # NiceGUI chat window (`daemon chat`)
└── pipeline/            # ingest_vault, QueryEngine
```

## Quickstart

### Windows 11 (one-click)

1. Install [Python 3.11+](https://www.python.org/downloads/) — tick **Add Python to PATH** during install.
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for Qdrant).
3. Double-click **`setup.bat`** at the project root. It creates `.venv`, installs all dependencies, and seeds `config.yaml` + `.env`.
4. Open `.env` in Notepad and paste your `ANTHROPIC_API_KEY`. Open `config.yaml` and point `vault.path` at your Obsidian vault.
5. From a terminal in the project folder:
   ```cmd
   docker compose up -d
   .venv\Scripts\activate.bat
   daemon ingest -v
   daemon query "what was I thinking about last week"
   ```

### Linux / macOS

```bash
python setup.py                         # creates .venv, installs deps, copies config templates
# or, if you prefer step-by-step:
uv sync                                 # or: pip install -e .[dev]
```

### Start Qdrant (one-shot, any OS)

```bash
docker compose up -d
```

### Configure

`setup.py` (and `setup.bat`) already copy `config.example.yaml` → `config.yaml` and `.env.example` → `.env`. If you skipped that step, run:

```bash
daemon init --vault ~/Documents/Obsidian/MyVault
```

Then open `.env` and set your `ANTHROPIC_API_KEY`.

### Ingest your vault

```bash
daemon ingest -v
```

Re-running is incremental — only modified notes get re-embedded.

### Ask your daemon

```bash
daemon query "what was I working through about graph-augmented retrieval"
```

You'll see the synthesized answer plus at least three ranked candidate sources. Every query is logged to `data/feedback.db` so a later phase can learn from which one you picked.

### Chat with your daemon (GUI)

For a warmer, less-terminal experience, launch the NiceGUI chat window:

```bash
daemon chat
```

By default it opens at <http://127.0.0.1:8765> — your browser should launch automatically. Type a question in the input at the bottom; the daemon's reply streams into the chat as it generates. The same retrieval pipeline and feedback logging run behind the scenes, so chat queries also land in `data/feedback.db`.

Flags:

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. Stay on loopback unless you know what you're doing. |
| `--port` | `8765` | Local port for the UI. |
| `--native` | off | Open as a desktop window via `pywebview` instead of in your browser. Requires `pip install pywebview`. |

Stop the server with `Ctrl+C` in the terminal that launched it.

> Heads up: the first send after launch can take a few seconds — the embedding model and graph load lazily on the first query, then stay warm for the rest of the session.

## Useful commands

| Command | What it does |
|---|---|
| `daemon chat` | Launch the warm-themed NiceGUI chat window (browser or `--native` desktop window) |
| `daemon models download` | Pre-pull the embedding model into the local cache so queries stay offline afterward |
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
