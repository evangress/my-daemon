## My Daemon

Daemon — a reference from Philip Pullman's His Dark Materials, where every person has an external animal-form soul-companion that knows them completely. Also nods to Socrates' inner advisory voice. Short, memorable, ownable. The fact that "daemon" already means a background process in computing is either thematically perfect (it runs quietly in the background, being you) or distracting, depending on your audience.

My Daemon is a personal knowledge and memory system that is designed to enhance human memory, recall, personal introspection, and reflection during a human's life. My Daemon would grow along with a person as they journal, save conversations with AI technology, perform research, take notes for learning and professional development, work through hobbies and projects, and document life events or vacations. Really, My Daemon gets to know the person and becomes a digital fingerprint of the person over time and with greater interaction and use. 

## Core Principles

My Daemon is designed to process markdown files primarily and will initially be designed to gather infromation from an Obsidian vault. 

The Obsidian application is the human's interface to their knowledge and stored information. The My Daemon application creates a separate data layer that pulls from and interacts with the markdown files inside the Obsidian vault. 

My Daemon is a graph-augmented vector store with hybrid retrieval, adaptive edge weighting from implicit feedback, classical graph algorithms surfacing structural patterns, an LLM interpreting those patterns semantically, and a snapshot-isolated overnight consolidation phase where the observer can analyze and simulate without contaminating live state. The system should take inspiration from human brain mechanics and build in improvements and scale where human brains are limited. For more details, read the [MY-DAEMON-VISION.md](MY-DAEMON-VISION.md) document.

All user chat history will be stored and connected with RAG results that are confirmed by the user to be correct. 

The app needs to have a nice, flexible, gui that can run on a desktop application on Windows or Ubuntu or in a web browser.


## Initial Project Scaffold

The initial scaffold phase is done. For reference, the linked MY-DAEMON-SCAFFOLD.md file contains information regarding how the app was originally designed.

[MY-DAEMON-SCAFFOLD.md](MY-DAEMON-SCAFFOLD.md)

## Design Concepts - Research

I have asked Claude to create human cognition and technology/software mappings for reference. When thinking about how to improve or strengthen the capabilities of this application, please read and reference the [MY-DAEMON-RESEARCH.md](MY-DAEMON-RESEARCH.md) document.

## Milestone Plan — Adaptive Memory Loop

Four connected milestones that take the daemon from "answers questions" to
"learns from how you answer back, then reflects on what it's learned." The
loop is: **pick → reinforce → snapshot → analyze → narrate**.

Full implementation plan with file-level detail lives at
`~/.claude/plans/this-project-uses-qdrant-optimized-pizza.md`. The roadmap
below is the scannable version.

### M1 — Adaptive edge weighting (online) — **[x] shipped 2026-05-17**

Capture which candidate the user actually picks; reinforce the graph path
that surfaced it; make expansion weight-aware so reinforced edges rank
their neighbors higher. See the Done section below for the full
implementation summary.

### M2 — Snapshot mechanism — **[x] shipped 2026-05-17**

**Goal:** one command produces a complete read-only frozen copy of vector +
graph + feedback state. Foundation for any heavy nightly analysis that
must not contaminate live state. See the Done section below for the full
implementation summary.

### M3 — Structural-pattern analysis — **[x] shipped 2026-05-17**

**Goal:** a pure-Python pass over a snapshot that produces a structured
report. No LLM yet. Lets us inspect what the graph has learned without
booting the model. See the Done section below for the full
implementation summary.

### M4 — Observer LLM agent — **[x] shipped 2026-05-17**

**Goal:** the LLM reads the structural + evolution reports and recent
feedback, then writes a markdown letter into `<vault>/Agent/observer-<date>.md`
— same writeback discipline as `daemon reflect`. This is where the
structural patterns get *interpreted semantically*. See the Done section
below for the full implementation summary.

### Out of scope (deliberately)

- **Counterfactual query replay** (A/B'ing the LLM's answers against a
  snapshot) — not picked during planning.
- **Cron / Task Scheduler integration** for `daemon consolidate` — keep it
  manual until the loop earns trust.
