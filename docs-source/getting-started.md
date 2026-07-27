# Getting Started

## Prerequisites

- **Python 3.11 – 3.14**. The project pins to this band in `pyproject.toml`
  (`>=3.11,<3.15`). 3.12 is what the project is developed against.
- **Docker — optional.** The default config runs Qdrant *embedded*, in-process
  against `./data/qdrant-local/`, so nothing extra needs installing. Docker is
  only for the server mode described in
  [Qdrant: embedded or server](#qdrant-embedded-or-server) below; the included
  `docker-compose.yaml` runs Qdrant 1.11 on ports 6333 (HTTP) and 6334 (gRPC).
- An **Anthropic API key** for LLM synthesis. Free-tier keys work for v0.1
  workloads; budget Sonnet 4.6 at the default settings. Get one at
  [console.anthropic.com](https://console.anthropic.com/settings/keys).

## Install

### Windows 11 — step by step

Nothing here needs Docker, and you never paste your API key into a file.

**1. Install Python.** Download [Python 3.12](https://www.python.org/downloads/)
and, on the first installer screen, tick **Add python.exe to PATH** before
clicking *Install Now*. Confirm it took, in a new **Command Prompt**:

```cmd
py --version
```

You should see `Python 3.12.x` (anything from 3.11 to 3.14 is fine). If you get
"not recognized", re-run the installer and choose *Modify → Add to PATH*.

**2. Get the project folder.** Either `git clone` it, or download the ZIP and
**right-click → Extract All**. Extract somewhere you own, such as
`C:\Users\<you>\my-daemon` — avoid `C:\Program Files`, which needs admin rights
to write the `data\` folder. Windows blocks scripts inside a still-zipped
folder, so extract before continuing.

**3. Bootstrap.** Double-click **`setup.bat`** in the project root. It creates
`.venv\`, installs every dependency, copies `config.example.yaml` →
`config.yaml`, and generates the launcher scripts (`launch-setup.bat`,
`launch-gui.vbs`, `launch-gui.bat`). Those launchers do **not** exist in a
fresh clone — `setup.bat` writes them, so run it before looking for them.

If SmartScreen shows "Windows protected your PC", click **More info → Run
anyway**; that prompt is about the file being newly downloaded, not about its
contents. Leave the window open until it prints `Setup complete`.

**4. Configure the vault and key.** Double-click **`launch-setup.bat`** (or run
`daemon setup`). In that window:

- **Vault folder** — browse to your Obsidian vault.
- **API key** — paste your Anthropic key. The field is masked; tick *Show* to
  check it. On save it goes into **Windows Credential Manager**, encrypted at
  rest by DPAPI. It is never written to `config.yaml` or `.env`.
- **Daily reflection** — optionally register the 03:00 Task Scheduler job.

Click **Save**. To confirm it landed, open *Control Panel → Credential Manager
→ Windows Credentials* and look for **`my-daemon`**.

**5. Verify.** In a new Command Prompt, from the project folder:

```cmd
.venv\Scripts\activate.bat
daemon doctor
```

Every row should read `pass`. The `api key` row should say
*present via the OS credential store*. If it instead warns *present, but read
from a plaintext .env file*, see
[Where your API key is stored](#where-your-api-key-is-stored).

**6. Ingest and ask.**

```cmd
daemon ingest -v
daemon query "what was I thinking about last week"
```

Then double-click `launch-gui.vbs` for the chat window.

### Linux / macOS

```bash
python setup.py              # creates .venv, installs deps, copies templates
# or step by step:
uv sync                      # or: pip install -e .[dev]
cp config.example.yaml config.yaml
```

Point `vault.path` in `config.yaml` at your Obsidian vault, then run
`daemon setup` to store your API key in the Keychain (macOS) or Secret Service
(Linux). On a headless box with no keyring, `export ANTHROPIC_API_KEY=...` in
your shell profile instead.

## Where your API key is stored

The key is resolved from three places, **first hit wins**:

| # | Source | Encrypted at rest | Set it with |
|---|---|---|---|
| 1 | `ANTHROPIC_API_KEY` environment variable | no | `export` / `$env:` — for CI, containers, headless servers |
| 2 | **OS credential store** | **yes** | `daemon setup` — Windows Credential Manager, macOS Keychain, Linux Secret Service |
| 3 | `.env` beside `config.yaml` | **no — plaintext** | legacy only; supported so old installs keep working |

`daemon setup` writes to layer 2 **and nothing else**. If it finds a key in a
`.env` left over from an older install, it moves it into the credential store
and deletes the plaintext line, leaving your other `MY_DAEMON_*` variables
untouched.

The key is never written to `config.yaml`, so that file is safe to commit.
`.env` is gitignored. Nothing in the daemon ever prints the key — `daemon
doctor` reports only which layer supplied it.

!!! warning "If doctor warns about a plaintext .env"
    Run `daemon setup` and save. Then **rotate the key** at
    [console.anthropic.com](https://console.anthropic.com/settings/keys):
    it has been sitting readable on disk, and moving it does not un-expose it.

**Upgrading from a pre-credential-store install?** Nothing breaks — your `.env`
keeps working as layer 3. `daemon doctor` will warn until you run `daemon
setup` once.

### Qdrant: embedded or server

Nothing to start. `config.example.yaml` ships with **embedded** Qdrant — the
vector engine runs inside the daemon process and persists to
`./data/qdrant-local/`:

```yaml
vector_store:
  qdrant:
    path: ./data/qdrant-local
    collection: chunks
```

Embedded mode is single-process: one daemon may hold that folder at a time, so
you can't run the CLI and the GUI against it simultaneously. Switch to the
**server** when you want concurrent access or your vault has grown large enough
that filtered-search latency shows. To switch, comment out `path`, uncomment
`url` in `config.yaml`:

```bash
docker compose up -d
```

That boots Qdrant on `http://localhost:6333` with storage persisted to
`./data/qdrant/`. The two layouts are not interchangeable — re-run
`daemon ingest --full` after switching either way. Retrieval quality is
identical in both modes (hybrid dense+sparse RRF included).

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
├── config.yaml           # all knobs — no secrets, safe to commit
├── .env                  # optional MY_DAEMON_* overrides (gitignored)
│                         #   the API key lives in the OS credential store
├── data/                 # gitignored — everything the daemon owns
│   ├── qdrant-local/       # embedded vector store (default mode)
│   ├── qdrant/             # docker server's storage mount (server mode only)
│   ├── models/             # embedding model cache (offline after first pull)
│   ├── graph.gpickle       # NetworkX pickle
│   ├── manifest.json       # ingest state
│   └── feedback.db         # SQLite query + agent-state log
└── <your vault>/         # never touched, unless agent.enabled = true
```

## Troubleshooting

- **"ANTHROPIC_API_KEY is not set"** — run `daemon setup` and save; it stores
  the key in the OS credential store. On a headless machine with no keyring,
  `export ANTHROPIC_API_KEY=...` in your shell profile instead.
- **"present, but read from a plaintext .env file"** — a key left over from an
  older install. Run `daemon setup` to migrate and strip it, then rotate the
  key. See [Where your API key is stored](#where-your-api-key-is-stored).
- **`daemon setup` says "No usable OS credential store"** — expected over SSH
  or in a bare container on Linux, where there is no Secret Service session.
  Install `gnome-keyring`, or use the environment variable instead. This is
  reported rather than silently falling back to a plaintext file.
- **`launch-setup.bat` / `launch-gui.vbs` not found** — they are generated by
  `setup.bat`; run that first.
- **"Collection 'chunks' uses the old single-vector schema; dropping it"** —
  expected when upgrading to the hybrid retrieval feature. Re-run
  `daemon ingest --full` to repopulate.
- **First chat send is slow** — the embedding model and graph load lazily on
  the first query, then stay warm.
- **Opus 4.7 errors about `temperature`** — leave `llm.temperature: null` for
  reasoning models. Sonnet and Haiku accept a float.
