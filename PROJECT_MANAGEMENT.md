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

**[MY-DAEMON-RESEARCH-APPLIED.md](MY-DAEMON-RESEARCH-APPLIED.md) (2026-07-28)** is
the post-implementation companion. Where the document above surveys the
landscape, this one starts from the mechanisms *actually running in the repo* —
hybrid RRF seeding, weighted-Dijkstra expansion, path reinforcement and half-life
decay, the IDF-weighted activation ledger, HDBSCAN themes with centroid matching,
Louvain structural analysis, snapshot-isolated consolidation — cites each to
`file:line`, pairs it with verified memory-science and ML literature, names four
honest gaps, and lists costed candidate methods in three tiers. It also carries a
parameter-provenance appendix separating the principled constants from the
judgement calls. Read it before adding any new learning machinery.

**Every proposal in it carries a checkbox**, with a status index at the top —
`- [x]` is running in the repo today, `- [ ]` is available to build, each with
effort and licence. Currently **6 shipped · 2 partial · 15 open of 23**
*(corrected 2026-07-31 — this line still read 4/1/18 after §IV.10 and §IV.18
shipped on 2026-07-30)*. Item IDs (`§IV.7`) are permanent and are never
renumbered, so they mean the same thing here, in that document, and in commit
messages. The index tells you what is left; the largest open item is **§IV.21**,
claim-level indexing, and the largest *conceptual* gap is still **§IV.9**, the
salience layer.

**For the order to build them in, read the AI-suggestions entry for 2026-07-31
below, not that document's own "If you are choosing what to build next"
section** — the latter was written before §IV.17 and §IV.18 shipped and both of
its top picks are now done.

**Seven items (§IV.17–§IV.23) were added 2026-07-30 from a competitor review**
against Hindsight and Honcho — they carry a `†` in the status index. See the
AI-suggestions entry for 2026-07-30 below for what that review found and what it
did not change.

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

- [x] **`add_tags`' frontmatter round-trip rewrites the whole file.**
  *(Corrected 2026-07-26: originally logged against `_atomic_write_text`
  forcing `newline="\n"`. That is not a bug — Python's `open()` performs no
  translation on write for `newline="\n"`, and CRLF passes through intact. The
  damage is entirely in the `frontmatter.loads`/`dumps` round-trip.)*
  `vault/writer.py:add_tags` round-trips the whole document through PyYAML.
  Verified on one small file, that single call: collapsed CRLF to LF on every
  line, expanded flow-style `tags: [a, b]` into block style, **deleted a YAML
  comment outright**, and dropped the trailing newline. On a Windows-synced
  vault every `daemon link` run turns a one-tag edit into a whole-file diff,
  and comments are lost permanently.
  **Fixed 2026-07-26 (M-mem-8).** `add_tags` now delegates to
  `add_frontmatter_list_values_textual`, which appends in whatever style is
  already there — flow stays flow, block stays block at its own indentation, a
  scalar becomes a flow list — and leaves every other byte alone.

- [x] **`neighbors_within` was called with a path after the UUID cutover.**
  Found 2026-07-26 while auditing the theme self-reinforcement loop.
  `pipeline/agent_link.py` and `integration/core.py` still passed a
  vault-relative path where nodes had become `note::<uuid>`, so both hit the
  "not in graph" early return and silently yielded `{}`. **`daemon link`'s tag
  suggestions and the Hermes `neighbors` tool were dead.** The test that should
  have caught it asserted only `isinstance(result, list)`, which passes on
  empty. **Fixed** — both resolve identities properly, `neighbors()` reports
  `ok: False` for an unknown path instead of an empty list, and the tests now
  assert real neighbour values.

- [x] **Frontmatter-only edits were invisible to ingest.** Also found
  2026-07-26. The identity cutover changed the skip check from `mtime` to
  `body_sha256`, so editing only frontmatter — every tag added in Obsidian,
  every theme tag accepted — never reached the graph, the registry, or the
  payload until `daemon ingest --full`. **Fixed** by hashing frontmatter
  separately and adding a *metadata refresh* path: differential graph update
  plus a registry refresh, with no chunking and no embedding. Reported as
  "Notes metadata-refreshed" in the ingest summary.

- [ ] **Activation rows recorded before the theme-tag exclusion shipped.**
  Any activation logged before 2026-07-26 may have been derived through a
  daemon-authored tag edge, and still clusters at full weight. Not worth
  invalidation machinery today — the ledger is effectively empty and the
  `memory.lookback_days` window (180d) ages them out. Revisit only if a real
  ledger predates the fix.

The 2026-07-26 five-agent maturity evaluation
([MATURITY-EVALUATION-2026-07-26.md](MATURITY-EVALUATION-2026-07-26.md), §7)
found thirteen more. Status after the same-day fix waves:

- [x] **Reinforcement crashed all retrieval** — float Dijkstra distances vs
  `RetrievedChunk.graph_distance: int`. The adaptive loop had never closed.
  Fixed: field widened to float, `is_seed_distance()` owns seed semantics,
  seam test added (`tests/test_expand_seam.py`).
- [x] **`daemon select` raised TypeError on every invocation** (dead
  `selected_note_path` kwarg from the UUID cutover). Fixed + 20 CliRunner
  tests (`tests/test_cli.py`).
- [x] **`daemon ask` silently never synthesized** (truthy Typer `OptionInfo`
  sentinels). Fixed via shared `_run_query()`.
- [x] **Hermes ambient prefetch classified as intentional** — new
  `hermes_prefetch` ambient surface; prompt-boundary filter for answer-less
  rows. Feedback rows kept (endorse + weight replay read them).
- [x] **Themes never reached the observer letter** — phase order now
  snapshot → analyze → themes → letter → decay; `observer_letter(themes=)`.
- [x] **Missing config silently became defaults** (broke cron/schtasks) —
  `ConfigNotFoundError`, search order, config-dir-anchored paths.
- [x] **Non-atomic, unlocked graph/manifest writes** — atomic replace +
  inter-process lock + `GraphStore.transaction()` at all three
  read-modify-write sites; `GraphCorruptError` with recovery hint.
- [x] **`daemon reset` footgun** — config-aware, `--models`/`--all` tiers,
  refuses live server storage; plus new `daemon backup`/`restore`.
- [x] **`config.example.yaml` shipped `agent.enabled: true`** — now matches
  the code default (false) after the example rewrite for embedded Qdrant.
- [x] **`ActivationLedger.compact()` would destroy fingerprints** (nothing
  reads `fingerprint_json`). **Deleted 2026-07-29** — zero callers, so removal
  cost nothing and took away a loaded gun. Column retained; see Done for what
  must be built first if compaction ever becomes necessary.
- [x] **`apply_selection` can route through theme-tag hubs**
  (`shortest_note_path` has no tag exclusion). **Fixed 2026-07-29** — the same
  `exclude_tag_prefixes` severance `neighbors_within` already applied. Loop
  severance is now complete on both the retrieval and the learning side.
- [x] **`similar()` IDF asymmetry** (probe weighted, candidates not) —
  eval §2 "3.6". **Fixed 2026-07-28**; see Done.
- [ ] **`agent_link_runs`/`agent_extract_runs` still path-keyed** after the
  UUID cutover — a rename re-runs the extractor/linker.

