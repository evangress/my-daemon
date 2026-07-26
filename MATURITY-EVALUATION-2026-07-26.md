# My Daemon — Maturity Evaluation

**Date:** 2026-07-26
**Repo state:** `master` @ `f70ea7d` (clean), 358 tests passing / 1 skipped, ruff clean
**Method:** Five parallel Opus audit agents, each owning one domain: (1) shipped-vs-claimed inventory, (2) memory-loop substance, (3) install & daily-use story, (4) engineering quality, (5) secondary surfaces (librarian / Hermes / GUI). Every bug cited below was **reproduced against the code, not inferred**.

---

## Executive summary

**Verdict: strong R&D core — craft is above-average for an alpha — but pre-alpha as a product.** The gap is almost entirely in the envelope (install, config, automation, process), not in the ideas or code quality.

The author's stated fear — "components just going through the motions and not doing substantive interlaced work" — is **half right, in an unexpected direction**. The interlacing is real and unusually thoughtful: activations genuinely feed theme clustering, theme tags genuinely round-trip through the vault back into ingest, structural analysis genuinely feeds the observer letter. But the one loop the system is named for — *learn from what the user picks* — **has never successfully run end-to-end.** It is severed at a seam between two modules that are each individually well-tested and never tested together.

### The three dead commands

| Bug | Location | Effect |
|---|---|---|
| Reinforcement crashes retrieval | `retrieval/expand.py:71` ← `stores/graph.py:244` | Float Dijkstra distances hit `RetrievedChunk.graph_distance: int \| None` (`models.py:119`). Uniform weights coerce (1.0, 2.0); the first reinforced edge makes distances fractional (reproduced: `0.666…`) → `ValidationError` kills every query seeded near that note. One GUI endorse click poisons the graph permanently. |
| `daemon select` crashes unconditionally | `cli.py:301` | Passes `selected_note_path=`, a kwarg `apply_selection` (`retrieval/weights.py:45-54`) no longer has after the UUID cutover. `TypeError` on every invocation. Mypy catches this today; mypy is a declared dev dep that never runs. |
| `daemon ask` never calls the LLM | `cli.py:257-260` | Forwards `query()`'s Typer `OptionInfo` defaults as values; they are truthy, so `synthesize` resolves False and `verbose` True. Prints candidates, no answer, no error. |

**Consequence of the first two combined:** every downstream consumer of learned weights — warm edges, weight-evolution replay, the "what the graph learned" half of the observer letter — has been analyzing a graph that is almost certainly still uniformly `weight=1.0`.

**Shared root cause:** the path→UUID cutover changed contracts at module seams; each side's tests kept passing against its own old assumptions. **Shared blind spot:** `cli.py` is 33% covered (494/735 statements untested) and no test feeds `neighbors_within` output into `expand_from_seeds`. This is the same failure mode as the already-fixed `neighbors_within` bug — the weak assertions were fixed, the untested-seam gap was not.

---

## 1. What is actually shipped (the inventory)

Full suite: `358 passed, 1 skipped in ~11s` (skip = `test_e2e.py`, gated on `MY_DAEMON_E2E` — and running it as the README documents fails with `NotImplementedError`).

### Shipped and reachable (code + entry point + tests all verified)

