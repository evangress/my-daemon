# A retrieval-quality evaluation harness (§IV.23) — design

- **Date:** 2026-07-31
- **Status:** **APPROVED** (four decisions chosen by the user from options).
- **Author:** Evan Gress, with Claude
- **Tracks:** §IV.23 in [MY-DAEMON-RESEARCH-APPLIED.md](../../../MY-DAEMON-RESEARCH-APPLIED.md)
- **Sequenced as:** step 1 of the 2026-07-31 build sequencing in
  [PROJECT_MANAGEMENT.md](../../../PROJECT_MANAGEMENT.md). Step 0 (wiring the
  policy ledger) shipped in `96ef0ac`.

## Problem

There is no way to answer *"did that retrieval change help?"* except by reading
answers and forming an impression. 901 tests establish that the pipeline
**works**; none establish that it **retrieves well**.

This matters right now rather than in general, because the next two items on the
roadmap — §IV.20 (activation stats into ranking) and §IV.9 (the salience layer)
— are both bets that change ranking on the user's own data. Their own entries
say to *measure* the effect rather than assert it. Today there is nothing to
measure with.

Step 0 restored the one instrument that existed: `daemon policy`'s interleaved
win rates. But that instrument is now **newborn rather than accumulated** — the
ledger holds 9 queries and 2 selections, because nothing was recording
impressions until yesterday. Win rates need well over twenty picks before they
are anything but noise. So the online signal cannot answer a question asked
today, and an offline instrument has to stand on its own fixtures from day one.

### Why not LoCoMo or LongMemEval

Recorded in §IV.23's entry and unchanged: Hindsight's headline numbers are not
reproducible from its own repository, its judge defaults to the same model as
the system under test, LongMemEval's *abstention* category is not implemented,
and its CI runs a hand-picked 3-of-10 LoCoMo subset excluding the conversation
that times out. Honcho's published quality depends on a proprietary fine-tune
absent from its repository.

The gap here is not benchmark quality — `daemon policy` and `simulate_evolution`
are *more* honest instruments than either project has. The gap is that no number
is published at all, which is a credibility problem rather than a quality one.
A small, honest, vault-local harness closes it without importing anyone's
methodology.

## Decisions

Four, each chosen by the user from presented options.

### 1. Purpose: tuning on the real vault, not CI regression detection

The harness answers *"is reranking actually earning its four seconds on **my**
vault?"* It runs against the real 130-note vault with hand-authored questions,
as a manual command in the shape of `daemon themes tune`.

It therefore **cannot ship in the repository or gate CI** — the corpus is about
the user's own notes. That is accepted, not worked around. A CI-safe subset
against `tests/fixtures/sample_vault` is explicitly out of scope; the fixture
vault is ~6 synthetic notes and could prove only that nothing crashed.

### 2. Fixtures: hand-authored, carrying a difficulty tier

There is no usable history to mine. Measured on the live database while
designing this:

```
feedback rows:        7        queries in ledger:      9
candidate_selected:   2        distinct query texts:   7
```

So the corpus is written by hand. Each question carries a **tier**, because
averaging a question whose wording overlaps its note with one that requires two
graph hops produces a number that describes neither:

| Tier | Meaning |
|---|---|
| `direct` | wording overlaps the target note; the bi-encoder should find it |
| `oblique` | no lexical overlap — recall by meaning, not by match |
| `multi-hop` | needs graph expansion; multiple `expect:` notes |
| `abstain` | the vault genuinely cannot answer it; `expect:` must be empty |

LLM-drafted questions are **deliberately not part of v1**. They are paraphrases
of note text and are therefore findable by construction, which flatters the
bi-encoder precisely where it is already strongest. If they are added later they
must be a separately-labelled tier, never averaged into the hand-written ones.

### 3. Comparison: sweep configurations within one run

One command evaluates several configurations back-to-back against the same vault
state, exactly as `daemon themes tune` sweeps thresholds.

The rejected alternative was a committed baseline JSON diffed across commits.
It is confounded: the vault mutates between runs, so a recall drop may be new
notes rather than changed code. A sweep cancels vault drift because every arm
sees identical notes, and it leaves no stored artefact to go stale.

### 4. Scoring cut: at the context block, with wide recall alongside

`RetrievalResult.ranked` is **post-trim** (`orchestrator.py:98-112` assigns
`kept`), so it is exactly what the model sees. That is the primary metric.

A second, wider number is reported beside it. The two disagreeing **is the
finding**: notes that retrieval found but ranking dropped mean the defect is in
ranking, not in retrieval — a distinction that determines where to look next and
that a single number hides.

## Architecture