## Other Planned Work

Separate from the Adaptive Memory Loop arc:

- [ ] Light setup UI for `ANTHROPIC_API_KEY` + a "compose docker" button.
  *(Partly obsolete since 2026-07-26: embedded Qdrant is the default, so
  Docker is optional; the setup UI ask shrinks to key + vault + first ingest.)*
- [ ] Multi-embedding spaces.
- [x] ~~Obsidian plugin /~~ file watcher — **shipped 2026-07-26** as
  `daemon run` (watchfiles debounced ingest + nightly consolidate) plus
  `daemon schedule` (systemd --user / schtasks units). An Obsidian *plugin*
  remains unplanned.
- [x] ~~`daemon graph todos`~~ — **shipped 2026-07-29**. Dangling wikilink
  targets as "notes you keep meaning to write", ranked by how many notes are
  waiting on each.
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

- **The policy ledger is actually wired — step 0 of the 2026-07-31 sequencing
  (2026-07-31).** §IV.7 shipped on 2026-07-28 and had **never once run in
  production**. Two independent breaks, both closed here, and the second was
  found while fixing the first:
  **(1) No impressions, on any surface.** `PolicyRecorder` was only ever
  attached by `integration/wiring.py`'s `build_orchestrator`, which `grep`
  confirms had **zero callers under `src/`**. Every real query went through
  `pipeline/query.py`'s `QueryEngine`, whose orchestrator was hand-built with
  `listeners=[ActivationRecorder(...)]` and nothing else — and since
  `DaemonCore` delegates to `QueryEngine`, the CLI, the GUI and Hermes all
  inherited the gap from that one place.
  **(2) No wins on the CLI.** `DaemonCore.endorse` has recorded
  `policy.record_win(team)` since it was written; **`daemon select` never did**.
  So even with impressions fixed, a CLI user's picks would still have been
  invisible. Worse, the two halves failing separately was self-concealing:
  `PolicyStat.win_rate` returns 0.0 when impressions are zero, so a Hermes user
  recording wins against no impressions would have read a table of confident
  0.0% rows rather than an obvious error.
  **The fix is structural, not a reminder.** New `pipeline/listeners.py` with
  one `build_listeners(ledger, policy, record=True)` that both construction
  paths call, so there is no second place to forget. It lives in `pipeline/`
  rather than in `wiring.py` for an import reason worth recording:
  `integration/__init__` imports `core`, which imports `pipeline.query`, so
  `query.py` importing `integration.wiring` would close a cycle — `wiring.py`
  already depends on `pipeline`, and this keeps the arrow pointing the way it
  already pointed. The two recorders stay separate objects (they answer
  different questions and must fail independently) but are switched together,
  because a `daemon search` probe that counts as an impression is exactly as
  dishonest as one that counts as an activation.
  **The missing-team discipline is preserved on the new path**: `daemon select`
  records a win only when the persisted candidate carries a `team`, so a pick
  from a score-ordered or pre-§IV.7 pool is skipped rather than assigned to a
  default — pinned by its own test.
  **Where the tests live is the lesson.** `tests/test_wiring.py` was green
  throughout because it called `build_orchestrator` directly; a factory tested
  in isolation proves nothing about whether production reaches it. The new
  tests assert on the path production takes — that `QueryEngine`'s orchestrator
  carries a `PolicyRecorder` pointed at the shared state DB, and, end to end
  through `CliRunner`, that `daemon query` records an impression and `daemon
  select` records the win.
  **Verified live, not only by test.** On the author's vault: `daemon query
  "what is a daemon" --no-llm -v` → `feedback_id=7`, then `daemon policy`
  printed real rows for the first time (`expansion 15`, `rerank 15`, `seed 7`
  impressions) where it had always printed "No picks recorded yet"; then
  `daemon select 7 2` → `seed` at 1 pick, 14.3%. The pick reinforced **0 edges**
  — a seed pick, precisely the case §IV.7 exists to capture and the one that
  used to vanish without trace.
  895 → 901 tests; ruff and mypy clean. **Note:** that live verification wrote
  one real feedback row and one synthetic `seed` win into `data/state.db`; see
  the AI-suggestions entry for 2026-07-31 for why that one row is worth
  deleting before the win rates are read seriously.

- **Cross-encoder reranking as a third drafting team, §IV.18 (2026-07-30).**
  Closes the largest capability gap the competitor review found:
  `RetrievalOrchestrator._pool` used to sort candidates on `combined_score`
  alone — a hybrid RRF score for seeds and a decayed graph distance for
  expanded ones, two quantities on different scales compared directly.
  `retrieval/interleave.py`'s `multileave` (already generalised to n rankings
  by the prior task) now takes a third ranking: a local cross-encoder
  (`retrieval/rerank.py`'s `CrossEncoderReranker`, also already built) scores
  the pooled candidates and its ordering enters the draft as `RERANK_TEAM =
  "rerank"`, competing against the seed and expansion rankings rather than
  replacing either. `RetrievalOrchestrator(..., reranker=None)` is the last
  constructor parameter, keyword-capable and defaulted, so every existing
  construction site kept working untouched — except `pipeline/query.py`'s
  `QueryEngine` and `integration/core.py`'s `DaemonCore`/`build_core`, which
  needed a `reranker=` parameter threaded through since they each hand-build
  their own orchestrator rather than going through
  `integration/wiring.py`'s `build_orchestrator` (see the finding below).
  New config: `retrieval.rerank` (default **false** — enabling it without the
  weights cached would download on first query and break the offline
  guarantee), `retrieval.rerank_model`
  (`cross-encoder/ms-marco-MiniLM-L-6-v2`), `retrieval.rerank_max_candidates`
  (100 — the cross-encoder is O(candidates), unlike the bi-encoder retrieval
  it reorders). `daemon doctor` gained a `reranking` check so off-by-default
  does not mean invisible: disabled reports *"available but disabled — set
  `retrieval.rerank: true` and run `daemon models download` to A/B it via
  `daemon policy`"*; enabled-but-cold names the download; enabled-and-cached
  passes. `daemon models download` and `integration/wiring.py`'s
  `build_stores` pull/construct the reranker the same way they already do for
  the dense and sparse embedders, sharing `embeddings.cache_folder`.
  `combined_score` is **never** overwritten by a rerank score — pinned by a
  dedicated test that checks the stored value, not just the ordering — because
  it is persisted in the retrieval summary and feeds the activation ledger's
  rank-based strength. Rerank latency is measured around the `rank()` call
  alone and surfaced as its own field (`RetrievalResult.rerank_ms`), printed
  by `daemon query -v` as `rerank_ms=<n>` rather than folded into the total,
  so a slow reranker stays attributable to itself. `daemon policy` now prints
  how many teams are currently drafting (2 or 3) with a note that win rates
  gathered under a different team count are not directly comparable — turning
  reranking on resets how much the existing §IV.7 policy history means to
  read. Ten new tests (`tests/test_orchestrator.py`,
  `tests/test_doctor.py`) plus the two behavioural properties above; full
  suite 895 passed / 1 skipped / 5 deselected (was 883/1/5 before this task,
  and a further 1 test added by the final-review fix wave); ruff and mypy
  clean.
  **Latency, measured for real** (not invented), on the author's 130-note
  vault, CPU-only, `cross-encoder/ms-marco-MiniLM-L-6-v2`, default
  `rerank_max_candidates: 100` — **cold and warm separately, because they
  differ:** **cold** (`daemon query`, a fresh process each time — includes
  the one-time `sentence-transformers`/torch import and weight load) ran
  **~4.1–4.2s** (`rerank_ms=4116`, `4227` across two live `daemon query -v`
  runs after `daemon models download`). **Warm** (orchestrator built once,
  `retrieve()` called repeatedly in-process — the shape a persistent session
  like `daemon chat` or Hermes actually has) ran **~2.5–3.4s** at the same
  100-candidate cap (`rerank_ms=3394`, `2518`), and ~1.7s for a query with
  only 37 candidates to score — confirming the cost scales with candidate
  count as expected. Warm is meaningfully cheaper than cold (load is a real
  fraction of the cold number), but **still multi-second at the default
  cap** — this is scoring cost, not import overhead, and it does not
  disappear once the process is warm. Slow enough either way that "off by
  default" is the right call for interactive use as shipped, and that
  `rerank_max_candidates` is the first knob to reach for, not a theoretical
  one.
  **Found along the way, not fixed here:** `QueryEngine` and
  `DaemonCore.build_core` each hand-roll their own `RetrievalOrchestrator`
  instead of going through `integration/wiring.py`'s `build_orchestrator` —
  the exact drift that module's own docstring says it exists to prevent. The
  practical effect: `PolicyRecorder` (§IV.7) is only ever attached by
  `build_orchestrator`, which nothing under `src/` actually calls, so real
  `daemon query` usage may never have been recording policy impressions at
  all — `daemon policy`'s win rates may be reading an empty ledger regardless
  of this task. This task threaded `reranker=` through both hand-rolled sites
  so reranking would actually run on a live query (verified above), but left
  the `PolicyRecorder` gap itself alone as out of scope. See the AI
  Suggestions entry below.

- **Opt-in live-LLM prompt-compliance fixtures, §IV.17 close-out (2026-07-30).**
  §IV.17's five `SYSTEM_PROMPT` rules (conditional reconciliation audit,
  update-vs-contradiction split, anti-arithmetic, abstention contract,
  `(undated)`/`[date~]` handling) were previously pinned only by *structural*
  tests — text-is-present checks that can't tell whether the model actually
  obeys them. `tests/test_live_prompts.py` adds five behavioural fixtures
  against the real Anthropic client (via `LLMClient`, not a raw `anthropic`
  client), driven by a new `live_answer` fixture in `tests/conftest.py` that
  builds real `RetrievedChunk`s from `(note_path, occurred_at, text)` tuples
  and resolves the API key through `secrets.resolve_api_key` — skipping with a
  clear message rather than failing when no key is available.
  Deliberately **not** part of CI or the default run: they cost money and are
  non-deterministic. `pyproject.toml` gained a registered `live_llm` marker and
  `addopts = "-m 'not live_llm'"` so `pytest -q` deselects them by default;
  invoke explicitly with `pytest tests/test_live_prompts.py -m live_llm -v`.
  Their value isn't a green tick — it's that the cases exist and can be
  re-run against a new model, since a model upgrade is exactly when prompt
  compliance changes silently (as already happened once with Opus 4.7 and
  `temperature`).
  No Anthropic API key was available in this environment, so the by-hand run
  produced 5 skips with the informative message rather than a pass/fail
  verdict — see `.superpowers/sdd/2026-07-30-time-binding-reconciliation-reranking/task-12-report.md`
  for the full accounting. 870 passed / 1 skipped / 5 deselected — the default
  count is unchanged from before this task.

- **Episodic time-binding, §IV.10 (2026-07-30).** Closes Part III.3: `Chunk`
  now carries an honest `occurred_at` (parsed from frontmatter, else filename,
  else file mtime as a last resort) plus `occurred_at_source`, and query-time
  filtering can bound retrieval to a date range on it. The synthesis prompt's
  `build_context_block` (`llm/prompts.py`) now shows the model that date
  instead of our internal ranking machinery: `[YYYY-MM-DD]` for a stated date,
  `[YYYY-MM-DD~]` for one inferred from mtime (the tilde is the model's cue
  that it's a guess, not a fact from the note), and `(undated)` — explicitly,
  not silence — when nothing is derivable. `score=`, `vector=`, and
  `graph_distance=` are gone from the block entirely: they were facts about
  retrieval, not the world, and no prompt instruction ever consumed them.
  The hard part was upstream of this task — deciding what counts as a stated
  date at all. `occurred_at` and `modified_at` are kept strictly separate and
  never OR'd at query time (conflating "happened then" with "was written
  then" is what makes a naive date filter meaningless), partial dates are
  rejected rather than scored at some midpoint, and everything normalizes to
  UTC before comparison.
  **Honest limitation:** about half of a typical vault carries no derivable
  date at all — file birth time was measured and rejected as a fallback (28-day
  median error, since copying a file resets it while preserving mtime), and raw
  mtime recovered only 9 of 67 undated notes on the author's own vault. The
  natural-language → `DateRange` conversion ("last spring") is deliberately
  left to the model rather than built here, so a query like "what was I
  working on last spring?" still needs that one translation step before the
  date filter can act on it.
  859 → 865 tests.

- **License compliance is a CI gate (2026-07-29).** `scripts/license_check.py`
  now runs in `.github/workflows/ci.yml` with `--strict`, so a dependency with
  an incompatible *or unrecognised* licence turns the build red. CLAUDE.md
  rule 10 is enforced by the build rather than by memory.
  The blocker was that the tree has 17 genuinely-incompatible packages (the
  NVIDIA CUDA runtimes, transitive via torch), so a naive wiring would have
  been red on every commit forever — and a permanently-red check is worse than
  no check, because it looks like coverage while nobody reads it. So the script
  gained a fourth verdict, **`EXCEPTED`**: incompatible, accepted for a written
  reason, never fails the build, and **listed by name with its rationale in
  every report**. An exception nobody can see is one nobody can challenge.
  `_ACCEPTED_EXCEPTIONS` is deliberately a *separate table* from
  `_VERIFIED_OVERRIDES` because they make opposite claims — an override says
  the declared licence is wrong, an exception says it is right and we are
  living with it. Collapsing them would let a real incompatibility hide behind
  a word meaning the opposite.
  Two remaining UNKNOWNs resolved by reading the shipped licence text:
  `py_rust_stemmers` declares no licence metadata at all but bundles the full
  MIT text (Qdrant's), and `cuda-toolkit` is an NVIDIA meta-package with no
  metadata. Tree is now **148 compatible, 0 incompatible, 0 unknown, 17
  excepted** — clean under `--strict`.
  Also fixed before it shipped: the report drift-check compared whole files,
  which embed a generation timestamp, so it would have failed on every run. It
  compares `findings` via `jq` instead. And a stray `scripts/debug/` copy of
  the report (from someone running the script with `cwd=scripts/`, since the
  default paths are relative) was tracked in git since May; deleted.
  774 → 789 tests.

- **`daemon key set | clear | status` (2026-07-29).** The credential store is
  now reachable without a desktop. `daemon setup` is a Tkinter window, so a
  server, an SSH session, or a container — the machines most likely to be
  running `daemon run` on a schedule — had no route to the encrypted store at
  all, and the honest workaround was the environment variable this project
  deliberately moved away from.
  **The key never travels through argv.** No `--key` flag, and a positional
  argument is rejected: arguments are visible in `ps`, in shell history, and on
  Windows in Event 4688 process-creation logs, which is precisely the leak that
  got `setx` removed in the 2026-07-27 rewrite. `set` prompts without echoing,
  or takes `--stdin` for `pass show anthropic | daemon key set --stdin`.
  **`status` prints a SHA-256 fingerprint, not the last four characters** —
  those are part of the secret, and this output lands in scrollback and in
  pasted support threads. The fingerprint is what makes "did my rotation take?"
  answerable without exposing anything.
  Three failure modes handled deliberately: a store failure exits non-zero
  rather than reporting success (believing a key was saved when it was not is
  how a scheduled job breaks silently); the legacy plaintext `.env` is stripped
  **only after** the store succeeds, since purging the last surviving copy of a
  key you failed to save would lose it outright; and an `ANTHROPIC_API_KEY` in
  the environment is called out, because it takes precedence and would
  otherwise shadow the key you just stored. An unexpected key prefix warns but
  is accepted — the format is Anthropic's to change, and refusing a valid key
  is worse than accepting one the API rejects in a second.
  `doctor`'s two API-key hints and `getting-started.md` now point at
  `daemon key set` rather than at a GUI a headless reader cannot open.
  757 → 774 tests.

- **Five cheap wins + one deletion (2026-07-29).** TDD throughout;
  730 → 757 passing tests, ruff + mypy clean.
  **`daemon graph todos`** — the dangling-wikilink idea from the 2026-05-16
  suggestions, finally reachable. New `analysis/todos.py`: every `[[Name]]`
  with no note behind it, ranked by how many notes are waiting on it, and
  naming them so the entry is actionable rather than a bare noun. This is the
  one place the daemon touches **prospective memory** — remembering to do a
  thing you decided to do, a distinct system from the episodic and semantic
  memory everything else here serves, and one that degrades early in ageing
  and MCI.
  **Reinforcement no longer routes through theme tags** — `shortest_note_path`
  gained the `exclude_tag_prefixes` severance `neighbors_within` already had,
  and `apply_selection` passes it. Was latent until a theme tag is accepted;
  after that, any two notes sharing a daemon-authored tag looked adjacent and
  every pick between them warmed edges the daemon minted from its own
  conclusions. Closes the remaining half of the loop severance.
  **Louvain's disconnected communities are repaired** — new
  `structural.split_disconnected` splits any community whose induced subgraph
  isn't connected. Traag et al. (2019) measured up to 16% disconnected; here
  that meant the observer letter could assert a relationship between notes with
  no path between them. Extracted as a pure function rather than a `_partition`
  test hook, so no test-only parameter leaked into production.
  **Theme matching is optimal, not greedy** — `scipy.optimize.linear_sum_assignment`
  (Hungarian, Kuhn 1955) replaces greedy best-first in `reconcile_themes`.
  Greedy can strand a cluster whose only viable partner a higher-scoring pair
  just claimed, and every stranding is user-visible twice — as a theme
  appearing from nowhere and another going dormant for no visible reason. The
  threshold is applied *after* assignment so no rejected pairing is smuggled in.
  **`compact()` deleted** — it wrote a truncated fingerprint to
  `queries.fingerprint_json` then deleted the detail rows, but *nothing reads
  that column*: `fingerprint()` and `similar()` both rebuild from the detail
  rows, so calling it would have silently destroyed what recall and themes run
  on. Zero callers, so deletion cost nothing. The column stays, with a note on
  what has to be built first if compaction ever becomes necessary.
  **`pip-licenses` in the dev extra — and the checker actually runs now.**
  It resolved the tool with `shutil.which`, which searches only `PATH`, so
  running it the way everything else here runs (`.venv/bin/python scripts/…`)
  reported "not installed" with the tool sitting in that very venv. *That* is
  why `debug/license-compliance.md` had not been regenerated since 2026-05-17 —
  a false negative, which is worse than no checker, because it reads like a
  clean bill of health. Fixed (`find_pip_licenses`, interpreter's bin first,
  `PATH` still honoured) and the report regenerated: **147 compatible, 16
  incompatible, 2 unknown**. The 16 are the documented NVIDIA/CUDA proprietary
  binary exception.
  **Found while doing it: `fastembed` was flagged incompatible** — a *direct*
  dependency. Its PyPI Trove classifier says `Other/Proprietary License`, but
  its `License` field says "Apache License" and the wheel ships the full
  201-line Apache-2.0 text. The classifier is simply stale. Added a
  `_VERIFIED_OVERRIDES` table that carries the *evidence* into the report,
  precisely so an override can never be mistaken for suppressing a finding.

- **Applied-research fix wave (2026-07-28).** The first three recommendations
  out of [MY-DAEMON-RESEARCH-APPLIED.md](MY-DAEMON-RESEARCH-APPLIED.md),
  TDD throughout; 683 → 730 passing tests, ruff + mypy clean.
  **(1) The `similar()` IDF asymmetry is fixed** (§IV.1). `record()` stored a
  pre-IDF `l2_norm` while `similar()` divided an IDF-weighted dot product by it,
  which systematically inflated queries built from rare notes and poisoned
  recall → themes → the observer letter. The fix is structural, not a
  coefficient: a candidate's magnitude cannot be derived from the notes it
  shares with the probe, so `similar()` now runs two passes and weights both
  sides through one new public `ActivationLedger.idf()`. Pinned by a
  self-similarity test (identical fingerprints score 1.0) and a symmetry test
  (score(A→B) == score(B→A) across unequal document frequency) — the first
  version of both passed against the *broken* code because the fixture gave
  every note the same df and the IDF factors cancelled. Scores are now true
  cosines and generally lower than before, so `memory.min_score` (0.15) may want
  revisiting against real usage.
  **(2) The seed-starvation question is closed** (§IV.7) — team-draft
  interleaving, `retrieval.interleave` on by default. New
  `retrieval/interleave.py`; `RetrievedChunk.team` persisted through
  `build_retrieval_summary` so a pick attributes days later; new
  `stores/policy.py` + migration 6 (`retrieval_policy_stats`); new
  `pipeline/policy.py` listener on the existing retrieval seam (separate from
  `ActivationRecorder` — different questions, independent failure); `daemon
  policy` to read the win rates. **A seed pick is no longer a no-op:** it still
  reinforces no edge, but it is recorded as a win for the seed *policy*, which
  is an unbiased comparison because the draft gave both rankings symmetric
  exposure. Nothing feeds the win rate back into ranking — a policy tuned on its
  own win rate is a closed loop. Zero-count impressions are skipped and a
  missing team is never guessed, both so the measurement stays honest.
  **(3) The theme match threshold is measurable** (new §IV.15) —
  `analysis/theme_tuning.py` + `daemon themes tune` replays your ledger in
  sequential windows at a range of thresholds and reports mean churn, surviving
  themes, and created/matched/dormant. Read-only against the live theme store,
  and each threshold starts from an empty one. Needed `query_fingerprints(until=)`
  because bounding the window by filtering *after* clustering gives a different
  and wrong answer; and excludes the first window's churn, which is 0.0 only
  because there was no prior partition and whose inclusion made the score depend
  on `--windows`.
  Also: `retrieval.interleave` documented in both yamls. **Found, not fixed:**
  `python-louvain>=0.16` is a declared dependency that is never imported (the
  code uses NetworkX's built-in).

- **API key out of plaintext + Windows 11 install rewrite (2026-07-27).**
  The key was written to `.env` in the clear on *every* platform — including
  Windows, where `setx` had already stored it — and two of the three
  documented install paths never created a real env var at all. New
  `my_daemon/secrets.py` resolves the key **environment → OS credential store
  (`keyring`, MIT) → legacy plaintext `.env`**, and records *which* layer
  answered in `Settings.api_key_source`; `.env` is read directly rather than
  through `load_dotenv`, because once dotenv merges it into `os.environ` a
  plaintext key is indistinguishable from a safe one. `daemon setup` now
  writes to the credential store **and nowhere else**, migrates + strips any
  key it finds in a legacy `.env`, and fails loudly when no store exists
  rather than silently falling back to a file. `daemon doctor` warns on the
  plaintext case and names the source on pass; `setx` is gone (it also leaked
  the key through the process command line into event 4688). Docs: README and
  `getting-started.md` had contradicted each other on Docker (required vs.
  optional), on where the key goes, and on the Python band — now one numbered
  Windows 11 flow, with the `setup.bat`-generated launchers called out as
  generated. 631 → 663 tests, TDD throughout; keyring's tree verified MIT /
  BSD-3-Clause by hand (`pip-licenses` not installed, so
  `debug/license-compliance.md` is still the 2026-05-17 generation).

- **Maturity-evaluation fix waves, Arcs 1+2 (2026-07-26).** Five-agent audit
  ([MATURITY-EVALUATION-2026-07-26.md](MATURITY-EVALUATION-2026-07-26.md))
  followed by four fix waves, all Opus sub-agents, TDD throughout;
  358 → 624 passing tests. **Arc 1 (loop closure):** the three dead commands
  (float `graph_distance`, `select` kwarg, `ask` sentinel bug), the missing
  reinforce→expand seam test, `hermes_prefetch` ambient surface, themes into
  the observer letter. **Arc 2 (daemon envelope):** config search order +
  loud missing-config failure + config-dir-anchored paths (cron/schtasks now
  correct-by-construction), atomic graph/manifest writes + inter-process
  lock + `transaction()`, embedded Qdrant as default (Docker optional; local
  hybrid RRF verified by test), `daemon doctor` (8 checks + shared Qdrant
  error translation), defanged `reset`, `backup`/`restore`, `daemon run`
  supervisor + `daemon schedule`. **Hygiene:** uv.lock tracked, `.idea/` +
  `docs-site/` untracked, `[Ss]cripts` gitignore trap fixed (scripts/ was
  never tracked), GitHub Actions CI, mypy gate, repo-wide ruff format.

- **Memory refocus M-mem-0 → M-mem-4 (2026-07-26).** The identity spine and the
  activation ledger, per [PLAN-MEMORY.md](PLAN-MEMORY.md). **M-mem-0:** a real
  `PRAGMA user_version` migration ladder in `stores/db.py`, replacing the
  `PRAGMA table_info` hack; WAL, `busy_timeout`, and `foreign_keys` (all three
  were missing, and `foreign_keys` being off had silently made every
  `REFERENCES` clause inert). **M-mem-1b:** `GraphStore.update_note()` —
  differential re-ingest that stops every note save from deleting inbound
  wikilinks and resetting M1's learned edge weights. **M-mem-1a:** `uuid:` in
  frontmatter via a textual single-key writer that changes exactly one line,
  `vault/identity.py` (adoption of foreign id keys, deterministic derivation,
  path-derived fallback), the `notes`/`note_ordinals` registry, and
  `daemon migrate assign-uuids | list-runs | rollback-uuids`. **M-mem-2/3/6:**
  the identity cutover, collapsed into one change once a full re-ingest became
  acceptable — chunk ids from `note_uuid`, graph nodes `note::<uuid>` with a
  `dangling::<text>` namespace for unresolved links, `note_uuid` in the Qdrant
  payload with indexes on it and `note_path`, uuid-keyed content-hashed
  manifest, and renames handled as a payload update with no embedding and no
  weight loss. **M-mem-4:** the activation ledger (`queries`,
  `query_activations`, `note_activation_stats`), IDF-weighted fingerprints with
  inverted-index cosine, the `RetrievalListener` seam on the orchestrator, and
  `daemon activations` / `daemon hot-notes`. **M-mem-5:** fingerprint recall —
  a reworded question surfaces the earlier one it shares notes with, injected
  into the model's context and shown as a "You've been here before" panel (the
  two toggle independently). **M-mem-7:** emergent themes — HDBSCAN over
  fingerprint cosine inside `daemon consolidate`, with centroid matching, label
  locking, dormancy and an honest churn metric, so labels stay stable even
  though partitions over a mutating vault cannot. **M-mem-8:** theme tag
  proposals — accepted themes propose `theme/<slug>` tags, you decide, and
  accepted ones are written through a new textual list writer that `add_tags`
  now shares (closing its round-trip bug). Graph expansion refuses to walk
  `theme/` edges, which breaks the tags → edges → fingerprints → themes loop.
  **M-mem-9:** one construction path
  (`integration/wiring.py`), `DaemonCore.retrieve_only` / `log_answer` /
  `ask_stream`, the GUI fully onto `DaemonCore` — which is how it gained
  fingerprint recall it had silently never had — the reinforcement gate split
  (an explicit click is not the same act as ambient write-back), and
  `daemon migrate backfill-activations` to recover a ledger from the historical
  feedback log. **The whole M-mem arc is complete.** Suite 358 pass / 1
  pre-existing skip. **Requires `daemon ingest --full` once.**

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

### 2026-07-26 — After the maturity-evaluation fix waves

**What's deliberately still open** (evaluation Arcs 3–4, not started):
docs refresh (README/roadmap/cli.md still describe the pre-M1 world),
CONTRIBUTING.md, retiring commit-to-master before collaborators arrive,
version bump + tags, packaging config templates into the wheel, GUI
catch-up (session_id/history, themes + dream panels, status bar,
self-hosted fonts), one real Hermes-host validation, the librarian STATUS
banner + §L4 rewrite around UUID identity, finishing the theme-loop
severance, and the `similar()` IDF asymmetry.

**The deepest open design question is unchanged:** seeds dominate the
candidate pool and seed-picks reinforce nothing (`apply_selection` no-ops
when seed == selected). Now that the loop *can* close mechanically, decide
what "picking" should mean — reserve pool slots for expanded candidates,
reinforce seed retrieval features, or widen the pool with a damped seed
advantage — before scaling any more learning machinery. Watch
`daemon analyze` after a week of real use: if the weight distribution is
still uniform, this is why.

### 2026-07-27 — After moving the API key into the OS credential store

**Follow-ups this opened, none blocking:**

- **~~`daemon setup` is now the only supported way to set the key, but it is a
  Tkinter window.~~ — shipped 2026-07-29.** `daemon key set` / `clear` /
  `status`. Prompts hidden, `--stdin` for pipes, and the key is never accepted
  as an argument (argv is visible in `ps`, in shell history, and in Windows
  Event 4688 — the leak that got `setx` removed). `status` prints a SHA-256
  fingerprint rather than the last four characters, since those are part of the
  secret. `doctor` and the docs now point here instead of at the GUI, which
  headless readers could not follow.
- **Scheduled jobs are the weak spot.** Linux cron cannot reach the Secret
  Service; that is documented in `background-agents.md` now, but the honest
  fix is to point people at the systemd user unit (`daemon schedule` +
  `loginctl enable-linger`) as the default rather than crontab. Worth
  validating on a real machine before v0.2.
- **~~Nothing rotates keys.~~ — covered 2026-07-29.** `daemon key set`
  overwrites whatever is stored, so rotation is one command; the fingerprint in
  `daemon key status` is how you confirm it took. No separate `rotate` verb —
  it would have been an alias.
- **~~`pip-licenses` is not in the dev extra~~ — shipped 2026-07-29**, along
  with the reason it had never run (`shutil.which` searches only `PATH`) and
  the CI gate. CLAUDE.md rule 10 is now enforced by the build rather than by
  memory.
- **`config.yaml` is now genuinely secret-free**, which makes it a candidate
  for being committed as a real template — worth deciding before v0.2.

### 2026-07-28 — After the applied-research pass

Full write-up in
[MY-DAEMON-RESEARCH-APPLIED.md](MY-DAEMON-RESEARCH-APPLIED.md). The items that
turned into concrete work, in priority order:

- **~~⚠ Licence decision needed~~ — resolved, and my first answer was wrong.**
  The published fix for Louvain's badly-connected-communities defect (Traag et
  al., *Sci Rep* 2019 — up to 25% badly connected, up to 16% *disconnected*) is
  Leiden, and I reported that every Python implementation is GPL. That is
  incorrect. `leidenalg` (GPL-3.0) and `python-igraph` (GPL-2.0) are, but
  **`graspologic-native` is MIT** — verified on PyPI, v1.3.1, June 2026 — it
  implements Leiden and hierarchical Leiden in Rust, and it is what Microsoft's
  GraphRAG runs on. **No licence change is needed for correctness here.** The
  real constraint is Python versions: it ships wheels for 3.9–3.13, and this
  project declares `>=3.11,<3.15` with the librarian pinned `>=3.14`.
  Recommended sequence regardless: do the free fix first (split any disconnected
  Louvain community with `nx.connected_components`, ~10 lines, no dependency)
  and measure whether Leiden still earns its place. §IV.3.

- **~~Fix the `similar()` IDF asymmetry first~~ — shipped 2026-07-28.** See Done.

- **~~Team-draft interleaving is the answer to the seed-starvation question~~ —
  shipped 2026-07-28.** See Done. What remains open is deliberately *not* the
  mechanism but the interpretation: interleaving removes position bias, not
  sampling error, so the `daemon policy` win rates will be noise until there are
  well over twenty picks — and nothing should be wired to them before then.

- **Cheap wins queued:** MMR over the candidate pool before the token trim
  (§IV.2, no new deps); Hungarian matching via
  `scipy.optimize.linear_sum_assignment` instead of greedy theme reconciliation
  (§IV.4, scipy is BSD-3 and already transitive via scikit-learn); building the
  nightly `O(n²)` fingerprint distance matrix from an inverted index instead of
  a Python double loop, since most pairs share zero notes.

- **Personalized PageRank as an alternative expansion operator** (§IV.6) —
  `nx.pagerank(personalization=...)` needs zero new dependencies, and this is
  exactly the comparison `simulate_evolution` exists to make. Keep the integer
  hop budget as a pre-filter either way.

- **The amygdala gap is still the biggest one** (§III.1, §IV.9). Nothing in the
  system asks whether a note *mattered*; `STRENGTH_BY_SOURCE` describes which
  retrieval route found it, which is a fact about the machinery. Retrieval by
  recency and match is the wrong affordance for the assistive use case — a
  person with cognitive decline doesn't need help finding yesterday's note, they
  need help finding the one that mattered. The behavioural and structural
  components of a salience score are free from data already logged; the
  LLM-scored semantic component should be opt-in.

- **Three other honest gaps** worth keeping visible: no episodic time-binding
  (chunks carry no timestamp, so "what was I working on last spring?" isn't
  answerable by retrieval — §III.3); forgetting exists only for graph edges and
  only as one scalar, collapsing Bjork & Bjork's storage-strength /
  retrieval-strength distinction (§III.4); and the 30-day half-life applies one
  forgetting curve to a passing curiosity and a decade-long preoccupation alike
  (§IV.5 — `py-fsrs` is MIT, verified, if you want a learned version).

### 2026-07-30 — After comparing against Hindsight and Honcho

A feature-level review of two adjacent open-source projects, each read by an Opus
sub-agent against its source rather than its README:
[Hindsight](https://github.com/vectorize-io/hindsight) (Vectorize, **MIT**, 18.9k
stars, ~103k LOC engine) and [Honcho](https://github.com/plastic-labs/honcho)
(Plastic Labs, **AGPL-3.0** server, 6.3k stars). It produced §IV.17–§IV.23.

**Licences first, because they bound what is even possible.** Hindsight is MIT,
so its code may be copied here with attribution and its notice retained. Honcho's
server is AGPL-3.0 — **ideas only**; no vendoring, forking, or linking, and §13
extends copyleft to network use. Its SDKs (Apache-2.0) and CLI (MIT) are safe,
and calling a Honcho server over HTTP is fine. One trap: Hindsight's
`hindsight-api/README.md` states "Apache 2.0", which is **wrong** — the repo is
MIT. Do not cite that file.

**Honcho is not really a competitor.** It has no notion of a note, link, tag,
folder, or document; its docs tell you to model files as "messages from one peer",
which turns a vault into one person's monologue and yields conclusions *about the
author* rather than a queryable model of the subject matter. **Hindsight is** —
and it ships a first-class Obsidian plugin (~2,492 LOC TS) whose contract is
worth knowing because it is ours: one-way sync, vault canonical, mandatory
citations, conversations not stored by default, implicit `vault:` / `folder:` /
`created:` / `updated:` tagging.

**Where this project is genuinely ahead** — worth recording, because it is easy to
lose confidence reading two larger repos:

1. **The graph is human-authored.** Both competitors *derive* their relational
   structure from LLM extraction. This system reads the wikilinks and tags the
   person drew by hand and only *weights* them from behaviour. In a vault that is
   the highest-quality relational signal available, and nobody else uses it.
2. **Nobody else has behavioural learning at all.** And the negative evidence is
   striking: Hindsight ships `memory_units.access_count` — `NOT NULL DEFAULT 0`,
   **indexed DESC on both DB backends** — that no code reads or writes, plus a
   `Trend` (STRENGTHENING/WEAKENING/STALE) function imported by nothing. Two teams
   built the schema for adaptive memory strength and did not build the mechanism.
3. **Forgetting exists here and nowhere else.** Hindsight: no decay, no TTL, no
   eviction — stated as intentional, but `max_observations_per_scope` defaults to
   *unlimited*. Honcho: exhaustive grep confirms none. §III.4 is honest that ours
   is one scalar on edges only — but "limited" beats "absent".
4. **Snapshot-isolated consolidation.** `open_readonly()` handles that *raise* on
   write mean overnight analysis cannot contaminate live state. Honcho dreams
   against the live DB; Hindsight consolidates in place with destructive `UPDATE`
   and hard `DELETE`. And `simulate_evolution` can replay history against a shadow
   store — neither can A/B a retrieval change against its own past.
5. **Team-draft interleaving and the policy ledger.** Unmatched. Neither has any
   notion that its selection signal might be position-biased, and neither has the
   discipline in `stores/policy.py` of refusing to feed win rates back into
   ranking.
6. **Themes cluster query behaviour, not content.** Both competitors cluster
   facts. Clustering which sets of notes fire together when *this person* wonders
   about things is a different and more personal signal.
7. **Prospective memory.** `daemon graph todos` has no counterpart in either. For
   the assistive framing that is not minor — prospective memory degrades early in
   ageing and MCI, before the episodic recall everything else here serves.
8. **Licence compliance as a CI gate.** Neither has anything comparable.

**The one architectural gap: this system retrieves prose; both retrieve
propositions.** That is §IV.21, and it is the only item here that touches what
the project *is* rather than how well it works. Read its entry before starting —
extraction creates a second source of truth, which both this project and
Hindsight's own plugin contract refuse, and the recommended middle path is
narrower than full extraction.

**On their benchmark numbers — treat with care.** Hindsight's headline results are
**not reproducible from its own repo** (results gitignored, published from two
other repositories), its judge **defaults to the same model as the system under
test**, LongMemEval's *abstention* category — the one penalising hallucination —
is **not implemented**, CI runs a hand-picked 3-of-10 LoCoMo subset excluding the
conversation that times out, and its "independently reproduced by Virginia Tech
and The Washington Post" claim names institutions represented by the paper's own
co-authors. Honcho's published quality depends on a **proprietary fine-tune absent
from its repo**; the OSS default model is `gpt-5.4-mini` everywhere. Their own
deep-dive posts are considerably more honest than their READMEs — Honcho's
consolidation post lists three systems *above* it. The gap here is not quality, it
is that no number is published at all (§IV.23).

**Two documentation habits worth stealing.** Both repos carry unusually good
in-code rationale — Hindsight's comments cite issue numbers and measured
before/after latencies; Honcho's `CLAUDE.md` documents *invariants with reasons*
("never hold a DB session across external calls"; "never write through a read-only
AUTOCOMMIT session, because savepoints silently break"). This project already
writes in that register; it is reassuring that the two most mature projects in the
space do too.

**Deliberately not adopted**, so it is on record: Honcho's peer/`(observer,
observed)` domain model (no place for documents, links, or folders); Hindsight's
"TEMPR" branding (**zero code footprint** — no class, function, module, or config
key, and never expanded anywhere); Hindsight's `_infer_temporal_date`
first-date-in-body heuristic (accurate but its failure mode is dating a reference
note to a date discussed inside it); Honcho's enumeration and dedup prompt
procedures (fitted to LongMemEval categories, not to anything a vault owner asks);
and hosted reranker or embedding providers (13 in Hindsight — this project is
local-first and one local model is the whole requirement).

### 2026-07-30 — After wiring §IV.18, a construction-path gap worth its own task

While threading `reranker=` through every place a `RetrievalOrchestrator` gets
built (§IV.18), it became clear that `integration/wiring.py`'s
`build_orchestrator` — the function whose own docstring says "Everything that
needs stores goes through here now" — is never actually called from `src/`.
`pipeline/query.py`'s `QueryEngine` and `integration/core.py`'s
`DaemonCore`/`build_core` each still hand-build their own
`RetrievalOrchestrator`, exactly the drift `wiring.py` was written to end.

> **Corrected 2026-07-31 — the blast radius is smaller than this entry says.**
> Re-verified against the code: there are exactly **two** `RetrievalOrchestrator(`
> construction sites in `src/`, `wiring.py:129` and `pipeline/query.py:105`.
> `DaemonCore` does *not* hand-build one — it constructs a `QueryEngine`
> (`integration/core.py:105`) and exposes `self.engine.orchestrator` through a
> property (`:220-222`), whose docstring already says "one instance, one set of
> listeners." So there is a **single** production construction site to converge,
> not two, and every surface — CLI, GUI, Hermes — inherits the gap from that one
> place. The finding below is unchanged and still correct; only the estimate of
> what has to be untangled shrinks.

The concrete cost: `build_orchestrator` is the only place that attaches
`PolicyRecorder` (§IV.7) as a listener. `QueryEngine`'s orchestrator only gets
`ActivationRecorder`. **Confirmed live, not just by reading the code:** two
real `daemon query -v` calls on the author's vault (the ones used to measure
§IV.18's rerank latency below) each logged a feedback row, then
`daemon policy` still printed "No picks recorded yet" — the empty-table
message, which `RetrievalPolicyStore.stats()` only returns when
`retrieval_policy_stats` has *zero rows*, not merely zero wins. A recorded
impression with zero wins would still show a table row at 0.0%. So `daemon
query` / `daemon ask` — the actual command a person runs — has not been
recording team-draft impressions at all, and `daemon policy`'s win rates have
been reading an empty ledger since §IV.7 shipped, regardless of how many
queries have run since. `tests/test_wiring.py` passes because it tests
`build_orchestrator` directly; nothing exercises whether the CLI's query path
uses it.

I did not fix this as part of §IV.18 — it is a different item's bug, not this
one's, and untangling it touches `QueryEngine`'s and `DaemonCore`'s
construction surfaces (and needs someone to decide whether `QueryEngine`
should just delegate to `build_orchestrator` internally, or whether
`build_orchestrator` should absorb what `QueryEngine` does today). Suggest a
small, focused task: converge the construction paths so `wiring.py`'s "goes
through here now" claim is actually true, then re-verify with a real `daemon
select` → `daemon policy` round trip that impressions and wins both land.

### 2026-07-31 — Build sequencing for the next arc

**This entry supersedes
[MY-DAEMON-RESEARCH-APPLIED.md](MY-DAEMON-RESEARCH-APPLIED.md)'s own "If you are
choosing what to build next" section**, which was written on 2026-07-28 and is
now stale in a specific way: both of its top picks have shipped. Its "best
value-per-hour" was §IV.17 (conditional reconciliation, shipped 2026-07-30, with
the live fixtures still unrun) and its "largest capability gap" was §IV.18
(cross-encoder reranking, shipped 2026-07-30). The status *index* is still the
authority on **what** is left; this is the argument for **what order**.

One fact re-verified today changes the ordering, and it is not reflected
anywhere in that document: **the measurement instrument is dead.** Per the
2026-07-30 entry above, `grep` confirms `build_orchestrator` has zero callers in
`src/`, and `pipeline/query.py:110` attaches `listeners=[ActivationRecorder(…)]`
and nothing else. `PolicyRecorder` has never run in production. Everything
below except step 0 is a **ranking change**, and `daemon policy` is what says
whether a ranking change helped. It currently reads zero rows.

**0. Converge the orchestrator construction paths.** — **[x] done 2026-07-31,
see Done.** *Prerequisite, not a feature. Hours.* Make `QueryEngine` obtain its
orchestrator from
`build_orchestrator` so `wiring.py`'s claim is true and `PolicyRecorder` is
attached on the path a person actually runs. Verify by round trip — real `daemon
query` → `daemon select` → `daemon policy` showing a table row, not "No picks
recorded yet". Add the test that was missing: one asserting the *CLI query path*
carries both listeners, since `tests/test_wiring.py` only ever exercised the
factory nobody called. First because it is cheap, because §IV.18's new rerank
team is currently drafting invisibly, and because building §IV.20 on top of a
dead measurement path would repeat precisely the failure being fixed.

**1. §IV.23 — retrieval-quality evaluation harness.** — **design approved and
committed 2026-07-31**, see
[the spec](docs/superpowers/specs/2026-07-31-retrieval-eval-harness-design.md)
(`67a112d`); implementation not started, and it is gated on Evan hand-authoring
`<config-dir>/eval/questions.yaml` since the `oblique` and `multi-hop` questions
need vault knowledge. Four decisions locked there: vault-local tuning rather
than CI regression detection, hand-authored tiered fixtures, configuration
sweeps within one run rather than a committed baseline, and scoring at the
context block with wide recall alongside. *Effort M.* Questions with
known-correct source notes, scored on whether retrieval surfaced them, run
against a snapshot so results are comparable across commits. The rejection of
LoCoMo/LongMemEval in that item's entry stands and the reasoning is sound.
Sequenced second because 895 tests establish that the pipeline *works*, not that
it *retrieves well*, and because step 0 only restores the **online** signal —
which is slow, needs well over twenty picks before it is anything but noise (see
the 2026-07-28 entry), and cannot answer "did this commit help?" This is the
offline companion that can. §IV.20, §IV.2, §IV.6 and §IV.9 are all bets that
want a scoreboard, and there is not one.

**2. §IV.20 — activation stats into ranking.** *Effort S.*
`note_activation_stats` is populated and `daemon hot-notes` reads it; nothing
feeds it back into ranking. A bounded multiplicative term over data already
logged. Keep Hindsight's ±5% order of magnitude, keep the closed-loop caution in
that item's entry as the binding design constraint, and — now possible for the
first time — **measure** the effect through §IV.18's multileaved draft rather
than assert it.

**3. §IV.9 — the salience layer, behavioural and structural halves only.**
*Effort L, deliberately scoped down.* Revisit counts and endorsement history
from the ledger; betweenness from the structural report. Leave the LLM-scored
semantic component and Bayesian surprise out of the first pass, and keep the
semantic one opt-in when it comes, per that item's own recommendation. This is
the item that changes what the daemon is *for* rather than how well it works:
retrieval by recency and match is the wrong affordance for the assistive case,
where the need is not yesterday's note but the one that mattered.

**A dependency worth naming, because the research document does not.** §IV.20
and §IV.9's behavioural component read the **same table for the same purpose** —
they are one code path at two coefficients, not a small feature and a large one.
If §IV.20 is built with a config-driven weight rather than a hardcoded constant,
§IV.9's behavioural half arrives with it, and step 3 becomes "add the structural
term" instead of "build a subsystem". Sequence them adjacently and do not let
§IV.20 harden into a shape §IV.9 has to undo.

**Deliberately not next, so the reasoning is on record:**

- **§IV.21 (claim-level indexing)** — the largest open item and the only
  architectural one. Its entry says to expect brainstorming rather than
  implementation, it creates a second source of truth this project has twice
  refused, and it depends on §IV.22. Worth a design session; not a build slot.
- **§IV.19 (durable supersession)** — genuinely valuable, and the field is
  weak here, which makes it a real differentiator. But §IV.21 is what gives it
  an object to mark. Better after, not before.
- **§IV.2 (MMR) and §IV.16 (sparse distance matrix)** — both S, both
  dependency-free, both good filler between the larger items. §IV.2 is worth
  less than when first proposed, since §IV.7's interleaving already changed pool
  composition; what remains is stopping three chunks of one note filling the
  floor.
- **§IV.22 (chunk-level delta re-ingestion)** — its own entry is right that the
  saving is embedding cost only until an LLM enters the ingest path. Sequence it
  immediately before §IV.21, not here.

**Two open bugs to fold in opportunistically**, both already logged under Known
Bugs: incremental-vs-full ingest divergence when a linked note is deleted (which
also makes `daemon graph todos` under-count, so it is not purely cosmetic), and
`agent_link_runs`/`agent_extract_runs` still being path-keyed after the UUID
cutover, so a rename re-runs the extractor.

**The honest cost of this ordering:** steps 0 and 1 produce **no user-visible
feature** — roughly two sessions of pure instrumentation before anything new
appears. A defensible reordering is step 0 alone (hours) followed straight by
§IV.20; the price is that §IV.20's coefficient then gets tuned by impression
rather than by measurement, which is the thing this project has otherwise been
careful not to do.

### 2026-07-31 — After wiring step 0

**One synthetic row is now in the policy ledger, and it should probably come
out.** Verifying the fix end to end meant running a real `daemon select 7 2`
against the live vault, which recorded a genuine-looking `seed` win that
represents no actual preference — I picked rank 2 to exercise the code path, not
because that candidate was the better answer. It is one row against a ledger
that needs well over twenty picks before it says anything, so the distortion is
small; but this is the *honest-measurement* ledger, and the whole argument for
`stores/policy.py` refusing to feed win rates back into ranking is that the
numbers mean what they claim. Deleting it is one statement —
`DELETE FROM retrieval_policy_stats WHERE policy = 'seed'` would also drop its 7
impressions, so the surgical form is `UPDATE retrieval_policy_stats SET wins =
0, last_win_at = NULL WHERE policy = 'seed'`. I did not run it, because
destroying rows in the state DB is the user's call, not mine.

**The rerank team is live in the author's config.** Not obvious from the §IV.18
entry, which shipped `retrieval.rerank` defaulting to false: the local
`config.yaml` has it **on**, so the observed `rerank_ms=4040` is being paid on
every interactive query, and `daemon policy` reports 3 drafting teams. Worth a
deliberate decision rather than a default inherited by accident — the §IV.18
measurements say multi-second even warm.

**A measurement subtlety the first real table exposed.** The impressions came
out `expansion 15`, `rerank 15`, `seed 7` from a single query with 8 seeds and
333 expanded candidates. Seeds are structurally capped by `top_k` while
expansion and rerank draft from a far larger pool, so impressions are **not**
symmetric between teams even though the draft gives them symmetric *exposure per
slot*. That is fine for a win *rate* — the denominator is per-team by
construction — but it means the raw `shown` column is not a fairness check, and
anyone reading the table for the first time will be tempted to treat it as one.
Worth a line of help text in `daemon policy` at some point.

**What step 0 changed about step 1's design.** §IV.23's harness was framed as
the offline companion to `daemon policy`'s online signal. That framing survives,
but the online signal is now *newborn* rather than *accumulated* — there is no
history to mine, so the harness cannot be validated against "what the win rates
already told us". It has to stand on its own fixtures from day one.

