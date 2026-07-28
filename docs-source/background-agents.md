# Background Agents

The three writeback jobs that let the daemon **shape** the vault, not just
read from it. All gated behind `agent.enabled: true` in `config.yaml`.

## The trio at a glance

| Job | What it writes | Where | When to run |
|---|---|---|---|
| `daemon extract` | `## Agent Notes` section per note (summary, key points, themes, feelings, open questions) | Inside the note itself, between `<!-- daemon:start --> ... <!-- daemon:end -->` sentinels | Daily or after a burst of journaling |
| `daemon link` | Wikilinks (auto-applied at strict thresholds), tags (auto-applied at graph-density thresholds), suggestions file for review | Inside each source note; lower-confidence to `Agent/link-suggestions-YYYY-MM-DD.md` | Weekly, or after a big ingest |
| `daemon reflect` | Themed memory files distilling who the user is | `<vault>/Agent/memory-<theme>.md` + a rolling journal | Nightly (`schtasks` checkbox on Windows; `cron` on Linux/macOS) |

## The safety discipline — built once, applied everywhere

Every write goes through `vault/writer.py`, which enforces five gates in
order. The first one to fail aborts the write with a reason that surfaces in
the stats table.

1. **Containment check.** The resolved path must live inside `vault.path`.
   Defends against a malicious `[[../../etc/passwd]]` target.