| Feature | Entry points | Tests |
|---|---|---|
| M1 — Adaptive edge weighting | `daemon select` (dead, see bugs), GUI endorse → `core.endorse`; Dijkstra-over-`1/weight` expansion | `test_weights.py` |
| M2 — Snapshot mechanism | `daemon snapshot create\|list\|delete\|prune` | `test_snapshot.py` |
| M3 — Structural analysis | `daemon analyze` | `test_structural.py` |
| M4 — Observer LLM | `daemon consolidate` (double-gated `agent.enabled` + `observer_enabled`) | `test_observe.py` |
| M-mem-0 — DB migration ladder | `daemon migrate db\|status` | `test_db_migrations.py`, `test_cli_migrate.py` |
| M-mem-1a — UUID backfill | `daemon migrate assign-uuids\|list-runs\|rollback-uuids` | `test_note_identity.py`, `test_migrate_uuids.py`, `test_note_registry.py` |
| M-mem-1b — differential graph update | `GraphStore.update_note`, used by both ingest paths | `test_graph_update.py` |
| M-mem-2/3/6 — identity cutover | `note::<uuid>` / `dangling::<text>` nodes, `note_uuid` payload + index | `test_uuid_cutover.py` |
| M-mem-4 — activation ledger | `daemon activations`, `daemon hot-notes`; listener seam in both construction paths | `test_activation_ledger.py`, `test_retrieval_seam.py` |
| M-mem-5 — fingerprint recall | CLI panel + GUI "You've been here before" strip | `test_recall.py` |
| M-mem-7 — emergent themes | dream phase of `daemon consolidate`; `daemon themes list\|accept\|reject` | `test_themes.py` |
| M-mem-9 — one construction path | `integration/wiring.py`; `daemon migrate backfill-activations` | `test_wiring.py`, `test_core_answer_path.py`, `test_activation_backfill.py` |
| Hermes H1–H3 — memory provider | `hermes/provider.py` + `plugins/memory/my-daemon/` shim; `daemon hermes doctor`; off by default | `test_hermes_provider.py`, `test_integration_core.py` |
| Background agents (extract/link/reflect) | `daemon extract\|link\|reflect`, gated `agent.enabled` | covered via pipeline tests |
| Hybrid retrieval (dense+sparse RRF) | `embeddings.hybrid` toggle, server-side Qdrant RRF | via retrieval tests |

### Partial, doc-only, or dead

- **M-mem-8 theme tag proposals — partial.** Core + CLI shipped (`daemon themes review`, loop-severance tests), but the GUI panel PLAN-MEMORY §7 specifies does not exist (`grep theme src/my_daemon/gui/` → nothing).
- **`librarian/` — doc-only (vaporware).** Exactly two files, both markdown (`CLAUDE.md`, `LIBRARIAN-PLAN.md`), untouched since 2026-06-05. Zero `.py`, no entry point, no config block. Root `CLAUDE.md` describes it as live code running as a separate process — a collaborator will look for source that doesn't exist.
- **Hermes H4 (MCP) — config-only.** Five `hermes.mcp_*` fields exist and are settable; no MCP code anywhere. Flipping `mcp_enabled: true` does nothing.
- **Dead machinery:** `ActivationLedger.compact()` (zero callers — and see §2: it would destroy data); note ordinals (`registry.py:199-232`, zero callers); `queries.theme_id`/`theme_score` + index (added in migration 5, never written, never read); `queries.session_id` always NULL on the main paths (nothing supplies it).
- **Doc-only roadmap items:** `daemon graph todos`, `daemon query --skip`, multi-embedding spaces, Obsidian plugin, file watcher, local LLM.

### Documentation divergences (ranked)

1. **README is ~2 months / 13 milestones stale** — lists adaptive weighting, consolidation, and the observer as "intentionally deferred (Phase 4+)"; all shipped 2026-05-17. No mention of UUID identity, the ledger, recall, or themes. Also references launcher files that don't exist (`launch-gui.vbs/.sh/.command`, `my-daemon.desktop`).
2. **`docs-source/roadmap.md` inverts reality** — its "Next, in priority order" items are M1–M4 verbatim, all shipped; "deliberately not implemented" still lists `daemon select`.
3. **`docs-source/cli.md` is missing five shipped command groups**: `select`, `snapshot`, `analyze`, `consolidate`, `hermes doctor`. README's command table is missing those plus `activations`, `hot-notes`, `themes`, `migrate`. cli.md also claims exit code 1 on missing config — false (see §3).
4. **`docs-source/configuration.md` documents 9 of 15 config sections** — `memory`, `hermes`, `snapshot`, `consolidation`, `agent.observer_*` missing. Worse: the `memory:` block exists in **neither** `config.yaml` nor `config.example.yaml` — M-mem-5's seven knobs are discoverable only by reading source.
5. **`docs-source/architecture.md`/`index.md` describe the pre-M1 retrieval path** (BFS, "log every query for Phase 4").
6. **`PLAN-MEMORY.md` checkboxes contradict PROJECT_MANAGEMENT.md** (M-mem-5/8 unchecked though shipped).
7. **Version never moved:** `0.1.0` across 48 commits and 14 shipped milestones; no tags, no CHANGELOG.

---

## 2. Memory-loop substance audit (the "going through the motions" question)

### Stage-by-stage verdict

