# LIBRARIAN-PLAN.md — Build plan for the Obsidian Librarian

> **One line:** A passive background agent that *shapes* an Obsidian vault —
> cross-links, sorts notes into folders, normalizes formatting, and writes
> abstracts — built as a **thin layer on top of `my_daemon`** (the package
> alongside it in this repo, imported as a library) so it reuses that project's
> vault I/O, embeddings, graph, vector store, and battle-tested write-safety
> instead of rebuilding them.

This is a design + milestone plan, not yet code. It is grounded in the actual
`my_daemon` APIs (`vault/writer.py`, `pipeline/agent_link.py`,
`pipeline/agent_extract.py`, `config.py`, `stores/`, `embeddings/`).

**Repo layout:** the Librarian lives at `librarian/` inside the **my-daemon**
repo (folded in from the former standalone `my-daemon-librarian` repo on
2026-06-05). Same repo as `my_daemon`, separate process. How it's packaged in
the repo — a uv-workspace member with its own `pyproject.toml`, or a
`my_daemon.librarian` subpackage — is an open question (see §5).

---

## 0. Decisions locked in (2026-06-03 Q&A)

| Question | Decision |
|---|---|
| Relationship to `my_daemon` | **Depend on it as a library** (hard dependency) |
| Folder sorting | **Auto-move files**, with backups + wikilink rewriting + grace window + dry-run-first + default-off gate |
| Default LLM backend | **Hybrid**: cheap local (Ollama) for bulk, cloud (Haiku/GPT) for high-value summaries |
| Storage stack | **Reuse my-daemon's** — Qdrant (Docker) + NetworkX + local BGE-small embeddings |
| Repo layout (2026-06-05) | **Sub-package of the my-daemon repo** (`librarian/`), not a standalone repo |

"Lightweight" therefore means **lightweight in code** — a small layer over proven
infrastructure — not minimal dependencies.

---

## 1. Architecture — one thin layer, three new capabilities

```
        ┌─────────────────────────────────────────────────────────────┐
        │  librarian  (this subtree, Apache-2.0)                       │
        │                                                              │
        │  run / watch (scheduler)  ──▶  incremental pipeline:         │
        │     ingest changed → link → extract → CLASSIFY → SORT(move)  │
        │                                  → CLEANUP → abstract-embed  │
        │                                                              │
        │  NEW modules:                                                │
        │   • preferences.py   loads note-to-the-librarian.md → context │
        │   • llm/router.py    hybrid local(Ollama)+cloud provider     │
        │   • classify.py      note → category/folder (judgment-first)  │
        │   • mover.py         link-aware atomic file move             │
        │   • cleanup.py       whitespace/format normalizer            │
        │   • runner.py        incremental, stateful background loop    │
        └───────────────┬──────────────────────────────────────────────┘
                        │ imports
                        ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  my_daemon  (src/my_daemon, same repo — imported as a library)│
        │  VaultReader · vault.writer (is_writable/snapshot/atomic/    │
        │  insert_wikilinks/add_tags) · Embedder · VectorStore(Qdrant) │
        │  · GraphStore(networkx) · AgentStateStore · pipeline.ingest  │
        │  · agent_link.run_link · agent_extract                       │
        └───────────────┬──────────────────────────────────────────────┘
                        ▼
            Qdrant (:6333, docker) · graph.gpickle · the Obsidian vault
```

**Reuse vs. build:**