2. **Agent-folder guard.** Nothing inside `<vault>/<agent.folder_name>/` (the
   daemon's own folder) may be modified by an agent job. The reflection
   writer is the only thing that touches it, via direct file IO.
3. **Frontmatter opt-out.** A note with `daemon: ignore` in its frontmatter is
   invisible to every job.
4. **Grace period.** Files modified within `agent.write_grace_minutes` (default
   30) are skipped — you're probably still typing.
5. **Snapshot backup.** Before any successful write, the original is copied to
   `<vault>/<agent.folder_name>/backups/<rel>.<unix_ts>.md`. Add
   `Agent/backups/` to your vault's `.gitignore` if you keep the vault in git.

Writes themselves are atomic: a tempfile + `os.replace()` dance, so a crash
mid-write can never leave a half-written file.

## `daemon extract`

### Selection

A note is eligible if:

- Its word count ≥ `agent.extract_min_word_count` (default 80).
- Its mtime is newer than the last recorded extract run (tracked in the
  `agent_extract_runs` table inside `feedback.db`).
- It passes all five writer gates.

Pass `--all` to override the mtime filter; pass `--note <relative-path>` to
restrict to a single note.

### Output

The LLM (`llm.batch_model`, default Haiku 4.5) returns a structured JSON
payload — summary, key points, themes, feelings, topics, open questions —
which gets rendered into the markdown block that lives between the sentinel
markers. The block always carries an "Last updated YYYY-MM-DD by your daemon
(model: …)" line for transparency.

Re-running extract on an unchanged note is a no-op: the writer sees the
section is already correct and returns `changed=False`.

## `daemon link`

### How candidates are scored

For each source note:

1. The note body (first 6000 chars) is embedded.
2. Qdrant returns the top ~40 chunks. Best chunk score per *target note* is kept.
3. Two thresholds split the ranked list:
   - `cosine ≥ link_apply_cosine` (default 0.85) **AND** the target title
     appears verbatim (case-insensitive, word-boundary) in the source body →
     auto-apply.
   - `cosine ≥ link_suggest_cosine` (default 0.78) and below the apply gate
     → "suggest only," written to the daily suggestions file.
4. Optionally (`llm` argument), an LLM second-opinion proposes anchor text
   and a confidence score. This annotates the suggestions file; it does *not*
   bypass the apply gate.

### Tags

Tags use a different signal: graph density. For each tag carried by ≥
`agent.tag_apply_min_neighbor_count` (default 4) graph-nearby notes (depth 2,
both wikilinks and shared tags) but missing from the source, the linker
appends it to the source's frontmatter `tags:` block. Always frontmatter,
never inline `#tag` — frontmatter is a single safe surface to mutate.

### Writeback

The linker uses `insert_wikilinks()` (first-match-only, skips code fences and
existing wikilinks) for links and `add_tags()` for tags. Each note gets at
most one wikilink write and one tag write per run.

### Suggestions file

When borderline candidates exist, the linker writes (or rewrites)
`<vault>/Agent/link-suggestions-YYYY-MM-DD.md`. One H3 block per source note,
listing candidate `[[Target]]` lines with cosine and optional LLM reason.
You accept by editing the source note directly — the linker won't
auto-apply anything in that file.

## `daemon reflect`

### What "reflection" means

For each configured theme (default: personality, projects, relationships,
themes), the daemon maintains one memory file at
`<vault>/<agent.folder_name>/memory-<theme>.md`. Each run:

1. Pulls notes modified in the last `agent.reflect_lookback_days` days
   (default 7).
2. Pulls the last ~20 chat events from the feedback DB.
3. Sends them to the LLM along with the prior memory body. The system prompt
   instructs Claude to **preserve any user edits as ground truth** and to
   note explicit uncertainty when evidence is thin.
4. Writes the new body atomically, with the original snapshotted first. A
   YAML frontmatter block records `theme`, `updated`, `model`, and source
   counts so a future ingest can reason about provenance.
5. Appends a one-block summary to `<vault>/<agent.folder_name>/memory-rolling.md`.

If the source signature (note mtimes + chat ids) hasn't changed since the
last run, the theme is skipped — re-running an idempotent run does nothing.

### Editing the memory

The memory files are designed to be edited by you. The system prompt for the
reflection LLM treats your edits as ground truth, so correcting a hallucination
or sharpening a sentence will stick on the next run. Delete the file entirely
to start a theme over.

## Running as a service

`daemon run` is the supervisor: one long-lived foreground process that watches
the vault and runs the nightly consolidation, so you stop having to remember
`daemon ingest` after every editing session.

```bash
daemon run              # watch + nightly consolidate, until Ctrl-C
daemon run --once       # preflight + one incremental ingest, then exit
```

### What it does, in order

| Stage | Behaviour |
|---|---|
| Preflight | Vault exists, vector store opens. Either failure refuses with the `daemon doctor` hint and exits 1. A missing API key or cold model cache do **not** refuse — those recover on their own. |
| Initial ingest | One incremental pass, identical to `daemon ingest`. This one is allowed to kill the process: a supervisor that can't ingest even once is not something to leave running for a week. |
| Watch | Every `.md` change under `vault.path` restarts a `run.debounce_seconds` quiet timer (default 5s). One ingest per burst — a sync client rewriting a hundred notes gets one ingest *after* it settles, not one in the middle. |
| Nightly consolidate | At `run.consolidate_at` (default 03:00 local), gated on `agent.enabled` **and** `agent.observer_enabled`. |
| Heartbeat | An "alive" line every `run.heartbeat_minutes` (default 15), with run counts and the next consolidation time. |
| Shutdown | `SIGINT`/`SIGTERM` finish the work in flight, close the vector store, release the graph lock, exit 0. |

Once the loop is up, a failed ingest or a failed consolidation is logged and
the daemon keeps going — the opposite of the startup rule, and deliberately so.
A dead *watcher* is the one exception: it stops the process rather than
heartbeat "alive" while silently no longer watching anything, which is exactly
what `Restart=on-failure` in the generated unit is there to catch.

### The Agent folder does not feed itself

The watcher ignores `<vault>/<agent.folder_name>/` unconditionally — not merely
because `Agent` is in the default `exclude_dirs`, but because it is the
daemon's own writeback target. A nightly `consolidate` writing
`Agent/observer-2026-07-26.md` must not, at 03:00, schedule an ingest to
discover its own letter.

### Embedded Qdrant is single-process — say it out loud

With `vector_store.qdrant.path` set (the default), `daemon run` holds the
storage folder exclusively for its entire life, and the startup banner says so:

```text
embedded vector store at ./data/qdrant-local — this process holds it
exclusively. Every other my-daemon process (daemon query, daemon chat, the
GUI, Hermes) will refuse to open it until `daemon run` stops.
```

That includes the GUI: it is a separate process and hits the same lock. There
are only two honest options — stop `daemon run` when you want to query
interactively, or move to server mode (`vector_store.qdrant.url` +
`docker compose up -d qdrant`), which exists precisely for concurrent access.
The daemon does not attempt to broker multi-process access to an embedded
store; that is the engine's constraint, not something a supervisor can paper
over.

### Generated units

```bash
daemon schedule show                  # print the unit, write nothing
daemon schedule install               # write it, print the activation commands
daemon schedule install --dry-run     # print what install would write
```

Both shapes name the interpreter-adjacent `daemon` binary and the resolved
config absolutely, so neither depends on a working directory.

**Linux** — `~/.config/systemd/user/my-daemon.service`:

```ini
[Unit]
Description=My Daemon — vault watcher and nightly consolidation
After=default.target

[Service]
Type=simple
ExecStart="/path/to/my-daemon/.venv/bin/daemon" --config "/path/to/my-daemon/config.yaml" run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

then

```bash
systemctl --user daemon-reload
systemctl --user enable --now my-daemon.service
journalctl --user -u my-daemon.service -f    # follow the log
loginctl enable-linger $USER                 # keep it running after you log out
```

There is deliberately **no companion `.timer`**: the supervisor owns its own
schedule, and a timer would either double-fire the consolidation or try to
restart a process that never stopped.

**Windows** — nothing to write, so `install` prints the command:

```text
schtasks /Create /SC ONLOGON /TN "MyDaemonRun" /TR "\"C:\my-daemon\.venv\Scripts\daemon.exe\" --config \"C:\my-daemon\config.yaml\" run" /F
```

Same quoting shape as the `daemon setup` window's `MyDaemonReflect` task, and a
test pins the two together so they cannot drift.

Neither `systemctl` nor `schtasks` is ever run for you. Enabling a unit that
will outlive your shell — and hold your vault's embedded vector store — is a
decision to make with your eyes open.

## Scheduling

`daemon run` covers the common case. The cron recipes below remain the right
answer when you want the jobs to run *without* a resident process — on a
machine you rarely log into, or alongside a GUI that needs the embedded store.
`daemon run --once` is the cron-friendly shape of the supervisor: preflight
plus one incremental ingest, no watcher, no long-lived lock.

Every scheduler starts the job in a directory you did not choose — cron in
`$HOME`, Windows Task Scheduler in `system32`. A daemon that resolved
`config.yaml` and `./data/...` against that directory would read an empty,
phantom daemon and report success. So **name the config explicitly, or `cd`
first.** Either is enough: relative paths inside a config are resolved against
*that config's directory*, never the working directory. See
[Configuration](configuration.md) for the full search order.

### Windows

The `daemon setup` window has a checkbox: *"Run `daemon reflect` daily at
03:00 (Windows Task Scheduler)."* Saving the form runs `schtasks /Create`
under the hood. The task is named `MyDaemonReflect`; unchecking it on a
subsequent save removes the task.

The registered command names the config absolutely, because `schtasks` has no
working-directory switch:

```text
"C:\path\to\my-daemon\.venv\Scripts\daemon.exe" --config "C:\path\to\my-daemon\config.yaml" reflect
```

If you register a task by hand, use the same shape.

### Linux / macOS

```cron
# crontab -e
 0 3 * * *  cd /path/to/my-daemon && .venv/bin/daemon reflect
15 3 * * *  cd /path/to/my-daemon && .venv/bin/daemon extract
# Keep the index current without a resident process:
*/30 * * * *  cd /path/to/my-daemon && .venv/bin/daemon run --once
```

The 15-minute gap exists because `reflect` reads recent notes; running
`extract` afterward lets the next reflection see the new `## Agent Notes`
sections.

The `cd` is not decoration — without it cron runs in `$HOME`, finds no
`./config.yaml`, and the job exits 1 with a message naming every location it
searched. If you would rather not depend on the working directory at all, name
the config instead:

```cron
0 3 * * *  MY_DAEMON_CONFIG=/path/to/my-daemon/config.yaml /path/to/my-daemon/.venv/bin/daemon reflect
```

or equivalently `daemon --config /path/to/my-daemon/config.yaml reflect`.

Either form also puts the `.env` beside that config back in scope — cron
inherits almost no environment.

!!! warning "Scheduled jobs and the OS credential store"
    `daemon setup` stores `ANTHROPIC_API_KEY` in the OS credential store, and a
    scheduled job cannot always reach it:

    - **Linux cron** has no session D-Bus, so the Secret Service is usually
      locked or absent. `resolve_api_key` degrades quietly to the next layer,
      so give cron the key explicitly — either `ANTHROPIC_API_KEY=...` on the
      crontab line, or a `.env` beside the config (plaintext; chmod it `600`).
      A **systemd user unit** (`daemon schedule`) is the better answer: with
      `loginctl enable-linger` the session keyring is available and the
      credential store works normally.
    - **Windows Task Scheduler** reads DPAPI-protected credentials fine when
      the task runs as your own user with the profile loaded — which is how the
      `MyDaemonReflect` task registered by `daemon setup` is configured. A task
      set to run under `SYSTEM` will not see your Credential Manager entry.
    - **macOS launchd** prompts for Keychain access on first run from a new
      context; approve it once with *Always Allow*.

    Whichever you pick, confirm it with `daemon doctor` run *the same way* the
    job runs — the `api key` row tells you which layer answered.

## Recommended rollout

If you've never run any of the writeback jobs against your vault:

```bash
# 1. Preview against your real vault, agent.enabled still false.
daemon extract --dry-run -v
daemon link --dry-run -v
daemon reflect --dry-run -v

# 2. Pick a single note to see actual output.
daemon extract --note "Inbox/today.md"

# 3. Eyeball the result. If you like the format, flip the gate.
# (edit config.yaml → agent.enabled: true)

# 4. Run for real, small scope first.
daemon link --note "Inbox/today.md"
daemon reflect --theme personality
```

The 30-minute grace, the snapshot, and the `daemon: ignore` opt-out mean
you can stop or roll back at any point.