| Stage | Verdict |
|---|---|
| Ingest: vault → chunks → embeddings (incremental on body+frontmatter hashes) | **REAL** |
| Differential graph update (surviving edges keep weight/last_reinforced_at) | **REAL** |
| UUID identity spine | **REAL** (leaks below) |
| Seed retrieval (dense+sparse RRF, server-side) | **REAL** |
| Graph expansion (weighted) | **BROKEN** (float/int seam, see exec summary) |
| `daemon select` → weights | **BROKEN** (TypeError) |
| GUI/Hermes endorse → weights | **REAL — then poisons expansion** |
| Activation ledger write (rank strength, IDF on read, non-blocking) | **REAL** |
| Fingerprint recall | **REAL** (similarity math flawed, below) |
| HDBSCAN clustering + theme reconciliation + LLM naming | **REAL** — best-engineered analysis code in the repo |
| Themes → observer letter | **COSMETIC** — letter is written *before* clustering runs (`agent_observe.py:318` vs `:337`); `observer_letter()` has no themes parameter. Themes visible only via `daemon themes list`. |
| Theme tags → vault → re-ingest | **REAL** (round trip closes via frontmatter hash) |
| Theme-loop severance | **5 of 8 edges severed** (gaps below) |
| Edge decay in consolidate | **REAL** |
| Structural analysis / weight-evolution replay / snapshots | **REAL** |
| Activation backfill migration | **COSMETIC** — writes `surface="unknown"`, which is excluded from `INTENTIONAL_SURFACES`; neither recall nor clustering will ever read its output |
| Ledger compaction | **DEAD + latently destructive** — nothing reads `fingerprint_json`; compacting would erase queries from recall/clustering, the opposite of its docstring |

### The deeper design problem: the learning signal is structurally starved

Even with both crashes fixed, `apply_selection` reinforces nothing when seed == selected (`weights.py:56-60`). Seeds carry undecayed vector scores; expanded chunks are multiplied by `decay^distance` (default 0.5); `candidate_pool` is 3. **The user is nearly always picking a seed, which reinforces no path.** The GUI even has copy for this case. Options: reinforce the seed's retrieval features instead of a path; guarantee expansion candidates a pool slot; widen the pool and dampen the seed advantage. This is the single most important design question for the next arc.

### Other confirmed wiring issues

- **Hermes ambient prefetch is classified as intentional.** `core.py:104` builds the engine with `surface="hermes_recall"`, which is in `INTENTIONAL_SURFACES` (`activations.py:53`) — but `prefetch` fires on *every conversational turn*. Once Hermes is on: themes become themes-of-Hermes-turns, and every turn writes an `answer=""` feedback row that floods the observer's "recent chats" (`agent_observe.py:144`, `_render_recent_chats` doesn't filter). Directly defeats the ambient/intentional separation the code's own comments specify.
- **Theme-loop severance gaps:** `shortest_note_path` (`graph.py:260-275`) has no tag exclusion — reinforcement can route through `tag::theme/x` hubs; `_diff_graphs` (`structural.py:473-496`) surfaces theme tags in the observer's weight-evolution block (the daemon quoting its own tags back to itself as evidence — the exact failure `bc54e1d` targeted); `_bridging_notes`/`_orphan_notes` (`structural.py:113,116`) receive the full graph, not the community projection.
- **`similar()` is not a true cosine** (`activations.py:348-354`): IDF is applied to the probe vector but the candidate side divides raw strengths by a pre-IDF norm. Scores unbounded by 1; `min_score`/`max_df_ratio` don't mean what the docstring says; high-df hubs keep full candidate-side weight.
- **Remaining path-keyed state after the cutover:** `agent_link_runs`/`agent_extract_runs` are `note_path` primary-keyed (`db.py:54-66`) — a rename re-runs the extractor/linker and orphans the old row. `backfill.py` resolves by path via `registry.by_path`, which excludes soft-deleted rows.
- **`ingest_note` captures are link-less:** `parse_note` alone can't resolve wikilinks (no title index), so every wikilink in a Hermes capture becomes a `dangling::` node with zero graph neighbors until the next full `ingest_vault`.
- **Stale Qdrant payload on frontmatter-only edits** — inert today because `Chunk.tags`/`wikilinks` have zero consumers (also dead payload weight).
- **Nightly clustering is O(n²) pure Python** (`analysis/themes.py:97-101`): ~8M `cosine()` calls at the default `theme_query_limit=4000`, ~128MB dense matrix — minutes of CPU, and the Hermes-surface bug fills that budget 10–50× faster than intended.
- **Hardcoded constants that govern learning behavior** (none in `Settings`): `weights.py:25-28` (`alpha=0.5`, `hop_decay=0.7`, `ceiling=5.0`, `half_life=30` — the entire learning-rate schedule), `theme_tags._MIN_NOTE_WEIGHT`, `themes._DORMANT_AFTER_MISSES`, `activations.STRENGTH_BY_SOURCE`, `_MIN_CORPUS_FOR_DF_PRUNING`, `structural.DEFAULT_WARM_THRESHOLD`, `expand.py` chunk limit, `vector.py` prefetch limit.