- **Negative-signal decrements** — M1 only reinforces selected paths;
  non-selection rides passively. Add explicit down-weights as v2 once we
  see how the positive-only signal accumulates.

## Known Bugs

Both found while planning the memory refocus (2026-07-26) and **verified in the
code**. Neither depends on that plan; both should be fixed regardless.

- [x] **Re-ingesting a note destroys inbound edges and resets learned weights.**
  ~~`pipeline/ingest.py:101-103` calls `graph_store.remove_note()` →
  `nx.MultiDiGraph.remove_node`, which drops *all* incident edges in both
  directions. `add_note` only recreates that note's **outgoing** edges,
  hardcoded to `weight=1.0`.~~ **Fixed 2026-07-26 (M-mem-1b)** by
  `GraphStore.update_note()` — differential re-ingest that leaves surviving
  edges completely untouched. Both `ingest_vault` and `ingest_note` use it.

- [ ] **Incremental and full ingest diverge when a linked note is deleted.**
  Found while fixing the above. Delete `B.md` while `A.md` still links to it:
  a **full rebuild** produces `note::B` — a dangling placeholder keyed by the
  raw wikilink text, since `[[B]]` no longer resolves to a file — preserving
  A's outgoing link. **Incremental** ingest calls `remove_note("B.md")`, which
  drops the `A → B` edge outright, and never rebuilds it until `A` happens to
  be re-ingested for some other reason. Same family as the bug above, but on
  the deletion path. Fixing it means re-resolving the linkers' wikilinks when a
  target disappears — deliberately out of scope for M-mem-1b. Current behavior
  is pinned by `test_deleting_a_notes_target_currently_loses_the_inbound_edge`
  so the fix is visible when it lands. Also feeds the planned
  `daemon graph todos`, which would otherwise under-count.

- [ ] **`_atomic_write_text` forces `newline="\n"`** (`vault/writer.py:103`).
  On a CRLF vault — anything synced from Windows — any write rewrites every line
  of the file, turning a one-line edit into a whole-file diff. Parameterize the
  line ending and detect the file's dominant one. Blocking for any whole-vault
  write.

## Other Planned Work

Separate from the Adaptive Memory Loop arc:

- [ ] Light setup UI for `ANTHROPIC_API_KEY` + a "compose docker" button.
- [ ] Multi-embedding spaces.
- [ ] Obsidian plugin / file watcher.
- [ ] `daemon graph todos` — surface dangling wikilink targets as
  "notes you keep meaning to write" (see AI Suggestions).
- [ ] **No payload index on Qdrant's `note_path`**, though
  `retrieval/expand.py:40-47` scrolls on it for every seed of every query.
  Free latency win; folded into M-mem-3.

## Memory Refocus — UUID identity + activation ledger

Plan to re-key the system on **per-note UUIDs** and add an **activation
ledger** lives in [PLAN-MEMORY.md](PLAN-MEMORY.md). Note identity today is the
vault-relative path, which is load-bearing in four places and breaks the moment
the Librarian moves a file. The plan puts a `uuid:` in each note's frontmatter
as the shared key across SQLite (identity), Qdrant (semantics), and networkx
(relations); records which notes *fire* for every query as a sparse
"fingerprint" over note-UUID space; finds historically related queries by
activation pattern rather than wording; clusters those fingerprints into named
**themes** during the nightly `daemon consolidate` dream phase; and surfaces
past queries both into the LLM's context and back to the user.

Decisions locked in 2026-07-26: frontmatter UUID with a one-shot backfill;
themes clustered offline in consolidation; recall both injected and displayed;
theme tags proposed and written only on confirmation. Eleven milestones
(M-mem-0 … M-mem-9), each independently shippable. **No milestone requires a
full re-ingest**, and the only one-way door (the graph relabel, M-mem-6) sits
after the entire fingerprint payoff has shipped. Plan file:
`~/.claude/plans/it-s-been-a-while-compiled-ritchie.md`.

## Hermes Integration

