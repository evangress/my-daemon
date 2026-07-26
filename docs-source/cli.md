# CLI Reference

The `daemon` binary is a Typer app installed by the project. Every command
below loads `config.yaml` from the current working directory (or
`$MY_DAEMON_CONFIG`), merges environment overrides, and reads
`ANTHROPIC_API_KEY` from the environment / `.env`.

Run `daemon --help` for the live listing.

## Top-level

### `daemon init`

```
daemon init [--vault PATH] [--force]
```

Generates `config.yaml` (from `config.example.yaml`) and `.env` (from
`.env.example`). If `--vault` is omitted, prompts for the vault path
interactively. Refuses to overwrite an existing `config.yaml` without
`--force`.

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

### `daemon reset`

```
daemon reset [--yes]
```

Deletes the entire `./data/` directory (Qdrant storage, models cache, graph
pickle, manifest, feedback DB). Prompts for confirmation unless `--yes`.
**Never touches the vault itself.**

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

## Environment variable overrides

Any config key can be overridden by an env var with the `MY_DAEMON_` prefix
and double-underscore nesting:

```bash
export MY_DAEMON_LLM__MODEL=claude-opus-4-7
export MY_DAEMON_GRAPH__EXPANSION_DEPTH=3
export MY_DAEMON_EMBEDDINGS__HYBRID=false
```

`MY_DAEMON_CONFIG=/path/to/other.yaml` chooses an alternate config file.

## Exit codes

- `0` — success.
- `1` — refused (missing `config.yaml`, `agent.enabled` false, user declined
  a destructive prompt).
- Other non-zero — unexpected exception; see the stack trace.
