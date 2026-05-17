# Getting Started

## Prerequisites

- **Python 3.11 – 3.14**. The project pins to this band in `pyproject.toml`.
- **Docker** (for Qdrant). The included `docker-compose.yaml` runs Qdrant 1.11
  locally on ports 6333 (HTTP) and 6334 (gRPC).
- An **Anthropic API key** for LLM synthesis. Free-tier keys work for v0.1
  workloads; budget Sonnet 4.6 at the default settings.

## Install

### Windows 11 (one-click)

1. Install [Python 3.11+](https://www.python.org/downloads/) — tick **Add
   Python to PATH** during install.
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).
3. Double-click **`setup.bat`** at the project root. It creates `.venv`,
   installs dependencies, and seeds `config.yaml` + `.env`.
4. Run `daemon setup` (or double-click `launch-setup.bat`) to pick your vault
   folder, paste your API key, and optionally schedule the daily reflection
   job.

### Linux / macOS

```bash
python setup.py              # creates .venv, installs deps, copies templates
# or step by step:
uv sync                      # or: pip install -e .[dev]
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml` to point `vault.path` at your Obsidian vault, and add your
`ANTHROPIC_API_KEY` to `.env`.

### Start Qdrant

```bash
docker compose up -d
```

This boots a local Qdrant on `http://localhost:6333` with storage persisted to
`./data/qdrant/`.

## First Ingest

```bash
daemon ingest -v
```

The first run downloads the embedding models (BGE-small for dense, BM42 for
sparse) into `./data/models/` and embeds every chunk in your vault. Subsequent
runs are incremental — only changed notes get re-embedded. A small JSON
manifest at `./data/manifest.json` tracks each note's mtime and chunk ids.

## First Query

```bash
daemon query "what was I working through about graph-augmented retrieval"
```

You'll see:

- The synthesized answer in a cyan-bordered panel.
- A ranked **candidates** table (at least three, per project policy) showing
  score, source note, heading path, and a preview.

Every query is logged to `./data/feedback.db`. Phase 4 will use that log to
learn from which candidate you actually picked.

## Chat (GUI)

```bash
daemon chat
```

Opens a **native desktop window** (pywebview-backed) by default. Same
retrieval pipeline, streaming responses, and the same feedback logging.
Pass `--no-native` for a browser tab at `http://127.0.0.1:8765` — useful
for remote dev work over SSH.

Per-OS one-action launches:

| Platform | Path |
|---|---|
| Windows | Double-click `launch-gui.vbs` (silent). `launch-gui.bat` is the debug fallback. |
| Linux | `cp my-daemon.desktop ~/.local/share/applications/`, then launch from the activities menu. |
| macOS | Double-click `launch-gui.command` from Finder. |

Because the native window hides stdout, the chat writes a rotating log to:

| Platform | Log path |
|---|---|
| Windows | `%LOCALAPPDATA%\my-daemon\daemon.log` |
| macOS | `~/Library/Logs/my-daemon/daemon.log` |
| Linux | `$XDG_STATE_HOME/my-daemon/daemon.log` (or `~/.local/state/...`) |

The setup window prints the resolved path; `daemon chat --no-native` shows
the same output live.

## What lives where

```
./
├── config.yaml           # all knobs
├── .env                  # ANTHROPIC_API_KEY (gitignored)
├── data/                 # gitignored — everything the daemon owns
│   ├── qdrant/             # vector store persistence
│   ├── models/             # embedding model cache (offline after first pull)
│   ├── graph.gpickle       # NetworkX pickle
│   ├── manifest.json       # ingest state
│   └── feedback.db         # SQLite query + agent-state log
└── <your vault>/         # never touched, unless agent.enabled = true
```

## Troubleshooting

- **"ANTHROPIC_API_KEY is not set"** — `daemon setup` is the easiest fix on
  Windows (writes `.env` and runs `setx`). On Linux/macOS, edit `.env` or
  export the variable in your shell profile.
- **"Collection 'chunks' uses the old single-vector schema; dropping it"** —
  expected when upgrading to the hybrid retrieval feature. Re-run
  `daemon ingest --full` to repopulate.
- **First chat send is slow** — the embedding model and graph load lazily on
  the first query, then stay warm.
- **Opus 4.7 errors about `temperature`** — leave `llm.temperature: null` for
  reasoning models. Sonnet and Haiku accept a float.
