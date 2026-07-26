# Configuration

Every knob the daemon exposes lives in `config.yaml`. Defaults come from
`src/my_daemon/config.py`; values in `config.yaml` override defaults; env
vars (`MY_DAEMON_<SECTION>__<KEY>`) fill in anything the YAML leaves unset.

The Anthropic API key is **never** read from `config.yaml`. It comes from the
environment (or the `.env` beside the config, auto-loaded on startup).

## Where `config.yaml` comes from

The first of these that exists wins:

| # | Location | For |
|---|---|---|
| 1 | `daemon --config PATH` | One-off, and schedulers |
| 2 | `$MY_DAEMON_CONFIG` | The same, from a crontab or a service unit |
| 3 | `./config.yaml` | Working in a checkout — unchanged from day one |
| 4 | `~/.config/my-daemon/config.yaml`<br>(`$XDG_CONFIG_HOME` honored; `%APPDATA%\my-daemon\config.yaml` on Windows) | An installed daemon. Create it with `daemon init --user` |

Naming a file (1 or 2) is *exclusive*: if that file does not exist the daemon
stops rather than quietly resolving to a different one, because a typo that
lands you on someone else's config is worse than a typo that fails.

**If none of them exists, the daemon refuses.** Every state-touching command
exits 1 and prints the list above with real paths filled in. It does not fall
back to built-in defaults — those describe a vault nobody owns, and a
scheduled job running against them would look like it was working.
`daemon version`, `daemon init` and `daemon setup` need no config.

## Relative paths follow the config, not your shell

Every relative path in a config — `graph.path`, `graph.manifest_path`,
`feedback.db_path`, `snapshot.dir`, `consolidation.out_dir`,
`embeddings.cache_folder`, `vector_store.qdrant.path`, `vault.path` — is
resolved against **the directory the config file is in**, at load time.

A config at `/home/evan/dev/my-daemon/config.yaml` that says
`./data/graph.gpickle` always means `/home/evan/dev/my-daemon/data/graph.gpickle`
— from that directory, from `$HOME` under cron, from `system32` under Windows
Task Scheduler. Your one daemon stays one daemon no matter where you launch it.

- Absolute paths pass through untouched.
- `~` expands to your home directory (it is not treated as a relative path).
- `:memory:` for `vector_store.qdrant.path` is a mode, not a path, and is left
  alone.
- Paths supplied via `MY_DAEMON_*` env vars are anchored the same way.
- The `.env` read for `ANTHROPIC_API_KEY` is the one beside the config (the
  copy in your current directory is still honored as a fallback).

`daemon init --user` therefore puts state under `~/.config/my-daemon/data/`.
If you want it somewhere else, give those keys absolute paths.

## Section by section

### `vault`

```yaml
vault:
  path: ~/Documents/Obsidian/MyVault
  exclude_dirs: [".obsidian", ".trash", "templates", "Agent"]
```

| Key | Default | Notes |
|---|---|---|
| `path` | `~/Documents/Obsidian/MyVault` | Vault root. Tilde-expanded at load. |
| `exclude_dirs` | `.obsidian, .trash, templates, Agent` | Case-insensitive directory names to skip during walk. `Agent` is the daemon's own folder — keep it excluded or your reflection memory will start ingesting itself. |

### `chunking`

```yaml
chunking:
  max_tokens: 512
  overlap_tokens: 50
  split_on_headers: ["#", "##", "###"]
```

| Key | Default | Notes |
|---|---|---|
| `max_tokens` | `512` | Approximate token budget per chunk (heuristic = chars/4). Oversized header sections are secondary-split. |
| `overlap_tokens` | `50` | Tail/head overlap when secondary-splitting. |
| `split_on_headers` | H1/H2/H3 | Primary split level. Lower headings stay inside their parent chunk. |

### `embeddings`

```yaml
embeddings:
  model: BAAI/bge-small-en-v1.5
  batch_size: 32
  device: auto         # auto | cpu | cuda | mps
  cache_folder: ./data/models
  hybrid: true
  sparse_model: Qdrant/bm42-all-minilm-l6-v2-attentions
```

| Key | Default | Notes |
|---|---|---|
| `model` | `BAAI/bge-small-en-v1.5` | 384-dim dense encoder. Fast on CPU. |
| `batch_size` | `32` | Encode batch size. Raise if you have GPU memory. |
| `device` | `auto` | `auto` lets sentence-transformers pick; override to `cpu`/`cuda`/`mps`. |
| `cache_folder` | `./data/models` | Model cache. First run populates; later runs stay offline. |
| `hybrid` | `true` | If true, also store a sparse vector and fuse with dense via Qdrant RRF on query. |
| `sparse_model` | `Qdrant/bm42-all-minilm-l6-v2-attentions` | fastembed BM42. Toggling `hybrid` requires `daemon ingest --full`. |

### `vector_store`

Qdrant runs in one of two modes, and the config picks which.

**Embedded (default — no Docker).** Qdrant runs in-process against a local
folder:

```yaml
vector_store:
  backend: qdrant
  qdrant:
    path: ./data/qdrant-local
    collection: chunks
```

**Server.** Point at a running Qdrant (`docker compose up -d qdrant`):

```yaml
vector_store:
  backend: qdrant
  qdrant:
    url: http://localhost:6333
    collection: chunks
```

| Key | Default | Notes |
|---|---|---|
| `backend` | `qdrant` | Only backend supported. |
| `qdrant.path` | unset | Set it to use **embedded** mode: a directory (persisted across restarts), or the literal `:memory:` for a throwaway store. |
| `qdrant.url` | `http://localhost:6333` | **Server** mode endpoint. The Docker compose service binds here. Only used when `path` is unset. |
| `qdrant.collection` | `chunks` | Collection name. |

