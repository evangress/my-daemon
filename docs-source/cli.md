# CLI Reference

The `daemon` binary is a Typer app installed by the project. Every command
below resolves a `config.yaml`, merges environment overrides, and reads
`ANTHROPIC_API_KEY` from the environment / the `.env` beside that config.

Run `daemon --help` for the live listing.

## Global options

```
daemon [--config PATH] <command> ...
```

`--config` names the config file to use, ahead of every other candidate. It is
authoritative for the whole process, so `daemon --config … chat` and
`daemon --config … reflect` mean the same thing.

Without it the search order is `$MY_DAEMON_CONFIG` → `./config.yaml` →
the per-user config directory (`~/.config/my-daemon/config.yaml`, or
`%APPDATA%\my-daemon\config.yaml` on Windows). If none exists, any
state-touching command exits 1 and prints every location it looked in — it
never falls back to defaults. See
[Configuration](configuration.md#where-configyaml-comes-from).

`version`, `init` and `setup` do not need a config and work without one.

## Top-level

### `daemon init`

```
daemon init [--vault PATH] [--force] [--user]
```

Generates `config.yaml` (from `config.example.yaml`) and `.env` (from
`.env.example`). If `--vault` is omitted, prompts for the vault path
interactively. Refuses to overwrite an existing `config.yaml` without
`--force`.

Writes into the current directory by default — the repo-dev workflow. `--user`
writes into the per-user config directory instead (creating it), which is the
right choice for an installed daemon: state paths follow the config, so
`./data/...` then lives under `~/.config/my-daemon/data/`. Templates are still
read from the directory you invoke it from.

### `daemon setup`

Opens the Tkinter setup window. Lets you browse to your vault folder, paste
the Anthropic API key (`.env` always; on Windows additionally via `setx` so
the key is a true OS env var), and — on Windows — toggle a daily
`daemon reflect` job in Task Scheduler.

### `daemon chat`

```
daemon chat [--host 127.0.0.1] [--port 8765] [--native]
```

Launches the warm-themed NiceGUI chat window. Streams Claude's response as it
generates. The retrieval pipeline + feedback logging are identical to the CLI
`query` command. `--native` opens as a desktop window via pywebview (install
separately).

### `daemon version`

Print the installed package version.

## Ingest & query

### `daemon ingest`

```
daemon ingest [--full] [-v]
```

Walk the vault, embed new/changed notes, update the graph. Incremental by
default — the manifest at `./data/manifest.json` lets unchanged notes be
skipped.

Three cheap paths avoid re-embedding entirely, and the summary reports each:

- **renamed** — body identical, path moved. One Qdrant payload update; chunk
  ids derive from the note's UUID, so the points are already correct.
- **metadata-refreshed** — body identical, frontmatter changed (a tag added in
  Obsidian, or a theme tag accepted). Differential graph update plus a registry
  refresh, so learned edge weights survive.
- **skipped** — nothing changed at all. `--full` clears the graph and manifest and re-ingests everything;
necessary after switching `embeddings.hybrid` on or off.

### `daemon query`

```
daemon query <text> [--no-llm] [-v]
```

Ask the daemon a question. Prints the synthesized answer, then a ranked
candidates table (at least `retrieval.candidate_pool`, default 3). `--no-llm`
skips synthesis and shows ranked context only — useful while tuning retrieval.
`-v` adds seeds/expanded counts, latency, and the feedback row id.

### `daemon ask`

Alias for `daemon query`.

### `daemon search`

```
daemon search <text> [-k N]
```

Vector-only debug path: no graph expansion, no LLM. Useful for sanity-checking
the embedding model and seeing what the dense+sparse fusion is returning
before the expander touches it.

## Health

### `daemon doctor`

```
daemon doctor
```

Preflights every moving part and prints one row per check with a remediation
hint on anything that is not passing. Exits **1** if any hard check fails;
warnings alone exit 0.

The checks run in dependency order, so the first failure is usually the cause
of the ones below it:

| # | Check | Fails when | Warns when |
|---|---|---|---|
| 1 | **config** | no `config.yaml` in any searched location (the hint lists each one) | — |
| 2 | **vault** | `vault.path` is missing or is not a directory | it exists but holds no `.md` files |
| 3 | **vector store** | server mode: nothing answers at `qdrant.url`; embedded mode: the folder cannot be opened, or another process holds it | — |
| 4 | **collection** | the collection's dense dimension does not match the embedder's | the collection does not exist yet (run `daemon ingest`), or the store was unreachable |
| 5 | **api key** | — | `ANTHROPIC_API_KEY` is unset. Retrieval still works; synthesis (`query`/`ask`) and `extract`/`reflect`/`consolidate` do not |
| 6 | **model cache** | — | the configured embedding model is not in `embeddings.cache_folder` — the first ingest will download ~130MB |
| 7 | **state db** | the file is at a schema *newer* than this build | it is behind (run `daemon migrate db`) |
| 8 | **graph** | `graph.gpickle` exists but will not load (`GraphCorruptError`) | it does not exist yet |

Two things it deliberately does **not** do: it never loads the embedding model
(the expected dimension is read from the model's own `1_Pooling/config.json`
in the cache, and reported as *not verified* when the cache is cold), and it
never keeps the embedded store's exclusive folder lock after the check — so
`doctor` cannot break the command you run next.

Missing config is reported as a finding rather than an exit path, so
`daemon doctor` is the one command that still says something useful when
nothing is configured.

### Inline preflights

`query`, `ask`, `search` and `ingest` catch the vector-store connection
failure at the CLI boundary and print the same one-liner `doctor` would,
instead of an `[Errno 111]` traceback out of the middle of retrieval:

```
Cannot reach the vector store at http://localhost:6333 (…).
Is `docker compose up -d` running at http://localhost:6333? Start it, or switch
vector_store.qdrant to an embedded 'path' (no Docker needed).
Run `daemon doctor` for the full preflight.
```

Embedded mode gets the "already open in another process" message instead. The
translation lives in one place (`my_daemon.doctor.store_error_message`), so
every command says the same thing about the same condition.

## Inspection

### `daemon activations [query_id]`

With no argument, lists recent queries from the activation ledger. With a query
id, shows which notes fired for it, via which source (`vector_seed` /
`graph_expansion`), and at what strength.

### `daemon hot-notes [--days N] [--limit N]`

Which notes your attention actually lands on, ranked by how many queries have
activated them.


### `daemon status`

Prints the vault path, vector chunk count, graph note/tag/edge counts, and
the manifest location.

### `daemon graph stats`

Top notes by PageRank and top tags by degree. Reads from the gpickle; no
LLM or vector calls.

## Maintenance

### `daemon backup`

```
daemon backup [DEST_DIR] [--list]
daemon backups
```

Writes a timestamped bundle holding the state the vault cannot regenerate:

```
./backups/backup-2026-07-26T22-56-22Z/
├── feedback.db      ← learned weights, activation ledger, themes, feedback log
├── graph.gpickle    ← the graph, including every reinforced edge
├── manifest.json    ← the ingest manifest
├── config.yaml      ← a copy of the config that produced all of the above
└── metadata.json    ← bundle_version, created_at, my-daemon version, schema version, file sizes
```

`feedback.db` is copied with SQLite's online-backup API, so a backup taken
while the GUI is open still captures writes sitting in the `-wal`. Files that
do not exist yet are simply skipped and omitted from `metadata.json`.

**Vectors are excluded by design.** They are the one part of the state that
comes back for free (`daemon ingest --full`), and leaving them out is what
keeps a bundle small enough to take often.

The destination defaults to `backup.dir` (config key, default `./backups`,
anchored to the config file's directory like every other relative path). It is
deliberately *outside* `./data` — a backup inside the tree `daemon reset`
clears is not a backup. Snapshots (`daemon snapshot create`) are a different
thing: they freeze state *for analysis*, live under `./data`, and have no
restore path.

`daemon backup --list` (or `daemon backups`) enumerates bundles with their
creation time, schema version and size.

### `daemon restore`

```
daemon restore <BUNDLE_DIR> [--yes]
```

Puts a bundle's `feedback.db`, `graph.gpickle` and `manifest.json` back.
In order:

1. **Validates the bundle** — `metadata.json` must be present, well-formed,
   and of a `bundle_version` this build writes.
2. **Refuses a newer schema.** A bundle whose `schema_version` is above this
   build's is rejected outright; a database is never migrated downwards.
3. **Backs up what it is about to overwrite** into a sibling
   `pre-restore-<timestamp>/` folder, so an accidental restore is itself
   undoable.
4. **Replaces each file atomically** (temp file + `os.replace`) while holding
   the graph's inter-process lock, so a GUI endorse-click cannot save a graph
   on top of the one being restored. Stale `feedback.db-wal` / `-shm`
   sidecars are removed — the restored copy is already consistent, and
   applying the old WAL to it would corrupt it.

Prompts unless `--yes`. The config copy in the bundle is *not* restored — it
is there to read, not to overwrite live configuration with.

Vectors are not in the bundle, so finish with `daemon ingest --full` if the
vault has changed since the backup was taken.

### `daemon reset`

```
daemon reset [--yes] [--models] [--all]
```

Deletes the daemon's **rebuildable** state. Every target comes from the
resolved config, is listed with its size before the prompt, and is deleted by
name — nothing is swept up by removing a parent directory. **The vault is
never touched.**

Three tiers, by cost of loss:

| Flag | Also deletes | Why it is gated |
|---|---|---|
| *(default)* | graph, graph lock, ingest manifest, snapshots, consolidation reports, embedded Qdrant folder | all of it returns from `daemon ingest --full` |
| `--models` | `embeddings.cache_folder` | a ~130MB re-download, not a loss — but not free either |
| `--all` | `feedback.db` (+ `-wal`, `-shm`) | learned edge weights, the activation ledger and themes are **not** derivable from the vault. This is the only irreversible part of the command |

`backup.dir` is never a reset target.

**Server mode never has its storage folder deleted while Qdrant is up.** The
collection is dropped over HTTP instead (which resets the vectors just as
well), and the compose mount at `data/qdrant` is reported as refused:

```
Refusing to delete …/data/qdrant while Qdrant answers at http://localhost:6333
— stop the container first (`docker compose down`) if you want the folder gone.
Dropping the collection instead, which resets the vectors either way.
```

If the server is unreachable the folder becomes an ordinary listed target, and
the output says plainly that the collection was *not* dropped. In embedded
mode deleting the configured `qdrant.path` is the drop, and the store is never
opened (opening it would recreate the folder).

`--yes` skips the prompt, for scripts. `scripts/reset.py` is now a thin
wrapper around this command — same guards, same prompt — rather than its own
unguarded copy of the wipe.

### `daemon migrate db`

```
daemon migrate db
```

Brings the state database (`data/feedback.db`) up to the current schema
version, applying each pending migration in its own transaction. Idempotent —
re-running when there's nothing to do prints `Already at schema vN`.

You rarely need to run this by hand: every store migrates the file on
construction. It exists so an upgrade can be applied (and inspected)
deliberately, before the first command that would otherwise do it silently.

### `daemon migrate status`

```
daemon migrate status
```

Shows the database's current schema version against the target, and lists any
pending migrations by name. Works before the database has ever been created —
a missing file reports version 0 and is not created by the check.

### `daemon migrate backfill-activations`

```
daemon migrate backfill-activations [--apply] [--days N]
```

Recovers an activation ledger from the historical feedback log. Every query the
daemon ever answered already recorded its ranked candidates, which is a
proto-activation record — replaying it means fingerprint recall and themes start
with history instead of starting empty.

Dry-run by default, and idempotent: re-running writes nothing new.

Honest limitation, reported rather than hidden: a query naming notes that have
since been renamed or deleted yields a *partial* fingerprint, which looks less
similar to everything than it should. Above a 20% drop rate the command says so.

### `daemon migrate assign-uuids`

```
daemon migrate assign-uuids                       # dry run — prints a table, writes nothing
daemon migrate assign-uuids --apply
    [--path-glob 'Projects/**']                   # scope the run
    [--limit 200]
    [--grace-minutes 2]
    [--no-backup]
```

Stamps a stable `uuid:` into every note's frontmatter. That UUID becomes the
shared key across SQLite (identity), Qdrant (semantics), and the graph
(relations) — see [PLAN-MEMORY.md](https://github.com/evangress/my-daemon/blob/master/PLAN-MEMORY.md).

**Exactly one line per file changes.** Comments, key order, quoting style,
flow-style lists, dates, and line endings are all preserved byte-for-byte.

Per note, in order:

1. **Already carries our `uuid:`** → adopted, nothing written.
2. **Carries a foreign id key** (`uid`, `id`, `guid`, `note-id`,
   `permanent_id`) → if it holds a real UUID it is adopted verbatim; if it
   holds an opaque id (a Zettelkasten timestamp, say) a UUID is *derived*
   from it deterministically. Either way our `uuid:` is added and **their key
   is never touched**.
3. **Nothing usable** → a fresh `uuid4` is minted.
4. **Collision** — the same UUID resolved for two different notes, which is
   what Obsidian's "Make a copy" and sync conflicts produce — the second note
   is re-minted and flagged in the report.
5. **Unwritable** (`daemon: ignore`, inside the grace window, read-only) → the
   note gets a deterministic *path-derived* identity so it still participates
   in retrieval, recorded as `uuid_source='derived_path'`. **This identity is
   not stable across renames.** The report and `daemon status` both count these
   so the degradation stays visible.

Scope follows `vault.exclude_dirs`, so by default the `Agent/` folder is skipped
— stamping notes that ingest never sees would be pointless. The migration's own
backup directory is always skipped regardless of config.

Each `--apply` run writes a batch backup to
`<vault>/Agent/backups/migrations/<run_id>/`, holding `manifest.jsonl` (one
line per file, with before/after SHA-256) and byte-for-byte copies under
`files/`.

### `daemon migrate list-runs`

Lists `assign-uuids` runs available to roll back.

### `daemon migrate rollback-uuids`

```
daemon migrate rollback-uuids <run_id> [--mode key-removal|restore] [--force]
```

- **`key-removal`** (default) deletes the one `uuid:` line the migration added,
  and the frontmatter fences too if that empties a block the migration created.
  It never restores a body, so it stays safe on files you have edited since.
- **`restore`** copies the original bytes back. It **refuses** any file whose
  content changed after the migration unless `--force`, because restoring would
  discard those edits.

Either mode also clears the affected registry rows, returning the system to
path-only operation.

### `daemon themes list | accept | reject | review`

```
daemon themes list
daemon themes accept <id> [--label "Why projects stall"]
daemon themes reject <id>
daemon themes review [--accept-all | --reject-all]
```

Themes are clusters of your own questions that kept landing on the same notes,
found and named during `daemon consolidate`. `list` shows them; `accept` blesses
one (locking its label, so the observer never renames what you named) and queues
tag proposals for the notes that define it; `review` walks those proposals one
note at a time.

Nothing is written to your notes until you accept a proposal. Accepted tags
carry a `theme/` prefix so you can always tell which tags you wrote and which
the daemon proposed — and so retrieval can refuse to walk them, which stops the
daemon from converging on its own conclusions.

### `daemon models download`

Forces a download of the embedding model(s) into `embeddings.cache_folder`
(default `./data/models/`). The dense model always; the sparse model too when
`embeddings.hybrid: true`. After this, queries run fully offline (no HF Hub
calls) until the cache is cleared.

## Background agents

All three writeback jobs refuse to write until `agent.enabled: true` in
`config.yaml`. They accept `--dry-run` to preview at any time.

### `daemon extract`

```
daemon extract [--all] [--note PATH] [--dry-run] [-v]
```

Append an `## Agent Notes` section (summary, key points, themes, feelings,
open questions) to recently-changed notes — between
`<!-- daemon:start --> ... <!-- daemon:end -->` sentinels so re-runs are
idempotent. `--all` overrides the "changed since last run" filter;
`--note <relative-path>` restricts to a single note.

### `daemon link`

```
daemon link [--note PATH] [--dry-run] [--no-llm] [-v]
```

Walk the vault, propose wikilinks and tags. **Auto-applies** wikilinks at
cosine ≥ `agent.link_apply_cosine` (default 0.85) *and* the target title
appears verbatim in the source body. **Auto-applies** tags carried by ≥
`agent.tag_apply_min_neighbor_count` graph-nearby notes (default 4).
Borderline candidates are written to
`<vault>/Agent/link-suggestions-YYYY-MM-DD.md` for you to review. `--no-llm`
skips the LLM second-opinion.

### `daemon reflect`

```
daemon reflect [--theme NAME] [--dry-run] [-v]
```

Update themed memory files in `<vault>/Agent/memory-<theme>.md` (default
themes: personality, projects, relationships, themes). Pulls recent notes
(last `agent.reflect_lookback_days`) and recent chats from the feedback log,
sends them to Claude with the prior memory as context, and rewrites the
file atomically with a snapshot beforehand. Also appends a one-block-per-run
summary to `<vault>/Agent/memory-rolling.md`.

## Running unattended

### `daemon run`

```
daemon run [--once]
```

The foreground supervisor — the command that turns the daemon from a tool you
invoke into a process that keeps up with you. In order:

1. **Preflight.** A doctor-lite check of the two things that make an unattended
   run pointless: the vault exists, and the vector store opens. Either failure
   refuses with its remediation hint and exits 1. (A missing API key or a cold
   model cache do *not* refuse — the supervisor recovers from those on its own,
   and a daemon that won't start over a warning is a daemon nobody enables.)
2. **One incremental ingest**, exactly as `daemon ingest` would.
3. **Watch.** Every `.md` change under `vault.path` — respecting
   `vault.exclude_dirs`, and always ignoring `<vault>/<agent.folder_name>/` so
   the daemon's own writeback can't trigger a re-ingest storm — restarts a
   `run.debounce_seconds` quiet timer. One ingest per burst.
4. **Consolidate nightly** at `run.consolidate_at`, when both `agent.enabled`
   and `agent.observer_enabled` are open. When they aren't, the reason is
   logged once, not every night.
5. **Heartbeat** every `run.heartbeat_minutes`, so an idle daemon still proves
   it is alive.

Nothing forks or detaches — that is `daemon schedule`'s job, and both systemd
(`Type=simple`) and Task Scheduler want a foreground process. `SIGINT`/`SIGTERM`
finish the work in flight, close the vector store, release the graph lock, and
exit 0.

`--once` does the preflight and the initial ingest, then exits. That is the
cron-friendly shape: no watcher, no scheduler, no long-lived lock.

**In embedded vector-store mode** (`vector_store.qdrant.path`, the default),
`daemon run` holds the storage folder exclusively for its whole life. The
startup banner says so. Every other my-daemon process — `daemon query`,
`daemon chat`, the GUI, Hermes — will refuse to open it until `run` stops. If
you want to query while the supervisor runs, switch to server mode
(`vector_store.qdrant.url` + `docker compose up -d qdrant`).

### `daemon schedule show`

Print the scheduler unit for this platform, substituted with the real
interpreter-adjacent `daemon` path and the resolved `--config`. Writes nothing.

### `daemon schedule install`

```
daemon schedule install [--dry-run]
```

**Linux** — writes a `systemd --user` service to
`$XDG_CONFIG_HOME/systemd/user/my-daemon.service` (default
`~/.config/systemd/user/`) and prints the commands that activate it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now my-daemon.service
journalctl --user -u my-daemon.service -f    # follow the log
loginctl enable-linger $USER                 # keep it running after you log out
```

**Windows** — there is no file to write, so `install` prints the exact
`schtasks /Create /SC ONLOGON` line to paste into a Command Prompt. It uses the
same quoting shape as the `daemon setup` window's reflect task; a test pins the
two together so they cannot drift.

`--dry-run` prints and writes nothing. Neither `systemctl` nor `schtasks` is
ever run for you: enabling a unit that will outlive your shell — and hold your
vault's embedded vector store — is a decision to make with your eyes open.

Other platforms exit 1 with the `daemon run` command line to wire up by hand.

## Environment variable overrides

Any config key can be overridden by an env var with the `MY_DAEMON_` prefix
and double-underscore nesting:

```bash
export MY_DAEMON_LLM__MODEL=claude-opus-4-7
export MY_DAEMON_GRAPH__EXPANSION_DEPTH=3
export MY_DAEMON_EMBEDDINGS__HYBRID=false
```

`MY_DAEMON_CONFIG=/path/to/other.yaml` chooses an alternate config file — the
env-var equivalent of `--config`, useful in a crontab line.

## Exit codes

- `0` — success.
- `1` — refused. Includes a missing `config.yaml` (the message lists every
  location searched), `agent.enabled` false, a declined destructive prompt, a
  failing `daemon doctor` check, an unreachable vector store caught by an
  inline preflight, and a `daemon restore` bundle that fails validation.
- `2` — a preflight failed (e.g. `daemon chat --native` with pywebview
  missing).
- Other non-zero — unexpected exception; see the stack trace.