| Capability | Source |
|---|---|
| Cross-linking + tagging | **Reuse** `agent_link.run_link` (strict cosine≥0.85 + title-match gate already implemented) |
| Per-note summary/abstract section | **Reuse** `agent_extract` (writes `## Agent Notes` in sentinel block) |
| Embeddings, vector search, graph | **Reuse** `Embedder`, `VectorStore`, `GraphStore` |
| Vault read + write-safety | **Reuse** `VaultReader`, `vault.writer` |
| **Folder classification** | **Build** `classify.py` |
| **File moving (link-aware)** | **Build** `mover.py` (extends `vault.writer` discipline) |
| **Whitespace/format cleanup** | **Build** `cleanup.py` |
| **Hybrid LLM router** | **Build** `llm/router.py` (my-daemon's `LLMConfig.provider` is `Literal["anthropic"]` only) |
| **User preferences context** | **Build** `preferences.py` (loads `note-to-the-librarian.md`) |
| **Background scheduler** | **Build** `runner.py` + cron/systemd templates |

---

## 2. Milestones

### L0 — Foundation: dependency wiring + config + provider router *(MVP scaffolding)*
- Stand up a Typer app `librarian` (mirror my-daemon's `daemon` CLI shape). `[project.scripts] librarian = "obsidian_librarian.cli:app"`.
- Wire the `my_daemon` dependency. **In-repo this is a uv-workspace dependency, not a `../my-daemon` path dep** (see §5 for the workspace-vs-subpackage decision); set license Apache-2.0. (Python version is settled: the repo runs 3.14.4 and my-daemon's torch/sentence-transformers/qdrant stack is confirmed working on 3.14; `requires-python` is pinned `>=3.14,<3.15` — a strict subset of my-daemon's `>=3.11,<3.15`.)
- `config.py`: a `LibrarianConfig` (pydantic-settings, env prefix `LIBRARIAN_`) for librarian-only settings; load my-daemon's `Settings` via `my_daemon.config.load_settings()` for shared infra (vault, embeddings, Qdrant, graph). Read the **same `config.yaml`**, add a `librarian:` section.
- `llm/router.py`: a provider-agnostic `generate(task, prompt, *, tier)` where `tier ∈ {bulk, quality}` routes to Ollama (bulk) or cloud Anthropic/OpenAI (quality). Lazy-import each SDK. Embeddings stay on my-daemon's local BGE — the router governs **generative** calls only.
- `preferences.py`: load `<vault>/note-to-the-librarian.md` (path from config), cache it, and expose a `context_block()` the router prepends to **every** generative prompt. Re-read each run so user edits take effect live; degrade gracefully (empty block) when the file is absent. The file path is registered as **never-writable** (see §6).
- **Tests:** config layering (yaml+env) resolves; router picks the configured provider per tier and never imports an SDK it doesn't use; `is_available()` probes do no network; `preferences.context_block()` returns the file contents when present and an empty block when absent.

### L1 — Background runner + incremental state *(the "runs on its own" core)*
- `runner.py`: one idempotent pass = detect changed notes (mtime/content-hash vs. recorded state) → `ingest` just those → run the enabled shaping steps. Reuse `AgentStateStore` (or a small `librarian.db`) to record per-note last-processed hash so re-runs are cheap.
- `librarian run [--dry-run] [--only <note>] [-v]` (one pass) and `librarian watch` (watchdog observer on the vault, **debounced** — respect the grace window so we never touch a file mid-save).
- Scheduling templates: a systemd user timer (Linux) and a cron snippet; no agent-triggered full re-ingest (keep heavy ops operator-scheduled, matching my-daemon's stance).
- **Tests:** unchanged notes are skipped on a second pass; `--dry-run` performs zero writes; watch debounces rapid saves.

### L2 — Cross-link + abstract (reuse, wire-through) *(fast win)*
- Wire `agent_link.run_link` and `agent_extract` into the runner behind librarian config gates. These already exist and are strict/safe — this milestone is integration + a unified dry-run report, not new linking logic.
- Optional: embed the generated abstract as its own point in Qdrant (note-level vector alongside chunk-level) so "related notes" can use a whole-note signal. Store the abstract in frontmatter (`abstract:`) or the `## Agent Notes` block.
- **Tests:** runner invokes link/extract only when gated on; abstract embedding round-trips.

### L3 — Classifier: note → category/folder *(judgment-first, preferences-guided)*
- `classify.py`: for each note, derive a target category using (a) embedding neighborhood + graph community (reuse `GraphStore`/Qdrant; my-daemon's `analysis/structural.py` already does Louvain communities — consider importing it), then (b) a **bulk-tier** LLM call — with `preferences.context_block()` prepended — to assign the folder the user would find most logical.
- **Judgment-first, guided by `note-to-the-librarian.md`:** the user's file is the primary source of taxonomy/naming/placement rules; wherever it's silent the librarian uses its own judgment. The existing folder tree is itself a strong prior (prefer fitting notes into folders the user already maintains over inventing new ones). The `categories` config list is an **optional hard constraint** — normally empty. Write the decision to frontmatter `category:` first (cheap, reversible) — the *move* is L4.
- Guardrail against sprawl: respect a max-new-folders/run budget and route low-confidence notes to `unsorted_folder` rather than guessing.
- **Tests:** stable notes get stable categories across runs; a rule stated in a fixture `note-to-the-librarian.md` overrides the judgment default; low-confidence notes fall back to "unsorted"; an existing-folder prior is preferred over a novel folder.

### L4 — Mover: link-aware atomic file relocation *(the dangerous one — most care)*
This is the crux of "auto-move with safeguards." `mover.py` extends — never bypasses — `vault.writer`'s discipline.

A safe move of `A/note.md` → `B/note.md` must:
1. Pass `is_writable` (containment, not in `Agent/`, no `daemon: ignore`, past grace window).
2. **Snapshot** the note (and any reference file it will rewrite) via `writer.snapshot` before touching anything.
3. **Find every reference** to the note across the vault and classify it:
   - `[[note]]` / `[[note|alias]]` — resolves by **basename**; survives a move **iff** the basename stays globally unique. ✅ usually safe.
   - **Basename collision** — if moving makes two notes share a basename, previously-unambiguous `[[note]]` links become ambiguous → **refuse the move** (or relocate-and-rename, only if configured).
   - Path-qualified `[[folder/note]]`, markdown `[text](rel/path.md)`, embeds `![[...]]` / `![](rel/path)`, and attachment-relative links — **must be rewritten** to the new path. This replicates what Obsidian's "Automatically update internal links" does when you move inside the app (which we are bypassing).
4. Move the file (atomic: write to new path, fsync, then unlink old — or `os.replace` within the same filesystem), then atomically rewrite each affected reference file.
5. Record the move (old→new) in `librarian.db` for auditability and one-shot **undo**.

Gating: a dedicated `librarian.sort_enabled` flag, **off by default**, with `--dry-run` printing the full move + link-rewrite plan. Honor Obsidian's link-format setting (shortest-path vs. relative vs. absolute-in-vault) from config.

- **Tests (this milestone is the most test-heavy):** basename-unique move leaves `[[wikilinks]]` untouched and rewrites only path-based refs; basename collision is refused; path-qualified links + embeds are rewritten correctly; a snapshot exists for every file changed; `librarian undo <move_id>` restores byte-for-byte; move + rewrite is atomic (crash between steps leaves a recoverable state).

### L5 — Cleanup: whitespace / format normalizer
- `cleanup.py`: collapse 3+ blank lines → 1, strip trailing whitespace, ensure a single trailing newline, normalize heading spacing — **idempotent**.
- **Guardrails (because this touches user prose, which my-daemon never does):** operate on the body only; **never** alter fenced code blocks, frontmatter, or the `<!-- daemon:start --> … <!-- daemon:end -->` section (preserve byte-for-byte); after producing the candidate, assert the diff is **whitespace-only** (normalize-whitespace equality on both sides) and abort if any non-whitespace byte changed. Snapshot before write; atomic write.
- **Tests:** idempotence (second run is a no-op); code fences/frontmatter/sentinel block preserved exactly; a note that would change a non-whitespace byte is refused; running cleanup after extract doesn't disturb the agent section.

### L6 — Packaging, docs, ops
- Fill out `pyproject.toml` deps + scripts; `config.example.yaml` `librarian:` block; `.env.example`; a README quickstart (start Qdrant via my-daemon, point at the vault, `librarian run --dry-run`, then enable features one at a time).
- `librarian doctor`: verify Qdrant reachable, vault path valid, provider(s) reachable, interpreter/wheels OK.
- License-compatibility note for added deps (`debug/license-compliance` style, mirroring my-daemon).

---

## 3. Config additions (`librarian:` section, gates default-off)

```yaml
librarian:
  enabled: false                 # master gate
  # which shaping steps run (all start conservative)
  link: true
  extract: true
  classify: true
  sort_enabled: false            # the file-MOVER — off until proven on a real vault
  cleanup: false                 # touches prose — off by default
  # preferences — the user's standing instructions; judgment fills every gap
  preferences_file: "note-to-the-librarian.md"   # vault-root-relative; off-limits to all writeback
  # taxonomy — OPTIONAL hard constraints; normally empty so the librarian uses
  # judgment guided by preferences_file and the vault's existing folders
  categories: []
  unsorted_folder: "Unsorted"
  classify_min_confidence: 0.6
  max_new_folders_per_run: 3
  # mover
  obsidian_link_format: shortest # shortest | relative | absolute  (match the vault's setting)
  refuse_on_basename_collision: true
  # provider routing (generative calls only; embeddings stay on my-daemon BGE)
  llm:
    bulk:    { provider: ollama,    model: "llama3.1:8b",     base_url: "http://localhost:11434" }
    quality: { provider: anthropic, model: "claude-haiku-4-5" }   # or { provider: openai, model: gpt-... }
  # scheduling
  watch_debounce_seconds: 120
  write_grace_minutes: 30        # reuse my-daemon's collision-avoidance window
```

Secrets (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are **environment-only**, never in yaml — same rule my-daemon enforces. Ollama needs no key.

---

## 4. Dependencies & license (Apache-2.0)

- `my_daemon` — Apache-2.0 ✅ (hard dependency; in-repo it's a uv-workspace dependency rather than the old `{ path = "../my-daemon", editable = true }` path dep — see §5).
- `ollama` (Python client) — MIT ✅ ; `openai` SDK — Apache-2.0 ✅ ; `watchdog` (file events) — Apache-2.0 ✅ ; `typer`/`rich`/`pydantic*` already transitive via my-daemon.
- No new vector/graph/embedding deps — those come from my-daemon.

---

## 5. Open questions / risks

1. ~~**Python version.**~~ **Resolved 2026-06-03:** the repo runs 3.14.4; my-daemon's torch 2.12 / sentence-transformers 5.5 / qdrant-client 1.18 / fastembed 0.8 / numpy 2.4 are all installed and working on 3.14. Pin set to `>=3.14,<3.15`. No longer a blocker.
2. ~~**Taxonomy source.**~~ **Resolved 2026-06-03:** organization preferences live in a single user-authored `note-to-the-librarian.md` at the vault root, injected into every generative call; the librarian uses its own judgment wherever the file is silent, preferring the vault's existing folders. The `categories` config remains an optional hard-constraint override. (See L0 `preferences.py` + L3.)
3. **In-repo packaging (new 2026-06-05).** Now that the Librarian lives under `librarian/` in the my-daemon repo, how should it be packaged: a separate distribution that's a **uv-workspace member** with its own `pyproject.toml` (keeps the `librarian` CLI and deps cleanly isolated, still imports `my_daemon`), or folded into the existing package as a **`my_daemon.librarian` subpackage** (one distribution, simplest deps, but blurs the "separate process / separate concern" line)? Default leaning: **workspace member** — preserves the clean boundary while losing the cross-repo version skew that motivated the move.
4. **Ollama models.** Which local model for bulk classification/labeling, and is a local embedding model (e.g. `nomic-embed-text`) wanted later to go fully offline, or keep BGE-small via my-daemon? (Default: keep BGE.)
5. **Attachments.** Should the mover also relocate a note's linked attachments (images/PDFs) alongside it, or leave them and only fix links? (Default: leave attachments, fix links.)
6. **Single shared `config.yaml` vs. its own.** Reuse my-daemon's file with a `librarian:` block (recommended), or a separate `librarian.yaml`?

---

## 6. Out of scope (for now)

**Off-limits to all writeback (never linked / moved / cleaned / summarized):** the `Agent/` folder (my-daemon-owned), any note with `daemon: ignore` frontmatter, and **`note-to-the-librarian.md`** itself — registered as never-writable by path regardless of frontmatter, since it's the control file.

- Reimplementing anything my-daemon already provides (retrieval, the dream/observer loop, the GUI).
- Agent-triggered full re-ingest or consolidation — operator/scheduled only.
- Renaming notes for de-duplication, or merging notes — content edits beyond whitespace are out until the move/cleanup paths have earned trust.
- Remote/network exposure — local-only, like my-daemon.