```
analysis/eval.py           pure scoring + sweep; no CLI, no I/O beyond the corpus
├─ EvalQuestion            q, expect[], tier                 (frozen dataclass)
├─ QuestionOutcome         per question: in_context, in_pool, first_rank, top_score
├─ ArmReport               per config: recalls, MRR, abstention, latency
└─ run_sweep(stores, corpus, arms) -> list[ArmReport]

vault/eval_corpus.py       load + validate questions.yaml, resolve note paths
cli.py                     `daemon eval [--sweep NAME]` — table rendering only
config.py                  new `EvalConfig`: corpus_path, abstain_threshold
<config-dir>/eval/questions.yaml   the corpus; gitignored
```

### New configuration

One new block, both fields with defaults, so an existing `config.yaml` keeps
working untouched:

```yaml
eval:
  corpus_path: ./eval/questions.yaml   # resolved against the config directory
  abstain_threshold: null              # unset → measure, do not grade
```

`corpus_path` is **anchored to the config directory**, not to the process
working directory. That is not a detail: the 2026-07-26 maturity evaluation
found "missing config silently became defaults" as a bug that broke scheduled
runs, and the fix was config-dir-anchored paths throughout. A corpus path
resolved against `cwd` would find the file when run from the repo root and
silently report "no corpus yet" from anywhere else.

The corpus is a description of the user's private notes, so `eval/` is added to
`.gitignore` as part of this work. `config.example.yaml` gains the block with
the defaults and a comment pointing at the format.

Module boundaries follow the existing split: `analysis/theme_tuning.py` is the
precedent — a pure replay module with a frozen report dataclass, and a CLI
subcommand that only renders.

### Corpus format

```yaml
- q: "what did I decide about local LLMs?"
  expect: ["Reference Library/Infrastructure/Hermes Stack.md"]
  tier: direct

- q: "the thing I keep coming back to about memory decay"
  expect: ["Designing AI Memory.md"]
  tier: oblique

- q: "why did I pick Qdrant over Chroma"
  expect:
    - "Reference Library/Infrastructure/Hermes Stack.md"
    - "Reference Library/RAG with Hermes and Synology NAS.md"
  tier: multi-hop

- q: "what did I conclude about the tax filing deadline"
  expect: []
  tier: abstain
```

`expect:` holds **vault-relative note paths**, not chunk ids or UUIDs. Paths are
what a person can write without tooling; they are resolved to UUIDs at load time
through the existing `NoteRegistry`, so a note that has been renamed since the
corpus was written is reported as an unresolvable fixture rather than silently
scoring zero.

### Data flow

```
question ──► orchestrator.retrieve(q, surface="eval")   [listeners: none]
                    │
                    ├─ result.ranked             post-trim → in-context metrics
                    └─ result.seeds ∪ expanded   pre-trim  → wide recall
```

The wide set is `seeds ∪ expanded` deduped by chunk id, which is **exactly** the
set `_pool` merges (`orchestrator.py:137-141`). So the wide number requires
**no production change**. Exposing the untrimmed ordering on `RetrievalResult`
purely so eval could rank it would put test-only machinery in production, which
this codebase has refused before — `split_disconnected` was extracted as a pure
function rather than added as a `_partition` test hook for exactly this reason.

The cost is that wide recall is **set membership, not a rank**: the report says
*"found but ranked out"*, not *"found at position 34"*. Accepted for v1; the gap
is the actionable part, the depth is not.

### Metrics

Macro-averaged over questions, and broken out per tier.

| Metric | Computed from | Reads as |
|---|---|---|
| `in_context_recall` | `\|found ∩ expect\| / \|expect\|` over `ranked` | what the model actually saw |
| `wide_recall` | same, over `seeds ∪ expanded` | what retrieval found at all |
| `ranked_out_gap` | `wide_recall − in_context_recall` | **headline**: ranking problem vs retrieval problem |
| `mrr` | `1 / rank` of the first expected note in `ranked` | near the top, or scraping the floor |
| `fully_answered` | count where every expected note survived | multi-hop only |
| `abstain_top_score` | max `combined_score` on `tier: abstain` | the distribution that calibrates a threshold |
| `mean_latency_ms`, `mean_rerank_ms` | timing around `retrieve()` | what the gain costs |

Per-question recall is a **fraction**, not a boolean, so a multi-hop question
that surfaces one of its two notes scores 0.5 rather than being counted as a
miss. `fully_answered` is reported separately for the cases where partial credit
is not the interesting answer.

### Abstention

The user chose to **grade** abstention rather than only measure it. The pass
condition needs a score threshold, and **no such threshold exists anywhere in
this system today**. Inventing a constant here would be precisely the kind of
unprovenanced judgement call the parameter-provenance appendix exists to catch.

Resolution: `eval.abstain_threshold` defaults to `null`.

- **Unset** — the harness reports the two `combined_score` distributions
  (answerable versus unanswerable) and **declines to grade**, naming the
  threshold that would separate them.
- **Set** — it grades: an `abstain` question passes when the top
  `combined_score` falls below the threshold, or the context block comes back
  empty.