---

## 3. Install, configuration, and daily-use story

**Telling ground truth:** Docker/podman are not installed on the author's primary Ubuntu machine, and `data/` contains only `feedback.db` — the documented happy path (`docker compose up -d` → ingest → query) has never been exercised end-to-end on the author's own box.

### P0 — blocks unattended daily use

1. **CWD-relative config with silent fallback.** `_default_config_path()` = `Path.cwd()/config.yaml`; missing file → silent defaults (`config.py:269-301`). Verified: `daemon migrate status` from an empty temp dir exits 0 against a nonexistent vault. The `FileNotFoundError` catch in `cli.py:78-82` is dead code. **This breaks both documented scheduling paths:** the cron examples have no `cd` (cron runs from `$HOME`), and the setup GUI's `schtasks` call sets no working directory (Windows defaults to `system32`) — the nightly job reads the wrong config and reports success. Fix: user-level config/state dirs (`~/.config/my-daemon`, `~/.local/share/my-daemon`), refuse loudly when config is missing, fix both scheduler paths.
2. **No `daemon doctor` and no preflights.** Qdrant down = raw `Errno 111` traceback out of query/ingest/search (GUI renders `_(daemon error: …)_`); missing API key discovered only at synthesis time after a full embed+retrieve; bad vault path just reports "Notes scanned 0". A `hermes doctor` exists for the optional integration; nothing for the core.
3. **`daemon ask` bug** (see executive summary).
4. **Non-atomic, unlocked graph/manifest writes.** `pickle.dump` straight into the live file (`graph.py:54-59`); bare `pickle.load` with no error handling; same for `manifest.json`. No process locking anywhere. The realistic daily config — GUI open all day (saves graph on every endorse click) + nightly cron agents — is a last-writer-wins race; a Ctrl-C mid-save leaves a corrupt pickle whose only recovery is `daemon reset`. The vault writer already does atomic replace correctly — apply the same pattern.
5. **`daemon reset` is a footgun.** Hardcodes `./data` ignoring configured paths; `rmtree`s the Qdrant storage directory out from under a running container; destroys the model cache (~130MB re-download), snapshots, and `feedback.db` (activation ledger + learned weights + themes) behind one prompt. `scripts/reset.py` is the same with no prompt. Needs: config-awareness, refusal while Qdrant is up, `--keep-models` default, explicit `--all` for feedback.db.
6. **Nothing is automatic.** Zero occurrences of watchdog/watchfiles/APScheduler. Ingest — the most important recurring job — is never scheduled by anything. The only shipped automation is the Windows-only, `reflect`-only, wrong-working-directory checkbox. Recommended shape: `daemon run` (debounced vault watch → incremental ingest; nightly consolidate) + generated systemd-user/launchd/Task Scheduler units.
7. **`pip install` is impossible.** Wheel packages only `src/my_daemon`; `daemon init` hard-fails outside a repo clone ("Run from the project root"). No PyPI publish, no pipx/uvx story. Ship `config.example.yaml`/`.env.example` as package data via `importlib.resources`.
8. **`uv.lock` is deliberately gitignored** — no reproducible install; the 879KB lockfile exists locally, untracked. Commit it.
9. **No backup/restore for the daemon's own memory.** Snapshots live inside `./data` (reset eats them) and have no `restore` counterpart. `feedback.db` + `graph.gpickle` accumulate irreplaceable learned state with no export path.

### P1 — before handing to anyone else

