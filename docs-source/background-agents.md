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

## Scheduling

### Windows

The `daemon setup` window has a checkbox: *"Run `daemon reflect` daily at
03:00 (Windows Task Scheduler)."* Saving the form runs `schtasks /Create`
under the hood. The task is named `MyDaemonReflect`; unchecking it on a
subsequent save removes the task.

### Linux / macOS

```cron
# crontab -e
 0 3 * * *  /path/to/my-daemon/.venv/bin/daemon reflect
15 3 * * *  /path/to/my-daemon/.venv/bin/daemon extract
```

The 15-minute gap exists because `reflect` reads recent notes; running
`extract` afterward lets the next reflection see the new `## Agent Notes`
sections.

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
