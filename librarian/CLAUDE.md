# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in the
`librarian/` subtree of the **my-daemon** repository.

## What this is

**Obsidian Librarian** — a passive, background agent that quietly *shapes* an Obsidian vault: it cross-links related notes, sorts notes into folders by category, normalizes whitespace/formatting, and writes short abstracts/summaries for embedding. Where its host project **`my_daemon`** (this repo's `src/my_daemon`) is the *memory & retrieval* layer over a vault (graph-augmented vector store, "dream" consolidation, the front end for the Hermes agent), the Librarian is the *gardener*: it keeps the vault itself tidy and well-organized.

The two live in **one repository** but run as **separate processes** over a shared vault and infrastructure. (Historically the Librarian was its own repo, `my-daemon-librarian`; it was folded in here on 2026-06-05 because it leans so heavily on `my_daemon`'s internals that co-locating lets the two evolve together in one commit.)

## Relationship to `my_daemon` (read this first)

The Librarian is a **thin layer built on top of `my_daemon`, which it imports as a library** — the package living alongside it in this repo under `src/my_daemon`. It does *not* reimplement vault reading, embeddings, the vector store, the graph, or the write-safety discipline — it reuses them and adds three things my-daemon deliberately does not do: **folder sorting (moving files)**, **whitespace/format cleanup**, and a **hybrid local+cloud LLM router**.

Concretely, it imports from the `my_daemon` package:

| Need | Reused from `my_daemon` |
|---|---|
| Config (vault path, exclude dirs, embeddings, Qdrant, graph) | `my_daemon.config.load_settings`, `Settings` |
| Read the vault → `Note` objects | `my_daemon.vault.VaultReader`, `my_daemon.models.Note` |
| **Write-safety discipline** | `my_daemon.vault.writer` — `is_writable`, `snapshot`, `write_atomic`, `insert_wikilinks`, `add_tags` |
| Embeddings (local BGE-small) | `my_daemon.embeddings.Embedder` |
| Vector store (Qdrant) + graph (NetworkX) | `my_daemon.stores.VectorStore`, `GraphStore`, `AgentStateStore` |
| Incremental ingest | `my_daemon.pipeline.ingest` |
| Cross-linking + per-note summaries (already built!) | `my_daemon.pipeline.agent_link.run_link`, `my_daemon.pipeline.agent_extract` |

**`my_daemon.vault.writer` is the safety contract — never bypass it.** Every write to a user's markdown goes through it so the same guarantees hold everywhere: path-containment (no escaping the vault), `daemon: ignore` frontmatter opt-out, the `Agent/` folder is off-limits, a configurable grace window so we never collide with a save in progress, a `snapshot()` backup before every change, and atomic tempfile+`os.replace` writes. The Librarian's new write paths (move, cleanup) **extend** this module rather than working around it.

## Decisions locked in (2026-06-03 planning Q&A)

| Question | Decision | Consequence |
|---|---|---|
| Relationship to my-daemon | **Depend on it as a library** | `my_daemon` is a hard dependency; "lightweight" means *little code*, not *few deps* |
| Folder sorting aggressiveness | **Auto-move with safeguards** | Files are physically relocated — backups, wikilink rewriting, grace window, dry-run-first, default-off gate |
| Default LLM backend | **Hybrid: cheap local + cloud** | Local Ollama model for bulk; cloud (Haiku/GPT) for high-value summaries. A provider router is net-new (my-daemon is Anthropic-only) |
| Storage stack | **Reuse my-daemon's** | Qdrant (Docker) + NetworkX + local BGE embeddings — point at the same `config.yaml`/vault. Requires Qdrant running |
| Repo layout (2026-06-05) | **Sub-package of the my-daemon repo** | Lives at `librarian/`; same repo as `my_daemon`, still a separate process. Packaging (uv-workspace member vs. `my_daemon.librarian` subpackage) is an open question — see LIBRARIAN-PLAN.md §5 |

Full milestone plan with file-level detail: **[LIBRARIAN-PLAN.md](LIBRARIAN-PLAN.md)**.

## The user's standing instructions: `note-to-the-librarian.md`

A single user-authored file at the vault root — `<vault>/note-to-the-librarian.md` (path configurable) — is the Librarian's equivalent of this CLAUDE.md: the user's standing preferences for how notes should be foldered, named, linked, summarized, and what to leave alone. The Librarian reads it at the **start of every run** (so edits take effect live) and injects it as a context block into every generative LLM call (classification, folder labeling, link rationale, cleanup style).

**Precedence: explicit preferences in this file win; everywhere the file is silent, the Librarian uses its own judgment** for whatever organization is most logical/useful to the user. The file guides *how* to organize — it does **not** flip the safety gates (`sort_enabled`, `cleanup`, …), which stay config-controlled. If the file is absent, the Librarian runs purely on judgment + sensible defaults.

The file is **off-limits to the agent's own writes** — never linked, moved, cleaned, or summarized (treat it like the `Agent/` folder; it should also carry `daemon: ignore` frontmatter so my-daemon's jobs skip it too).

## Safety ethos (inherited from my-daemon — non-negotiable)

- **Default off, dry-run first.** Every writeback feature is gated behind config and ships disabled. The riskiest one (moving files) stays off until proven on a real vault with `--dry-run`. Mirror my-daemon's `agent.enabled` pattern.
- **Moving a file is the most dangerous thing this agent does.** Obsidian `[[wikilinks]]` resolve by basename, so a move only stays safe if the basename stays unique *and* every path-based reference (`[](rel/path.md)`, `![[embeds]]`, path-qualified `[[folder/Note]]`) is rewritten. The mover must replicate Obsidian's "update internal links on move" behavior and refuse on basename collisions. See LIBRARIAN-PLAN.md §Mover.
- **Cleanup touches user prose** (unlike my-daemon, which only ever appends an `## Agent Notes` block). Whitespace normalization must produce a **whitespace-only diff** — verify no non-whitespace bytes changed — and must never touch fenced code blocks, frontmatter, or the `<!-- daemon:start/end -->` sentinel section.
- **Secrets are environment-only**, never in `config.yaml` (same rule as `ANTHROPIC_API_KEY`). The `.env` is gitignored.

## Commands

uv-managed Python project. It depends on `my_daemon`, which lives in the same repo (`../src/my_daemon`), so it develops directly against that source.

```bash
uv sync                                   # install deps (incl. the in-repo my_daemon dependency)
uv run librarian run --dry-run -v         # one incremental pass over the vault, no writes (planned entry point)
uv run librarian watch                    # debounced file-watcher daemon (planned)
uv run pytest                             # tests
uv run pytest tests/test_mover.py::test_x # a single test
uv run ruff check .                       # lint
```

Qdrant must be running (the Librarian reuses my-daemon's vector store); the `docker-compose.yaml` is at the repo root:

```bash
(cd .. && docker compose up -d)           # starts Qdrant on :6333
```

The Librarian reads the **same `config.yaml`** my-daemon uses (point `vault.path` at the Obsidian vault) plus its own `librarian:` section — see LIBRARIAN-PLAN.md §Config.

## Conventions (match `my_daemon` — same repo)

- License **Apache-2.0**; start every source file with `# SPDX-License-Identifier: Apache-2.0`.
- Before adding any dependency, confirm it's Apache-2.0-compatible (Ollama client, OpenAI SDK, watchdog are all fine).
- `ruff` for lint, `pytest` for tests; tests must pass **without** external services where possible (mock Qdrant/LLM), with an opt-in env flag for the full end-to-end pass.
- pydantic-settings config layered as defaults → `config.yaml` → env (prefix `LIBRARIAN_`, nested `__`).

## Current state

Planning docs only: this file + LIBRARIAN-PLAN.md, moved here from the former `my-daemon-librarian` repo on 2026-06-05. No librarian code stood up yet (the old repo's `main.py` was unmodified PyCharm boilerplate and was dropped in the move; the package skeleton + `pyproject.toml` is the first thing L0 creates). Interpreter is settled: the repo runs **Python 3.14.4**, and my-daemon's full stack (torch 2.12, sentence-transformers 5.5, qdrant-client 1.18, fastembed 0.8, numpy 2.4) is confirmed installed and working on 3.14. First real modules to stand up, in order: the **provider router**, then the **classifier→mover**, then **cleanup** (see LIBRARIAN-PLAN.md milestones L0–L6).