- **`config.example.yaml` ships `agent.enabled: true`** — contradicts the code default (False), the README's documented rollout, and gives a new collaborator vault writes on day one. Set false.
- **Setup GUI destroys config comments:** `yaml.safe_dump` over the whole file deletes ~90 lines of explanatory comments on first Save; `PROJECT_ROOT = Path.cwd()` at import time writes config wherever it was launched. (The repo already has `add_frontmatter_list_values_textual` proving the textual-edit pattern.)
- **Token/cost accounting is absent** — `llm/client.py` discards `message.usage`; `consolidate` runs Opus over the whole structural report nightly with no estimate, cap, or ledger. Log usage to `feedback.db`; add `daemon cost --days N`.
- **`pytest` and `mkdocs` are runtime dependencies** (`[project.dependencies]`); move to dev. Python band `>=3.11,<3.15` with the local venv at 3.14.4 is a torch/sentence-transformers wheel minefield; document/target 3.11–3.12.
- **pywebview on Linux is an undeclared system dependency** — `import webview` succeeds without GTK backend; failure happens deep inside `ui.run(native=True)`; the `.desktop` launcher has `Terminal=false` so it's a silent no-op double-click. Probe the backend or default `--no-native` on Linux; document the apt packages.
- **Embedded Qdrant as default.** `qdrant-client` supports local/path mode; `VectorStore._client_` (`stores/vector.py:35-42`) is the only branch point. Deletes the single largest onboarding prerequisite (Docker Desktop).
- **`daemon status` loads the full embedding model** (cold cache = ~130MB download) just to print three numbers — cache the dimension in the manifest.
- **Secrets hygiene: verified clean** (credit where due). Key is env-only, never touches YAML; `config.yaml`/`.env` untracked; the local `.env` is still the placeholder. No key has ever been committed.
- **Destructive-op guards are the strongest UX in the repo** (also credit): `agent.enabled` refusals, dry-runs everywhere, `assign-uuids` defaulting to dry-run with backup + printed rollback.

---

## 4. Engineering quality and process

### Craft: high

- **Test quality is genuinely good** — better than the repo's own history suggests. Zero `MagicMock`; 12 hand-written fakes; 671 asserts / 359 tests with exact-value comparisons; negative/boundary cases throughout; visible anti-regression scar tissue ("Asserting the shape only is what let this silently return {} for a whole release"). SQLite and filesystem are real-but-tmp_path — the right line.
- **`stores/db.py` is the best-engineered file in the repo:** per-migration `BEGIN IMMEDIATE` with `user_version` in the same transaction; deliberate avoidance of `executescript()`'s implicit COMMIT (documented); rollback under `contextlib.suppress` so a failed ROLLBACK can't mask the original error; migration 1 byte-identical to legacy DDL so old DBs converge; `SnapshotSchemaTooOld` for read-only bundles; version derived from the list. 100% covered.
- **SPDX headers: 100%** across all 91 `.py` files. Zero TODO/FIXME/HACK in `src/`. Ruff clean.

### Process: absent

