# `vault/` — Markdown into structured Notes and Chunks

Source: `src/my_daemon/vault/`.

The vault layer is the only code that touches your `.md` files. Everything
downstream operates on the in-memory `Note` and `Chunk` models that this
module produces.

## `vault.reader.VaultReader`

Walk the vault root and yield `Note` objects with **resolved** wikilinks.

```python
from my_daemon.vault import VaultReader

reader = VaultReader(Path("~/Documents/Obsidian/MyVault"),
                     exclude_dirs=[".obsidian", ".trash", "templates", "Agent"])
for note in reader.read_all():
    ...
```

Two-pass design: first parse every file, then build a
`{lowercased_title → relative_path}` index, then yield each note with its
wikilinks rewritten to point at real `relative_path`s where the target was
found. Dangling targets (a `[[Foo]]` with no matching note) are left as the
raw target string — the graph store will turn them into placeholder nodes.

`discover()` is exposed separately for callers that want a sorted list of
markdown paths without parsing.

### Exclusions

- Hidden directories (any path part starting with `.`) are skipped.
- Configured `exclude_dirs` are matched case-insensitively at any depth.

## `vault.parser.parse_note`

`parse_note(file_path, vault_root) -> Note` — read one file, return the
parsed `Note`.

What it extracts:

- **Frontmatter** via `python-frontmatter`. Returned as the raw dict.
- **Title**: the first H1 in the body, falling back to the filename stem.
- **Wikilinks** (raw targets): regex `\[\[([^\]|]+)(?:\|[^\]]+)?\]\]`,
  de-duplicated in encounter order. Aliases (`[[target|alias]]`) keep only
  the target.
- **Tags**: union of frontmatter `tags:` (string or list) and inline `#tag`
  matches. Inline detection uses a negative lookbehind so `foo#bar` and
  `https://x.io/#section` are *not* picked up; fenced code blocks are
  stripped from the tag-detection source.
- **mtime** (UTC) and **word count**.

## `vault.identity` — reading a note's UUID

`parse_note` populates `Note.uuid` and `Note.uuid_source` by scanning
frontmatter for `ADOPT_KEYS` in priority order: `uuid` (ours), then `uid`,
`id`, `guid`, `note-id`, `permanent_id`.

- A value that parses as a UUID is **adopted verbatim**, canonicalized to
  lowercase-hyphenated form. Uppercase, braced, and `urn:uuid:` forms are all
  accepted, because that is what people and plugins actually write.
- A **foreign** key holding an opaque non-UUID string of 8+ characters (a
  Zettelkasten timestamp, say) yields a **derived** `uuid5`. Deterministic, so a
  re-run on a machine with no registry converges on the same answer.
- A non-UUID value in **our own** `uuid:` key reads as *absent*, never derived.
  Our key has a defined type, so garbage in it means a hand-edit or a sync
  corruption; minting a new identity there would silently orphan that note's
  entire history. Ingest restores the registry's value instead.
- `derive_path_uuid(rel_path)` is the fallback for notes the daemon may not
  write to. Deterministic across machines, but **not rename-stable** — the same
  weakness as path-keying, now at least recorded per note rather than implicit.

`NAMESPACE` is fixed forever. Changing it would silently re-identify every note
that relies on derivation.

## `vault.chunker.chunk_note`

`chunk_note(note, max_tokens=512, overlap_tokens=50) -> list[Chunk]`.

Two-stage splitting:

1. **MarkdownHeaderTextSplitter** splits on H1/H2/H3, preserving the heading
   stack on each piece as `heading_path` (e.g.
   `["Designing AI Memory", "RAG vs Fine-tuning"]`).
2. **RecursiveCharacterTextSplitter** secondary-splits any piece that exceeds
   `max_tokens` (token count ≈ chars/4 — a coarse heuristic that avoids
   loading a real tokenizer at chunk time). Splits prefer paragraph, then
   sentence, then word boundaries.

Each `Chunk` carries:

- a **stable id**: SHA-1 of `(relative_path, heading_path, chunk_index, content)`,
  truncated to 16 hex chars. Same content → same id, so incremental ingest
  doesn't thrash.
