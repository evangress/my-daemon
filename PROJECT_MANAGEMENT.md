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

- **`daemon setup` is now the only supported way to set the key, but it is a
  Tkinter window.** A headless box has no way to store a key except the env
  var. A `daemon key set` / `daemon key clear` CLI pair (reading from stdin or
  a prompt, never argv) would close that gap and make the credential store
  reachable over SSH.
- **Scheduled jobs are the weak spot.** Linux cron cannot reach the Secret
  Service; that is documented in `background-agents.md` now, but the honest
  fix is to point people at the systemd user unit (`daemon schedule` +
  `loginctl enable-linger`) as the default rather than crontab. Worth
  validating on a real machine before v0.2.
- **Nothing rotates keys.** `doctor` tells the user to rotate after a
  plaintext exposure and links the console, but a `daemon key rotate` that
  re-prompts and overwrites would make the remediation a single step.
- **`pip-licenses` is not in the dev extra**, so `scripts/license_check.py`
  cannot run without a manual install and `debug/license-compliance.md` has
  drifted since 2026-05-17. Adding it to `[project.optional-dependencies].dev`
  and running it in CI would make CLAUDE.md rule 10 self-enforcing instead of
  a habit.
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

