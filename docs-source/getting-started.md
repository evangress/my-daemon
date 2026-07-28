# Getting Started

There are two ways in. **Install on Windows 11** is one downloaded file and a
double-click, and is what you want unless you intend to read or change the
code. **Install from source** is the developer path, and the only path on Linux
and macOS today.

## Install on Windows 11

**1. Download the installer.** Save
[`install-my-daemon.bat`](https://github.com/evangress/my-daemon/raw/master/install-my-daemon.bat)
anywhere — your Downloads folder is fine. It is the only file you need; it
fetches everything else.

**2. Double-click it.**

Windows will show **"Windows protected your PC"**. That is SmartScreen
reporting that this file is not code-signed — a certificate costs money this
project does not spend — not that anything is wrong with it. Click
**More info → Run anyway**. If your browser blocked the download outright,
right-click the file → **Properties** → tick **Unblock** → OK.

The installer will:

- find Python, and offer to install it via `winget` if it is missing;
- print the exact URL it is downloading, before it downloads anything;
- unpack the app, install its dependencies (a few minutes the first time),
  add **My Daemon** to the Start Menu, and open the Setup window.

**3. In the Setup window,** pick your Obsidian vault folder and paste your
[Anthropic API key](https://console.anthropic.com/settings/keys). The key goes
into Windows Credential Manager, encrypted — never into a file. See
[Where your API key is stored](#where-your-api-key-is-stored).

**4. Ingest your notes.** Start Menu → **My Daemon** opens the chat window. To
run the first ingest, open **Command Prompt** and paste:

```cmd
"%LOCALAPPDATA%\my-daemon\app\.venv\Scripts\daemon.exe" ingest -v
```

### Where it puts things

```
%LOCALAPPDATA%\my-daemon\app\   the program itself, plus its virtualenv
%APPDATA%\my-daemon\            your config.yaml and data\ (index, models)
Start Menu\Programs\My Daemon\  the two shortcuts
```

Your notes are **never** copied or moved — the daemon reads the vault where it
already lives.

Two folders, split on purpose: updating replaces the program folder wholesale,
so everything you own — config, vault index, feedback history — lives in the
other one and survives. `%LOCALAPPDATA%` is the conventional home for a
per-user Windows app that shouldn't roam between machines, which is why the
program goes there rather than `C:\Program Files` (needs admin rights) or your
Documents (which OneDrive will try to sync, model cache and all).

### Updating and removing

**Update:** run `install-my-daemon.bat` again. It replaces the program and
leaves your config, index and vault untouched.

**Remove:** delete the folders above. My Daemon writes nothing else — no
registry keys, no services. Your vault is not one of them and is never touched.

## Install from source

For reading the code, changing it, or running on Linux or macOS.

**1. Get the code.** With git:

```bash
git clone https://github.com/evangress/my-daemon.git
cd my-daemon
```

Or without git: download the
[ZIP](https://github.com/evangress/my-daemon/archive/refs/heads/master.zip),
extract it, and `cd` into the extracted folder. On Windows, extract somewhere
you can write to — your home folder is fine, `C:\Program Files` is not.

**2. Bootstrap.**

```bash
python setup.py              # creates .venv, installs deps, copies templates
# or step by step:
uv sync                      # or: pip install -e .[dev]
cp config.example.yaml config.yaml
```

On Windows you can double-click `setup.bat` instead, which runs the same thing.
It also generates the launcher scripts (`launch-setup.bat`, `launch-gui.vbs`,
`launch-gui.bat`) — those are not in a fresh clone, so run it before looking
for them.

**3. Configure.** Point `vault.path` in `config.yaml` at your Obsidian vault,
then run `daemon setup` to store your API key in the OS credential store
(Credential Manager on Windows, Keychain on macOS, Secret Service on Linux). On
a headless box with no keyring, `export ANTHROPIC_API_KEY=...` in your shell
profile instead.

**4. Verify**, then ingest:

```bash
daemon doctor
daemon ingest -v
daemon query "what was I thinking about last week"
```

## Prerequisites

The Windows installer handles these for you; they matter for the from-source
path.

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

- **"Windows protected your PC" when running the installer** — SmartScreen
  flagging an unsigned file. **More info → Run anyway**. If the file seems
  inert when double-clicked, right-click → **Properties** → tick **Unblock**.
- **"Python is installed, but this window cannot see it yet"** — `winget`
  updates `PATH` for new processes only. Close the window and double-click
  `install-my-daemon.bat` again; it will pick Python up the second time.
- **"Could not download the installer"** — the `.bat` could not reach
  `raw.githubusercontent.com`. Check your connection, and any corporate proxy
  or filter that blocks GitHub.
- **"Downloaded archive does not match its published checksum"** — the
  installer refused to unpack and changed nothing. Run it again; if it repeats,
  stop and report it rather than working around it.
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
