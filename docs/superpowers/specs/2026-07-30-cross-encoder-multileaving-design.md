# Cross-encoder reranking as a multileaved team (§IV.18) — design

- **Date:** 2026-07-30
- **Status:** **APPROVED** (wiring chosen by the user from three options).
- **Author:** Evan Gress, with Claude
- **Tracks:** §IV.18 in [MY-DAEMON-RESEARCH-APPLIED.md](../../../MY-DAEMON-RESEARCH-APPLIED.md)

## Problem

The retrieval pipeline has no relevance reranking. `RetrievalOrchestrator._pool`
dedupes seeds and expanded candidates by chunk id, sorts by `combined_score`, and
either returns that or team-drafts it. `combined_score` is a hybrid RRF score for
seeds and a decayed graph distance for expanded candidates — two quantities on
different scales, compared directly.

Hindsight rewrites this ordering with a cross-encoder over up to 300 candidates
before applying its boosts. It is the largest capability gap the comparison found
that is also cheap, and — unlike most of that review's findings — it is **not**
in the existing 16-item research ledger, so it would otherwise stay invisible.

Two things make it cheap here. `sentence-transformers>=2.7` is already a
**declared direct dependency** (`pyproject.toml`) and ships `CrossEncoder`
(verified: 5.5.0 installed, class importable). And `embeddings/embedder.py`
already has the lazy-load + `download()` + offline-cache pattern to copy, wired
into `daemon models download`. So this needs **no new package** — only a new
model download.

Note that MMR (§IV.2, still open) is a **diversity** operator, not a relevance
one. These are complements: MMR decides what not to repeat, a cross-encoder
decides what is actually about the question. Shipping either does not remove the
case for the other.

## Decision: a third multileaved team

Chosen by the user over "post-fusion stage, default off" and "replace the score
merge, default on".

The reranker produces an **alternative ordering of the same candidate pool**, and
that ordering competes against the current score-merge ordering in the draft.
`daemon policy` then reports which one actually earns picks, on this vault.

Two reasons this is the right shape rather than the elaborate one:

**It answers the question the other options cannot.** A post-fusion stage behind a
default-off flag has no evidence attached, so enabling it is an act of faith; and
an off-by-default feature nobody measures tends to stay off. Replacing the score
merge outright would likely be the biggest single quality jump, but it changes
every answer with no A/B and it demotes the hand-drawn wikilink signal that makes
this project distinguishable from a vector store with a chat window.

**Comparing two orderings of one candidate set is the more canonical use of team
draft than the existing one.** The current 2-team setup compares two *sources*
(seed versus expansion), which partition the pool. Comparing two *rankings* of the
same set is the setup Radlinski, Kurup & Joachims (CIKM 2008) actually describes.
So the reranker is not a bolt-on to the interleave — it is closer to its
textbook application.

Going from two teams to three has published backing: **team-draft multileaving**
(Schuth, Sietsma, Whiteson, Lefortier & de Rijke, *Multileaved Comparisons for
Fast Online Evaluation*, CIKM 2014), which generalises team-draft interleaving to
n rankers while preserving the per-team position symmetry that makes a pick
attributable without a propensity model. Verified: authors, venue and year
confirmed, per the applied-research document's citation discipline.

**Confirmed by inspection: this costs zero schema or model change.**
`RetrievedChunk.team` is a free-form `str | None`, and
`RetrievalPolicyStore.record_impressions` takes an arbitrary `Mapping[str, int]`
with no enum or CHECK constraint. A third policy name simply appears in
`daemon policy` output.

## Design

### 1. Generalise the draft — `retrieval/interleave.py`

`team_draft(seeds, expanded, *, rng)` becomes
`multileave(rankings: Mapping[str, Sequence[RetrievedChunk]], *, rng) -> list[RetrievedChunk]`.

The existing algorithm already generalises with almost no structural change: it
maintains per-team cursors, counts and a shared `seen` set, dispatches to whichever
team has drafted fewest, and breaks ties by coin toss. For n teams:

- Dispatch to the team with the **minimum draft count** among non-exhausted teams.
- Break ties **uniformly at random among all tied teams** (`rng.choice` over the
  tied set), not by a fixed order. This is the load-bearing detail: any
  deterministic tie-break hands one policy the earlier position systematically and
  reintroduces exactly the bias the draft exists to cancel. With two teams a coin
  was sufficient; with three, "first tied team wins" would quietly privilege
  whichever key was inserted first.
- Keep the existing `_draft` return-value guard so termination stays structural
  rather than argued — it runs on every query, and a dispatch to an exhausted team
  would spin forever and freeze the daemon.

Teams overlapping is fine and expected: the reranker's ranking contains the union
of the other two. The `seen` set already handles a candidate being drafted once,
and the existing docstring anticipated this ("a chunk id already drafted is
skipped rather than drafted twice").

The single caller is `_pool`. `team_draft` is **replaced** rather than kept as a
wrapper — one caller, and two functions doing the same thing would rot. The module
docstring gains the Schuth et al. citation alongside the existing Radlinski and
Joachims ones.

### 2. New unit — `retrieval/rerank.py`

`CrossEncoderReranker`, mirroring `Embedder`'s shape so it is familiar and
testable:

- **Lazy model load**, cached on the instance. Nothing is loaded until a rerank
  is actually requested, so an unused reranker costs nothing at import or at
  construction.
- **`download() -> Path`**, so `daemon models download` can pre-pull it and
  subsequent runs stay fully offline. Copied from `Embedder.download`.
- **`rank(query, candidates) -> list[float]`** — scores only. It does not sort,
  does not know about `RetrievedChunk`, and does not touch config. A pure scorer
  is trivially testable with a stub, which is what keeps the orchestrator tests
  free of a model download.

**Pair construction** takes Hindsight's format, which pairs the query against
context-prefixed text rather than bare text:

```
(query, "Journal.md › March 2  [2026-03-02]\n<chunk text>")
```

The heading path and date are included deliberately — they are real relevance
signal a bare chunk body omits, and the date lands here for the same reason
§IV.10 amendment 2 puts it in the prompt.

**Score normalisation** copies the one genuinely subtle thing Hindsight does
here: **pass scores through verbatim when they are already in [0, 1]**, and only
apply a sigmoid to raw logits. Their in-code reasoning is that a calibrated model
scoring a top candidate at 0.007 *means* it, and unconditional sigmoiding destroys
that information by mapping everything to ~0.5. Most implementations sigmoid
unconditionally.

### 3. Wiring — `RetrievalOrchestrator._pool`

```
best        = dedupe(seeds + expanded)          # unchanged
by_score    = sort(best, key=combined_score)    # unchanged

if reranker is None or not settings.retrieval.rerank:
    rankings = {"seed": …, "expansion": …}       # today's behaviour, exactly
else:
    capped   = by_score[: settings.retrieval.rerank_max_candidates]
    reranked = sort(capped, key=reranker.rank(query, capped))
    rankings = {"seed": …, "expansion": …, "rerank": reranked}

return multileave(rankings, rng=self.rng)
```

The reranker is injected through the constructor with a `None` default, matching
how `sparse_embedder` and `listeners` already work — so every existing
construction site and test keeps working untouched.

**Rerank scores never overwrite `combined_score`.** The reranked ordering is
expressed as list order only. Two reasons: `combined_score` is persisted in the
retrieval summary and consumed by the activation ledger's rank-based strength, so
overwriting it would silently change what §IV.1's fingerprints mean; and keeping
the pre-rerank score intact is what allows the two orderings to be compared at
all.

### 4. Config

| Key | Default | Meaning |
|---|---|---|
| `retrieval.rerank` | `false` | Master switch |
| `retrieval.rerank_model` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Model id |
| `retrieval.rerank_max_candidates` | `100` | Latency bound |

**Default off, with a deliberate counter to the "stays off forever" risk** I
flagged when presenting the options: `daemon doctor` gains a check that reports
*"reranking is available but disabled — enable `retrieval.rerank` to A/B it via
`daemon policy`"*. Off-by-default is required because turning it on without the
model cached would download on first query, which breaks the offline guarantee
the embedder works hard to provide. Surfacing it in `doctor` is what stops
off-by-default from meaning invisible.

`daemon models download` pulls the reranker when `retrieval.rerank` is true,
alongside the existing dense and conditional sparse pulls.

## Licence check — a required gate before merge

No new pip package, so `scripts/license_check.py` has nothing new to say and the
CI gate stays green trivially. **That is exactly the gap worth naming:** the
checker covers Python distributions, not model weights, and this change
introduces a new *artifact* under its own licence.

`cross-encoder/ms-marco-MiniLM-L-6-v2` is understood to be Apache-2.0, **but that
is not verified** and CLAUDE.md rule 10 requires it to be before the dependency
lands. Required before merge:

1. Read the licence from the model card / repository metadata and record the
   finding, with its source, in `debug/license-compliance.md`.
2. If it is not Apache-2.0-compatible, stop and surface the tradeoff rather than
   silently accepting a weaker model — per the standing preference recorded from
   the Leiden/`graspologic-native` episode. Permissive alternatives to check in
   order: `BAAI/bge-reranker-base` and the `flashrank` ONNX family.
3. Note in the spec's follow-up whether `license_check.py` should grow a
   model-weights table. Recommendation: yes, but as its own item, not smuggled in
   here.

## Testing

| Unit | What gets pinned |
|---|---|
| `multileave` | n=1, 2, 3 teams; overlapping teams drafted once; per-team counts stay within 1 of each other; **tie-break is uniform over all tied teams** (seeded rng, asserted distribution over many draws, not a single draw); exhausted teams never dispatched; terminates on empty input |
| `multileave` regression | With exactly two teams it reproduces the current `team_draft` output for a seeded rng — the existing interleave tests must still pass unchanged |
| `CrossEncoderReranker` | `rank` returns one score per candidate, in order; scores already in [0,1] pass through unchanged; raw logits get sigmoided; pair text includes heading path and date; model is not loaded until `rank` is called |
| `_pool` | Reranker `None` → today's two-team behaviour byte-for-byte; reranker present but `rerank=false` → same; reranker present and enabled → three teams; `rerank_max_candidates` respected; **`combined_score` unchanged after reranking** |
| Policy ledger | Three policies recorded; `rerank` impressions counted only when it contributed; zero-count teams still skipped |
| Offline | `download()` pre-pulls; a stub reranker keeps every orchestrator test free of a model download |

A stub `CrossEncoderReranker` returning fixed scores is what keeps this testable
without network access — the same trick the embedder tests already use.

## Risks

**Latency, unmeasured.** MiniLM-L-6 cross-encoding is fast on CPU but not free,
and it runs on every query when enabled. `rerank_max_candidates=100` bounds it,
but the actual figure on this hardware is unknown until measured. The
implementation must report rerank latency separately in `daemon query -v` so the
cost is visible rather than folded into a total. If it is bad, the honest response
is lowering the cap, not hiding the number.

**Three teams means fewer slots each.** The presented pool is trimmed to a token
budget, so with three teams drafting alternately each contributes roughly a third
rather than a half. That is the correct behaviour for an unbiased comparison, but
it does mean the seed-versus-expansion win rates gathered under two teams are not
directly comparable to those gathered under three. `daemon policy` should note the
team count so a historical comparison is not made naively. Worth stating plainly:
turning this on **resets the interpretability** of the existing policy history,
which currently has too few picks to be meaningful anyway (the §IV.7 note says
nothing should be wired to those rates below ~20 picks).

**Win rates remain purely diagnostic.** Nothing feeds them back into ranking, per
the standing rule in `stores/policy.py` — a policy tuned on its own win rate is a
closed loop with nothing outside it.

## Out of scope

- **Feeding win rates back into ranking.** See above.
- **Hosted reranker providers.** Hindsight supports 13; this project is
  local-first and one local model is the whole requirement.
- **Reranking the expansion arm separately**, or reranking before fusion. The
  point here is to rank the *pool*, which is what makes it an alternative ordering
  rather than a fourth source.
- **A model-weights table in `license_check.py`** — recommended, tracked as its
  own follow-up.
