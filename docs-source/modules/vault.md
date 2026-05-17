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

### `is_writable(note_path, ...) -> (ok, reason)`

The five-gate check (containment, agent folder, opt-out, grace, parseable
frontmatter). Returned `reason` strings flow into the stats tables shown
after each `daemon extract` / `link` / `reflect` run.

### `snapshot(note_path, ...) -> Path`

Copy a note to `<vault>/<agent_folder>/backups/<rel>.<ts>.md`. Every
successful write is preceded by a snapshot.

### `write_atomic(path, text)`

Public wrapper around the tempfile + `os.replace()` write used by the
reflection writer (which writes whole files, not section edits).