Measured first, graded second. The threshold arrives with provenance — the run
that produced it.

### Sweeps

Named, each a list of `(label, settings-mutator)` pairs:

| Name | Arms |
|---|---|
| `none` *(default)* | one arm: the current config |
| `rerank` | off / on / on with `rerank_max_candidates: 40` |
| `interleave` | on / off |
| `expansion` | `graph.expansion_depth` 1 / 2 / 3 |
| `seeds` | `retrieval.seed_top_k` 4 / 8 / 16 |

Each arm **deep-copies `Settings`**, applies its mutator, and rebuilds only the
orchestrator (plus a reranker when that arm needs one) from a shared `Stores`.
The embedder, Qdrant client and graph are constructed once.

Two rejected alternatives, both for concrete reasons:

**Mutating a live orchestrator's settings between arms** looks cheapest and is a
trap specific to this codebase. `RetrievalOrchestrator` resolves its reranker at
construction (`wiring.py:85-92` builds it from `settings.retrieval.rerank`), so
flipping the flag on a live instance changes nothing — the arm would report
"rerank on" while running the baseline. That is the same class of silent no-op
as the `PolicyRecorder` gap step 0 just fixed: configuration and behaviour
drifting apart with no error raised.

**A subprocess per arm** is not merely slow, it is structurally blocked.
Embedded Qdrant — the default since 2026-07-26 — holds a single-process file
lock, so a second process cannot open the same storage.

## Three disciplines carried from the existing code

**The harness is a probe, not a question the user asked.** It builds listeners
with `record=False` — the switch step 0 single-sourced in
`pipeline/listeners.py`. A `--sweep rerank` run over 20 questions is 60
retrievals; recording them would pour synthetic queries into the activation
ledger and corrupt fingerprint recall, themes, **and** the policy win rates that
step 0 just made real. `daemon search` already has exactly this discipline.

**Live state is never mutated.** Same rule `theme_tuning.py` follows: the sweep
reads the vault and the stores and writes nothing back.

**Fixtures are validated before any arm runs.** An `expect:` path that does not
resolve would otherwise read as a retrieval failure — the harness would blame
the code for the corpus's bug.

## Failure handling

Every case fails loudly and early rather than degrading into a misleading
number:

| Case | Behaviour |
|---|---|
| `expect:` path does not resolve | Error naming the path **and** the question; refuse to run any arm |
| `eval/questions.yaml` missing or empty | The `themes tune` treatment — explain the format, exit 0 |
| `tier: abstain` with non-empty `expect:` | Contradiction; rejected at load |
| Unknown tier | Rejected at load, listing the valid tiers |
| Rerank arm requested, weights not cached | Name the `daemon models download` fix; never download mid-sweep |
| Unknown `--sweep` name | List the available sweeps |

## Testing

TDD throughout, real code rather than mocks, per house style.

- **Corpus loader** — valid parse; unresolvable path rejected; abstain-with-expect
  rejected; unknown tier rejected.
- **Scoring** — known result plus known expectation gives known recall and MRR,
  including the multi-expect partial-credit case and the empty-expect case.
- **The gap metric** — a fixture where an expected note sits in `expanded` but is
  trimmed out of `ranked` must produce `ranked_out_gap > 0`. This is the one
  behaviour the whole design exists to surface, so it gets a dedicated test.
- **Sweep faithfulness** — each arm's orchestrator genuinely differs: assert the
  rerank arm has a reranker and the baseline does not. This is the
  approach-B trap written down as an assertion.
- **No pollution** — run a sweep, then assert the activation ledger and the
  policy store are both byte-for-byte unchanged. This is the regression that
  would quietly corrupt real memory, so it is tested rather than trusted.
- **CLI** — renders a table; declines gracefully on an empty corpus.

## Out of scope

Named so the boundary is on record:

- **No baseline file and no cross-commit tracking.** If it is ever wanted it must
  be pinned to a snapshot id, so the corpus cannot drift underneath it.
- **No CI wiring.** The corpus is about a private vault.
- **No LLM-drafted questions.** See decision 2.
- **No answer-quality judging.** This measures retrieval, not synthesis. Answer
  quality is §IV.17's territory and already has live-LLM fixtures in
  `tests/test_live_prompts.py`.
- **No feedback into ranking.** Same discipline as `stores/policy.py`: a system
  tuned on its own evaluation score is a closed loop.

## Open question deferred to implementation

How many questions make the numbers meaningful. Twenty is the working figure,
but the honest answer depends on the tier mix — five `oblique` questions carry
more information about a ranking change than fifteen `direct` ones. The harness
should **print its own sample size and per-tier counts** beside the metrics, so
a thin corpus is visible in the output rather than inferred, in the same spirit
as `daemon policy`'s "too few to read much into" line.