- The parent note's tags (inherited).
- Wikilinks re-extracted from the *chunk* body, so a future chunk-aware graph
  could traverse at chunk granularity.

If a note has no headings, the entire body becomes one piece (which may then
be size-split). Empty bodies produce zero chunks.

## `vault.writer` — Safe writeback

Source: `src/my_daemon/vault/writer.py`. This module is the *only* path by
which any background-agent job mutates a user note. Everything else hangs
off three public functions:

### `write_agent_section(note_path, inner_body, ...) -> WriteResult`

Replace (or append) the `## Agent Notes` section between
`<!-- daemon:start --> ... <!-- daemon:end -->` sentinels. Idempotent: byte
content outside the sentinels is preserved exactly. Refuses to write if more
than one `## Agent Notes` heading is found (rather than mangle the file).

### `insert_wikilinks(note_path, links, ...) -> WriteResult`

`links = [(target_title, anchor_text), ...]`. For each pair, rewrite the
**first** case-insensitive word-boundary match of `anchor_text` to
`[[target_title|anchor_text]]`. Skips matches inside existing wikilinks and
inside fenced code blocks.

### `add_tags(note_path, tags, ...) -> WriteResult`

Extend the frontmatter `tags:` list with new entries, preserving order.
Creates the `tags:` block if absent. Never writes inline `#tag` markers —
frontmatter is the safer surface to mutate.

!!! warning "This round-trips the document through PyYAML"
    `add_tags` still uses `frontmatter.loads`/`dumps`, which rewrites the whole
    file. Measured on one small note, a single call collapsed CRLF to LF on
    every line, expanded flow-style `tags: [a, b]` into block style, deleted a
    YAML comment, and dropped the trailing newline. It needs converting to the
    textual approach below — see Known Bugs in `PROJECT_MANAGEMENT.md`.

### `set_frontmatter_key_textual(note_path, key, value, ...) -> WriteResult`

Insert or replace **one scalar `key: value` line**, leaving every other byte of
the file untouched — comments, key order, quoting style, flow-style lists,
dates, and line endings all survive. A note with no frontmatter gains a minimal
block; new keys go at the end of an existing one.

This exists because the UUID migration touches every note in the vault, and a
whole-vault reformat is the fastest way to make a user distrust the tool. The
byte-level contract is pinned by `tests/test_vault_writer_frontmatter.py`.

Two details that are easy to get wrong, both regression-tested:

- **The opening `---` only counts on the very first line.** A `---` further down
  is a horizontal rule.
- **Reads use `newline=""`.** Python's universal-newline mode translates CRLF to
  LF *on read* — that, not the write, is what would turn every edit to a
  Windows-synced vault into a whole-file diff.

`value` is written bare, so it must be YAML-safe as-is. This is not a
general-purpose YAML writer.

### `remove_frontmatter_key_textual(note_path, key, ...) -> WriteResult`

Deletes a single `key:` line. The rollback path for the UUID migration.
Surgical by design — it never restores a body, so it stays safe to run on a
file the user has edited since the key was written.

### `detect_newline(text) -> str`

The dominant line ending of a document, so inserted lines match their
neighbours.

### `is_writable(note_path, ..., allow_agent_folder=False) -> (ok, reason)`

The five-gate check (containment, agent folder, opt-out, grace, parseable
frontmatter). Returned `reason` strings flow into the stats tables shown
after each `daemon extract` / `link` / `reflect` run.

`allow_agent_folder=True` relaxes the agent-folder gate **and only that gate**.
That gate exists to stop the daemon rewriting its own generated *prose*;
stamping an identity key into its own files is a different act, and the observer
letters need to be addressable like any other note. Containment, `daemon:
ignore`, and the grace window always apply.

### `snapshot(note_path, ...) -> Path`

Copy a note to `<vault>/<agent_folder>/backups/<rel>.<ts>.md`. Every
successful write is preceded by a snapshot.

### `write_atomic(path, text)`

Public wrapper around the tempfile + `os.replace()` write used by the
reflection writer (which writes whole files, not section edits).