Plan to make My Daemon the **memory and dream layer for the Hermes agent**
(Nous Research `hermes-agent`, MIT) lives in [PLAN-HERMES.md](PLAN-HERMES.md).
Investigation of Hermes's source found a native **memory-provider plugin**
interface (`prefetch` / `system_prompt_block` / `sync_turn`) that delivers
ambient recall + automatic dream injection + turn capture more natively than
MCP — so the plan recommends a provider-primary hybrid (memory-provider plugin
first, MCP server kept as optional portability for other clients). Both faces
share one core adapter over the existing `QueryEngine` / `apply_selection` /
`run_observe`. **Direction confirmed 2026-06-03: provider-primary hybrid** —
build the Hermes memory-provider plugin first (milestones H1–H3), keep the MCP
server as optional portability (H4).

**H1–H3 shipped 2026-06-05** (see Done). The memory-provider plugin is live:
`integration/core.py` (the shared adapter), `hermes/provider.py`
(`MyDaemonProvider`), the `plugins/memory/my-daemon/` shim, a `hermes:` config
block (all off by default), and `daemon hermes doctor`. **Still open:** H4 (the
MCP server) stays deferred until a non-Hermes client wants it, and the open
questions in PLAN-HERMES §14 (identity-note seed, single-vs-many clients,
NiceGUI's long-term fate) are not yet decided.

## Done

- **Hermes integration H1–H3 — memory-provider plugin (2026-06-05).** My Daemon is now a native Hermes memory provider (PLAN-HERMES.md Path A). One shared adapter — `src/my_daemon/integration/core.py` (`DaemonCore` + `build_core`) — wraps the existing pipeline behind plain methods: `recall`/`recall_block` (retrieval-only by default — Hermes brings its own model and wants cited context, not a composed answer), `endorse` (the `daemon select` reinforcement factored out of `cli.py`), `remember` (provenance-stamped capture → single-note `ingest_note` so it's recallable in-session), `latest_dream`/`dreams`/`read_dream` (the observer letters), `neighbors`, `status`. The plugin `src/my_daemon/hermes/provider.py` (`MyDaemonProvider`) subclasses Hermes's `MemoryProvider` ABC **lazily** (resolves to `object` when Hermes isn't importable, so `my_daemon` adds no runtime dep and the module unit-tests standalone) and implements the full hook set: `is_available` (network-free kill-switch on `provider_enabled`), `initialize`, `system_prompt_block` (identity framing + last night's letter), `prefetch` (ambient cited recall, caches the feedback id), `sync_turn` (non-blocking daemon thread: salient-turn capture + positive-only soft reinforcement of any prefetched note the reply used), `get_tool_schemas`/`handle_tool_call` (`mydaemon_recall`/`dream`/`neighbors` always; `endorse`/`remember` only when write-back is on), `on_memory_write`, `on_session_end`, `get_config_schema`/`save_config`, `shutdown`. New `HermesConfig` in `config.py` + both yamls (everything off by default: `provider_enabled`, `allow_write_back`). Hermes shim at `plugins/memory/my-daemon/` (`__init__.register`, `plugin.yaml`, `README.md`); new `daemon hermes doctor` preflight; new `pipeline.ingest.ingest_note`; docs at `docs-source/integrations/hermes.md` (+ mkdocs nav). The Anthropic key stays daemon-side (observer Opus + batch Haiku sub-agents) and Hermes never sees it; My Daemon stays local-only. 20 new tests (`tests/test_integration_core.py`, `tests/test_hermes_provider.py`) cover recall shape + `feedback_event_id`, budget-capped cited block, capture→recallable + provenance, write-back gating, soft endorse, dream newest-first + graceful-empty, tool routing, and non-blocking `sync_turn`. Full suite 86 pass / 1 pre-existing skip; ruff clean. **Deferred:** H4 MCP server; PLAN-HERMES §14 open questions. License: Hermes MIT → Apache-2.0 compatible, lazily imported, not redistributed.

- **Observer LLM agent (2026-05-17).** Closes the adaptive memory loop. `daemon consolidate [--snapshot <id>] [--dry-run] [-v]` runs the full M1→M4 pipeline as one command: create-or-reuse snapshot → `compute_report` + `simulate_evolution` (M3) → load prior observer letters for continuity → call the observer LLM → write `<vault>/Agent/observer-<YYYY-MM-DD>.md` via `write_atomic` (with a `vault_snapshot` of any prior same-day letter for recovery) → refresh the rolling `<vault>/Agent/observer.md` index → record `agent_observer_runs` row → `decay_unused_edges` on the live graph. Decay is the only place the live graph is mutated by consolidation, and only gentle-direction. New module `pipeline/agent_observe.py`; `_OBSERVER_SYSTEM` + `observer_letter` added to `llm/agents.py` (second-person letter, "do not invent" + uncertainty discipline, prior letters fed in for continuity); new `AgentConfig.observer_*` fields gating on a separate switch from the master `agent.enabled` (observer defaults to Opus 4.7 since this is the structural→prose lift worth paying for); new `agent_observer_runs` table in `AgentStateStore`. Seven observe tests cover letter+index write, dry-run (no vault writes, no live mutations, still records the attempt), live-graph decay, snapshot reuse, missing-snapshot error path, prior-letter continuity, and same-day re-run backup discipline. Full suite 66 pass / 1 pre-existing skip; ruff clean. Plan file: `~/.claude/plans/this-project-uses-qdrant-optimized-pizza.md`.

- **Structural-pattern analysis (2026-05-17).** `daemon analyze <snapshot_id>` runs a pure-Python pass over a snapshot bundle and produces two reports without ever touching live state. `compute_report` (new module `analysis/structural.py`) extracts Louvain communities on the full undirected projection — tags are clustering glue, only note members are surfaced — plus sampled betweenness centrality for bridging notes, `nx.bridges` for load-bearing note↔note links, orphan notes (undirected degree ≤ 1, excluding dangling), dangling wikilink targets ranked by in-degree, and the warmest reinforced edges above a configurable threshold (default 1.5). `simulate_evolution` opens the snapshot read-only, deep-copies its graph into a shadow `GraphStore`, replays every `candidate_selected` event from the snapshotted feedback DB within a configurable lookback through `weights.apply_selection`, then diffs the post-replay shadow against the snapshot baseline — top edges by absolute Δweight and top notes by aggregated incident |Δ|. Reports persist as JSON at `data/consolidation/<snapshot_id>/structural.json` and `weight_evolution.json` for M4 to consume. New `ConsolidationConfig` (out_dir + caps + betweenness sample size + lookback) wired into `Settings` and both yaml files. `FeedbackStore.selections_since(cutoff)` added so the analyzer can pull events without raw SQL. Thirteen tests cover the report shape, orphan/dangling/warm/community detection, replay correctness, snapshot immutability after replay, lookback filtering, and JSON persistence; full suite 59 pass / 1 pre-existing skip; ruff clean. Plan file: `~/.claude/plans/this-project-uses-qdrant-optimized-pizza.md`.

- **Snapshot mechanism (2026-05-17).** `daemon snapshot create | list | delete <id> | prune` produces a self-contained, read-only frozen copy of the daemon's state under `./data/snapshots/<ts>/`. Each bundle is the Qdrant native snapshot (downloaded from the docker-compose volume mount when available, otherwise pulled over the HTTP API; `--no-qdrant` skips it for graph+feedback-only bundles), a `shutil.copy2` of `graph.gpickle`, a SQLite `.backup()` of `feedback.db` (online-safe, never a raw file copy), the manifest, and a `metadata.json` with bundle version + provenance. New module `stores/snapshot.py` exposes `SnapshotBundle`, `create_snapshot`, `open_readonly` (returns a handle whose `GraphStore` and `FeedbackStore` both raise on any write — including `attach_signal` and `log` — so the M3 analyzer literally cannot contaminate live state), `list_snapshots`, `delete_snapshot`, `prune_snapshots`. `SnapshotConfig` (dir + retention_days, default 14) added to `config.py` and both yaml files. `GraphStore` gained an opt-in `read_only=True` mode whose `.save()` raises. Verified end-to-end with nine snapshot tests; full suite stays green. Plan file: `~/.claude/plans/this-project-uses-qdrant-optimized-pizza.md`.

- **Adaptive edge weighting (2026-05-17).** Implicit-feedback loop now closes: every retrieved candidate is clickable in the chat UI (and via `daemon select <feedback_id> <rank>` on the CLI), which attaches a `candidate_selected` signal to the feedback row and reinforces every edge on the shortest graph path from the seed note to the picked candidate's note. Graph expansion switched from plain BFS to a Dijkstra over `1/edge.weight` (hop budget still applied), so reinforced edges feel "shorter" and pull their neighbors in with higher decayed scores. New module `retrieval/weights.py` exposes `apply_selection` (online, called from CLI/GUI) and `decay_unused_edges` (offline, called from the consolidation pipeline in Milestone 4). Feedback table gained `selected_rank / selected_chunk_id / selected_note_path` columns via an additive migration; the canonical `build_retrieval_summary` helper is now shared between CLI and GUI so a click in either place produces the same shape. Plan file: `~/.claude/plans/this-project-uses-qdrant-optimized-pizza.md`.

- **Hybrid retrieval (2026-05-16).** Added BM25-style sparse retrieval via Qdrant sparse vectors (BM42 from fastembed) fused server-side with the dense signal using Qdrant RRF. Graph expansion is unchanged. Toggle with `embeddings.hybrid` in `config.yaml`. **Upgrade note:** the chunks collection schema changed (named dense+sparse slots); first run prints a warning and drops the old collection — re-run `daemon ingest --full` after pulling these changes.
- **Background-agent writeback (2026-05-16).** Three new commands: `daemon extract` (per-note `## Agent Notes` between sentinel markers), `daemon link` (strict-threshold wikilinks + tags; lower-confidence to `Agent/link-suggestions-*.md`), `daemon reflect` (themed memory files in `<vault>/Agent/`). Shared safety discipline in `vault/writer.py` (containment check, `daemon: ignore` frontmatter, 30-min grace, snapshot backups). Gated by `agent.enabled` (default `false`). Defaults to Haiku 4.5 via the new `llm.batch_model`. Tkinter setup window gained a Windows Task Scheduler checkbox for daily reflection.


## AI Suggestions (Agent Thoughts)

### 2026-05-16 — First draft completed

Phase 0 + most of Phase 1 of the scaffold is in. End-to-end pipeline compiles and unit tests pass without external services. Below are thoughts I had during the build that I deliberately *did not* implement, because they belong in later phases or want your input first.

**"Candidate-selection feedback" (cheap to add, big payoff later).**
The retrieval orchestrator already trims to a `candidate_pool` floor of 3, matching the PROJECT_MANAGEMENT.md requirement. The natural next step is a `daemon select <feedback_id> <candidate_index>` command that writes a `candidate_selected` signal back to the feedback DB. That row becomes the training signal for adaptive edge weighting in Phase 4. Not built yet because we should agree on the *shape* of the signal first (per-chunk? per-note? does selecting candidate #2 down-weight #1?).

**"More like these" follow-up.**
PROJECT_MANAGEMENT.md says the user should be able to ask for *another* set of 3 results. I sketched this in my head as `daemon query "..." --skip <feedback_id>` — re-runs the same query but filters out the previously-shown chunk_ids. It is one tiny CLI flag away. Held back because the GUI is the eventual home for this interaction and the CLI version may not be worth investing in.

**Dangling wikilinks as a feature, not a bug.**
The graph store currently keeps dangling wikilink targets as placeholder nodes (filtered out of stats). These are actually interesting — they represent "notes the user means to write." A future `daemon graph todos` command could surface them. Stashing the idea here.

**The philosophical framing should live somewhere the daemon can read.**
You and I have talked about why this project matters. Right now that context lives in CLAUDE.md (which only I see) and PROJECT_MANAGEMENT.md (which is gitignored from RAG, ironically). If My Daemon ingests its own vault, you might want to seed it with a short "purpose" note so it can answer questions like "why am I building this" from your own voice rather than mine. Just an idea.

