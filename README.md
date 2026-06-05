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
├── integration/         # DaemonCore — the one adapter every front end shares
├── hermes/              # Hermes memory-provider plugin (front-end agent)
└── pipeline/            # ingest_vault, QueryEngine
```

> **Hermes front end.** My Daemon can act as the **memory & dream layer** for the
> [Hermes agent](https://github.com/NousResearch/hermes-agent) (MIT) — Hermes
> *acts*; My Daemon *remembers and dreams*. Ambient cited recall before every
> turn, automatic injection of the daemon's latest observer letter, and
> turn-by-turn capture, all off by default. See
> [`docs-source/integrations/hermes.md`](docs-source/integrations/hermes.md) and
> [`PLAN-HERMES.md`](PLAN-HERMES.md); preflight with `daemon hermes doctor`.

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

`daemon chat` opens a **native desktop window** by default (via `pywebview`, which now ships as a hard dependency). No browser tab, no terminal needed.

| Platform | One-action launch |
|---|---|
| **Windows** | Double-click `launch-gui.vbs` — silent native window, no console flash. (`launch-gui.bat` is a debug fallback that keeps a visible console + browser tab for diagnosing startup errors.) |
| **Linux** | `cp my-daemon.desktop ~/.local/share/applications/` once, then launch "My Daemon" from your activities menu. Or run `./launch-gui.sh` from anywhere. |
| **macOS** | Double-click `launch-gui.command` from Finder. (For a fully terminal-free experience, wrap it in a 1-line AppleScript saved as a `.app` bundle.) |

From a shell:

```bash
daemon chat                # native window (default)
daemon chat --no-native    # browser tab at http://127.0.0.1:8765 — useful for remote dev
```

Flags:

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. Stay on loopback unless you know what you're doing. |
| `--port` | `8765` | Local port (browser mode) / loopback port (native mode). |
| `--native` / `--no-native` | `--native` | Pass `--no-native` for a browser tab. |

Close the window to stop the daemon (native mode); `Ctrl+C` in the terminal (browser mode).

> Heads up: the first send after launch can take a few seconds — the embedding model and graph load lazily on the first query, then stay warm for the rest of the session.

> Logs: because the native window hides stdout, the chat writes to a platform-standard log file — `%LOCALAPPDATA%\my-daemon\daemon.log` on Windows, `~/Library/Logs/my-daemon/daemon.log` on macOS, `$XDG_STATE_HOME/my-daemon/daemon.log` (or `~/.local/state/...`) on Linux. The setup window prints the resolved path.

## Useful commands

| Command | What it does |
|---|---|
| `daemon chat` | Launch the warm-themed NiceGUI chat window (browser or `--native` desktop window) |
| `daemon setup` | Tkinter window: pick the vault folder, paste the API key, opt into daily reflection |
| `daemon extract` | Write an `## Agent Notes` section into recently-changed notes (background agent) |
| `daemon link` | Auto-link / tag notes at strict thresholds; review lower-confidence in `Agent/link-suggestions-*.md` |
| `daemon reflect` | Update themed memory files in `<vault>/Agent/` (the "digital embodiment") |
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

### Background agent jobs

Three writeback jobs help the daemon **shape** the vault, not just read from it. All are gated behind `agent.enabled: true` in `config.yaml`; until you flip that, every command refuses to write and asks you to `--dry-run` first.

| Job | What it writes | Where |
|---|---|---|
| `daemon extract` | An `## Agent Notes` section per recently-changed note: summary, key points, themes, feelings, open questions | inside the note itself, between `<!-- daemon:start -->` / `<!-- daemon:end -->` sentinels (idempotent on re-runs) |
| `daemon link` | Wikilinks at cosine ≥ 0.85 AND verbatim-title match; tags shared by ≥ 4 graph neighbors | inside the note; lower-confidence suggestions accumulate to `Agent/link-suggestions-YYYY-MM-DD.md` for review |
| `daemon reflect` | Themed memory files distilling who you are (personality, projects, relationships, themes) plus a rolling daily journal | `<vault>/Agent/memory-*.md` |

**Safety discipline (built once, applied everywhere):**

- The daemon never touches files inside `<vault>/Agent/` (its own folder).
- A frontmatter `daemon: ignore` on any note removes it from every job.
- Files modified within the last 30 minutes are skipped (avoid colliding with a save in progress).
- Every write is preceded by a snapshot to `<vault>/Agent/backups/<rel>.<unix_ts>.md` — add `Agent/backups/` to your vault's `.gitignore` if you keep the vault in git.
- All three jobs default to `claude-haiku-4-5` via `llm.batch_model` — ~15× cheaper than Opus. Flip back to Sonnet if you want richer extractions.

**Scheduling.** Run on demand or schedule:

- **Windows:** the `daemon setup` window has a checkbox "Run `daemon reflect` daily at 03:00 (Windows Task Scheduler)" that registers/removes the task for you.
- **macOS / Linux:** add `crontab -e` lines like:
  ```cron
  0 3 * * *  /path/to/my-daemon/.venv/bin/daemon reflect
  15 3 * * * /path/to/my-daemon/.venv/bin/daemon extract
  ```

First-time recommendation: `daemon extract --dry-run` against your real vault, eyeball the candidate notes, then `daemon extract --note <one>` on a single note to see the section format. Flip `agent.enabled` after that.

### Hybrid retrieval

Retrieval is **hybrid by default**: a dense embedding (BGE small) and a BM25-style sparse signal (BM42 via `fastembed`) are fused server-side in Qdrant using Reciprocal Rank Fusion. The dense side handles paraphrased / conceptual queries; the sparse side handles proper nouns, project names, and other exact-token recall. Graph expansion runs on the fused seeds as usual.

Flip to dense-only by setting `embeddings.hybrid: false` in `config.yaml` and re-running `daemon ingest --full` (the collection schema differs between modes).

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