Setting **both** `path` and `url` is a validation error — the daemon will not
guess which store your vectors should land in.

**Which to use.** Embedded needs no Docker and is the right default for a
personal vault, but it is *single-process*: exactly one daemon may hold the
folder at a time (a second one fails with a clear "already open in another
process" error rather than corrupting anything), and it filters payloads in
Python instead of using real indexes. Move to the server when you want the GUI,
CLI and Hermes open at once, or when the vault is large enough that filtered
search latency shows.

Retrieval behaves identically in both modes, including hybrid dense+sparse RRF
fusion — the embedded engine emulates the same Query API server-side path.

The two on-disk layouts are **not** interchangeable: switching modes means
re-running `daemon ingest --full`. `./data/qdrant/` is the docker server's
storage mount; embedded mode deliberately uses a different folder so they can
coexist.

### `graph`

```yaml
graph:
  path: ./data/graph.gpickle
  manifest_path: ./data/manifest.json
  expansion_depth: 2
  distance_decay: 0.5
```

| Key | Default | Notes |
|---|---|---|
| `path` | `./data/graph.gpickle` | Pickle of the `MultiDiGraph`. |
| `manifest_path` | `./data/manifest.json` | Incremental-ingest tracker. |
| `expansion_depth` | `2` | BFS depth from each seed. Sibling notes via a shared tag sit at distance 2. |
| `distance_decay` | `0.5` | `combined = vector_score * decay**distance` for expanded chunks. |

### `retrieval`

```yaml
retrieval:
  seed_top_k: 8
  context_token_budget: 6000
  candidate_pool: 3
```

| Key | Default | Notes |
|---|---|---|
| `seed_top_k` | `8` | Vector hits used as seeds. |
| `context_token_budget` | `6000` | Cap on combined chunk text shipped to Claude. |
| `candidate_pool` | `3` | Minimum candidates surfaced regardless of token budget — project policy is to always show at least N so the user picks the winner. |

### `llm`

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_tokens: 2048
  temperature: null
  batch_model: claude-haiku-4-5
```

| Key | Default | Notes |
|---|---|---|
| `provider` | `anthropic` | Only provider supported. |
| `model` | `claude-sonnet-4-6` | Synthesis model for chat / `daemon query`. |
| `max_tokens` | `2048` | Cap on synthesized response length. |
| `temperature` | `null` | Leave null for reasoning-capable models (e.g. Opus 4.7) which reject `temperature`. Float (e.g. 0.3) for Sonnet/Haiku. |
| `batch_model` | `claude-haiku-4-5` | Cheaper model used by the three background-agent jobs (~15× cheaper than Opus). Falls back to `model` when null. |

### `agent`

```yaml
agent:
  enabled: false
  folder_name: Agent
  link_apply_cosine: 0.85
  link_apply_requires_title_substring: true
  link_suggest_cosine: 0.78
  link_max_per_note: 5
  tag_apply_min_neighbor_count: 4
  extract_min_word_count: 80
  extract_skip_if_processed_within_hours: 18
  reflect_themes: [personality, projects, relationships, themes]
  reflect_lookback_days: 7
  write_grace_minutes: 30
```

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Master switch. Every writeback command refuses to write unless this is true. `--dry-run` works regardless. |
| `folder_name` | `Agent` | The daemon's own folder under the vault — backups, suggestion files, memory files all live here. The writer refuses to touch anything inside it. |
| `link_apply_cosine` | `0.85` | Auto-apply threshold for the linker. |
| `link_apply_requires_title_substring` | `true` | Additional gate: the target title must appear verbatim (case-insensitive) in the source body. |
| `link_suggest_cosine` | `0.78` | Between this and `link_apply_cosine`, candidates land in `Agent/link-suggestions-YYYY-MM-DD.md`. |
| `link_max_per_note` | `5` | Cap on auto-applied wikilinks per note per run. |
| `tag_apply_min_neighbor_count` | `4` | Tag is auto-applied if ≥ N graph neighbors carry it and the source doesn't. |
| `extract_min_word_count` | `80` | Don't extract observations from stubs. |
| `extract_skip_if_processed_within_hours` | `18` | Currently advisory — the extractor primarily uses mtime-based dedup. |
| `reflect_themes` | personality, projects, relationships, themes | One `memory-<theme>.md` file per theme. |
| `reflect_lookback_days` | `7` | Window of "recent" notes for the reflection prompt. |
| `write_grace_minutes` | `30` | The writer skips any file modified within this many minutes (avoids collisions with a save in progress). |

### `feedback`

```yaml
feedback:
  db_path: ./data/feedback.db
```

| Key | Default | Notes |
|---|---|---|
| `db_path` | `./data/feedback.db` | SQLite file. Holds both the `feedback` table (every query) and the three `agent_*_runs` state tables. One file to back up. |

### `logging`

```yaml
logging:
  level: INFO
```

Standard Python log level. The CLI's verbose flags layer on top of this.

## Switching models

Change models without re-editing the YAML:

```bash
# A one-off Opus query with a roomier budget
MY_DAEMON_LLM__MODEL=claude-opus-4-7 \
MY_DAEMON_LLM__MAX_TOKENS=4096 \
  daemon query "draw a long thread through the last six months of journaling"

# Cheap, dense-only retrieval for a single run
MY_DAEMON_EMBEDDINGS__HYBRID=false daemon search "exact phrase here"
```

The `MY_DAEMON_` env-var override lane is type-checked the same way the YAML
is, so a typo will fail at startup rather than silently use the wrong value.

## Validation

`load_settings()` runs all values through Pydantic. If a field is invalid
(wrong type, unsupported literal, missing required key), the CLI exits with a
red-formatted error before any side-effect.