- **No CI** (`.github/` doesn't exist), no review gate, commit-to-master convention, 48 commits / 1 branch / 0 tags.
- **Mypy is a declared dev dep that never runs — and it is hiding the `daemon select` crash today** (29 errors in 22 files; most are stub noise, one is the live TypeError; others worth triage: `vector.py:59,143,149`, `snapshot.py:230,336`, `migrate_uuids.py:125`, `expand.py:71`).
- **Coverage 67%** (measured ad hoc; pytest-cov not even a dependency). Bimodal: core stores ~96–100%; `cli.py` 33%, `gui/` 0%, `llm/agents.py` 23%, `stores/vector.py` 30%, agent jobs ~31–34%.
- **`ruff format` never adopted** — 60 of 93 files would reformat. Run once as an isolated commit before collaborators branch.
- **`CONTRIBUTING.md` is 0 bytes.** `.idea/` (8 files) committed. `docs-site/` (4.1MB of build output) committed alongside its source.
- **`build_core` still duplicates `build_stores`** — the M-mem-9 "one construction path" is one-third done: `build_stores` has exactly one caller (CLI); `build_core` (`core.py:541-582`) hand-rolls the identical construction, and it is what the GUI and Hermes call. `test_wiring.py`'s invariants therefore don't hold for the two surfaces the refactor was written to fix. Five more inline `ActivationLedger`/`NoteRegistry` constructions in `cli.py`.
- **README documents a failing test command** (`MY_DAEMON_E2E=1 pytest tests/test_e2e.py` → `NotImplementedError`). No true end-to-end test exists; `test_integration_core.py` is closest but stubs `core.engine.ask` with a lambda, so retrieval is never exercised in-chain.

---

## 5. Secondary surfaces

### `librarian/` — grade 0/5 → **FREEZE (keep the plan, label it)**

Planning docs only; single commit 2026-06-05, never touched. The plan is unusually high-quality and the core has since moved *toward* it — but it is **identity-stale**: §L4's mover contract is designed entirely around basename uniqueness + path rewriting, with no knowledge of the `uuid:` frontmatter / `NoteRegistry` / `derive_path_uuid` machinery that was built precisely to make moves survivable (`PROJECT_MANAGEMENT.md:166` says so explicitly). The packaging decision (workspace member vs subpackage) is still unresolved, which is why L0 never started. Action: add a `librarian/STATUS.md` banner (or move the docs under `docs-source/plans/`), rewrite §L4 around UUID identity before any implementation.

### Hermes — grade 3/5 → **INVEST, narrowly and conditionally**

Real, tested, genuinely interlaced code (prefetch→recall_block, sync_turn→remember/endorse, five tools dispatching into `DaemonCore`) that **has never met its host**. The ABC import is a 4-way guessed fallback; `plugin.yaml` is unverifiable against the real loader; the documented install path (`pip install my-daemon`) cannot work (unpublished). The `neighbors` regression proves the surface is only ever exercised through core tests. Also: `_reinforce_used` endorses on a filename-stem substring match in the assistant's reply — trivially fooled; `core.remember` re-implements containment locally instead of using `writer.is_writable`. Action: (a) install Hermes once, verify the ABC path and plugin schema, replace the guess + add one contract test; (b) fix the install story. **Do not start H4 (MCP) or H5 until (a) passes.** If Hermes won't run in the next arc, downgrade to freeze — nothing rots while `DaemonCore` tests pass.

### GUI — grade 3/5 → **INVEST (it's the product)**

Correctly migrated to `DaemonCore`/`ask_stream` with real producer-thread streaming, the endorse split done right (explicit click not gated by `hermes.allow_write_back`), memories strip, proper HTML escaping. Gaps: **it's styled as a chat but behaves as a query box** — no `session_id` (zero hits in the file), no turn history, no message context to the LLM, activations never grouped into sessions; **zero UI tests** on a surface with a demonstrated silent-breakage history (the NiceGUI root-callable 500); **four milestones behind the core** — themes review and observer letters have no GUI surface despite `core.latest_dream`/`read_dream` sitting right there; no status/health surface; **Google Fonts fetched on every launch, including `--native` desktop mode** — contradicts the local-only stance asserted in the Hermes provider and plugin README. Setup GUI: Windows-shaped (`.venv/Scripts/daemon.exe`, `setx`, schtasks-only) while the project targets Windows *and* Ubuntu.

---

## 6. Prioritized roadmap

### Arc 1 — Close the loop (days)

1. Fix `graph_distance: int | None` → `float | None` (`models.py:119`); audit the distance-sensitive comparisons at `trace.py:64`, `query.py:54`, `backfill.py:121`.
2. Fix `cli.py:301` (`selected_note_path` → drop; kwargs now uuid-only).
3. Fix `daemon ask` to invoke the query body with real defaults.
4. Add seam tests: `neighbors_within` output → `expand_from_seeds` **with a reinforced edge in the graph**; `typer.testing.CliRunner` smoke tests for all 18 commands' argument plumbing.
5. Reclassify Hermes prefetch as ambient (either a distinct `hermes_prefetch` surface or drop `hermes_recall` from `INTENTIONAL_SURFACES`), and stop writing empty-answer feedback rows per turn.
6. Pass themes into the observer letter (reorder `agent_observe.py` phases; add a themes parameter to `observer_letter`).
7. Then **use it for a week and watch `daemon analyze` show a non-uniform graph for the first time.**

### Arc 2 — Make it a daemon (1–2 weeks)

8. User-level config/state dirs; loud failure on missing config; fix cron/schtasks working directories.
9. `daemon doctor` (config / vault / Qdrant / collection dim / API key / model cache / DB schema) + inline preflights; wrap the Qdrant connection error with remediation text.
10. Atomic writes (`tempfile` + `os.replace`) + a `.lock` file for `graph.gpickle` and `manifest.json`; graceful corrupt-pickle recovery message.
11. Defang `daemon reset` (config-aware, refuse while Qdrant up, `--keep-models`, `--all` for feedback.db); give `scripts/reset.py` the same guards.
12. `daemon run` supervisor (debounced watch → incremental ingest; nightly consolidate) + generated scheduler units.
13. Embedded/local Qdrant as the default; Docker as scale-up.
14. `daemon backup`/`restore` for feedback.db + graph + manifest.

### Arc 3 — Collaborator readiness (before invites)

15. Commit `uv.lock`. Add CI: pytest + `ruff check` + `ruff format --check` + mypy (baseline the stub noise). One isolated `ruff format` commit first.
16. Write `CONTRIBUTING.md` (clone → setup → compose → pytest → ruff, `MY_DAEMON_E2E` note, SPDX/license rules). Untrack `.idea/` and `docs-site/`.
17. Retire commit-to-master; adopt branches + PR review.
18. Fix `config.example.yaml` (`agent.enabled: false`); add the missing `memory:` block to both YAML files.
19. Docs refresh: README (features + commands + remove phantom launchers), roadmap.md, cli.md (five missing command groups, false exit-code claim), configuration.md (six missing sections), architecture.md (post-M1 reality). Implement or delete `test_e2e.py`.
20. Package config templates in the wheel so `daemon init` works installed; move pytest/mkdocs to dev deps; bump the version and start tagging.
21. Finish M-mem-9: route `build_core` through `build_stores`; collapse the five inline store constructions in `cli.py`.

### Arc 4 — Invest by surface

22. GUI first: `session_id` + turn history; status bar (Qdrant / key / last ingest); themes-review + dream-viewer panels; one smoke test against a stubbed core; self-host the fonts; anchor setup GUI paths to the package + cron/systemd path parallel to schtasks.
23. Hermes: one real host validation, then the contract test; else freeze.
24. Librarian: STATUS banner now; rewrite §L4 around UUID identity before L0.
25. Loop-severance completion: tag exclusion in `shortest_note_path`, `_diff_graphs`, betweenness/orphan inputs.
26. Config-lift the learning constants (`weights.py` schedule at minimum); fix or delete `compact()`; fix the `similar()` IDF asymmetry; migrate `agent_*_runs` tables to uuid keys; vectorize the O(n²) clustering (or cap input post-surface-fix).

### The design question to sit with

**Decide what "picking" means before scaling the learning machinery.** Seeds dominate the candidate pool and seed-picks reinforce nothing — the reinforcement seam is the difference between a system that adapts and one that merely records. Everything downstream is already built and waiting for signal. Candidate directions: reinforce seed retrieval features, reserve pool slots for expanded candidates, widen the pool with damped seed advantage.

---

## 7. Known Bugs candidates for PROJECT_MANAGEMENT.md

New, verified, not currently in the Known Bugs list:

1. `expand_from_seeds` `ValidationError` on any reinforced edge (float distance vs `int` field) — **critical**.
2. `daemon select` `TypeError` on every invocation (dead kwarg) — **critical**.
3. `daemon ask` silently skips synthesis (Typer `OptionInfo` defaults) — **high**.
4. Hermes prefetch surface classified intentional → ledger/theme pollution — **high** (latent until Hermes is on).
5. `ActivationLedger.compact()` would erase fingerprints it claims to preserve (nothing reads `fingerprint_json`) — **medium** (latent, zero callers).
6. `apply_selection` can route reinforcement through theme-tag hubs (`shortest_note_path` has no tag exclusion) — **medium** (latent until theme tags accepted).
7. Themes never reach the observer letter (phase ordering) — **medium**.
8. Backfill writes `surface="unknown"` that nothing reads — **medium**.
9. `similar()` IDF asymmetry (probe weighted, candidates not) — **medium**.
10. Missing-config silent fallback + CWD-relative state (breaks cron/schtasks) — **high** for daily use.
11. Non-atomic graph/manifest writes, no locking — **high** for daily use.
12. `agent_link_runs`/`agent_extract_runs` still path-keyed post-cutover — **low**.
13. `config.example.yaml` ships `agent.enabled: true` against the documented rollout — **low** severity, **high** surprise.
