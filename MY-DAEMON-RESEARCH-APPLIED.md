# My Daemon — Applied Research

**Companion to [MY-DAEMON-RESEARCH.md](MY-DAEMON-RESEARCH.md).**
Written 2026-07-28 against the code as it stands at commit `f0ebb3d`, and
revised the same day after the first three recommendations were implemented.

> **Revision note (2026-07-28, later).** Three items from Part IV shipped
> immediately: the fingerprint cosine fix (§IV.1), team-draft interleaving plus
> the policy ledger (§IV.7), and the threshold sweep that turns the theme match
> constant into a measurement (new §IV.15). Those sections have been rewritten
> to describe what the code now does rather than what it might do, and the
> parameter-provenance appendix updated to match. §IV.3's licence problem also
> turned out to have a clean answer — see that section.

The earlier research document was written *before* the system existed. It maps
human neural systems onto the technology landscape in general — a survey, and
deliberately so. This document does the opposite thing: it starts from
**mechanisms that are actually running in this repository**, names the cognitive
science and the machine-learning literature each one lands in, and then asks what
mathematical or algorithmic layer could be added next.

Three ground rules I held myself to:

1. **Only implemented features.** Every mechanism in Part II is code you can run
   today, cited to `file:line`. Nothing planned, nothing aspirational.
2. **Verified citations.** Every paper named in Parts II–IV had its authors,
   venue, and year confirmed against a primary or bibliographic source while
   writing. Part V lists them with that status. Where I could not verify
   something I say so instead of guessing.
3. **Honest gaps.** Part III is the list of places where the biological analogy
   *doesn't* hold in this codebase — not as self-criticism, but because those
   gaps are exactly where Part IV's proposals get their justification.

---

## Part I — What is actually implemented

A one-screen inventory, so the pairings in Part II have something concrete to
attach to.

| # | Mechanism | Where | Key parameters |
|---|-----------|-------|----------------|
| 1 | Heading-aware chunking with identity-stable ids | `vault/chunker.py:37` | 512 tok max, 50 tok overlap, h1–h3 splits |
| 2 | Dense + sparse hybrid seed retrieval, server-side RRF | `retrieval/seed.py:11` | BGE-small-en-v1.5, BM42, top_k=8 |
| 3 | Graph expansion by weighted Dijkstra over `1/weight` | `retrieval/expand.py:10`, `stores/graph.py:493` | depth=2 hops, decay=0.5 |
| 4 | Team-draft interleaving of the two rankings | `retrieval/interleave.py:47`, `orchestrator.py:_pool` | `retrieval.interleave`, default on |
| 4b | Score merge, max-dedupe, greedy token-budget trim | `retrieval/orchestrator.py` | 6000 tok budget, floor of 3 candidates |
| 5 | Path reinforcement from implicit selection | `retrieval/weights.py:45` | α=0.5, hop_decay=0.7, ceiling=5.0 |
| 5b | Retrieval-policy ledger — which ranking wins picks | `stores/policy.py`, `pipeline/policy.py` | impressions + wins, `daemon policy` |
| 6 | Half-life decay of reinforced edges | `retrieval/weights.py:99` | 30-day half-life, toward 1.0 |
| 7 | Activation ledger — per-query sparse note fingerprints | `stores/activations.py:134` | rank-based strength, per-source weights |
| 8 | IDF weighting + two-pass inverted-index cosine | `stores/activations.py:idf`, `:similar` | max_df_ratio=0.25, df-pruning above 50 queries |
| 9 | Fingerprint recall — "you've been here before" | `pipeline/recall.py:32` | top_k=3, min_score=0.15, 180-day lookback |
| 10 | Emergent themes — HDBSCAN over fingerprint cosine | `analysis/themes.py:70` | min_cluster_size=3, leave-one-out filter 0.10 |
| 11 | Theme stability — centroid matching, dormancy, churn | `analysis/themes.py:163`, `:235` | match threshold 0.60, churn = 1 − mean Jaccard |
| 11b | Threshold sweep against replayed ledger history | `analysis/theme_tuning.py` | `daemon themes tune`, read-only |
| 12 | Structural analysis — Louvain (connectivity-repaired), betweenness, bridges | `analysis/structural.py:133`, `:185`, `:212` | 8 communities, BC sample k=200, warm > 1.5 |
| 12b | Dangling wikilinks as prospective memory | `analysis/todos.py` | `daemon graph todos` |
| 13 | Counterfactual weight-evolution replay on a shadow graph | `analysis/structural.py:365` | 7-day lookback |
| 14 | Snapshot isolation — writes raise, not warn | `stores/snapshot.py` | 14-day retention |
| 15 | Observer LLM letter with prior-letter continuity | `pipeline/agent_observe.py`, `llm/agents.py:357` | Opus, 4 prior letters, ~400–700 words |
| 16 | Write-back discipline — containment, grace, snapshots | `vault/writer.py` | 30-min grace, `daemon: ignore` opt-out |

---

## Part II — The pairing

Each subsection: **what the code does** → **the memory/cognitive science it lands
in** → **the AI/ML lineage** → **what the pairing tells you**.

### II.1 — Chunking with heading context and identity-stable ids

**What runs.** `chunk_note` splits first on markdown headings (h1–h3), then
re-splits any oversized piece with a recursive character splitter at a 512-token
budget with 50 tokens of overlap. The chunk id is
`sha1(note_uuid :: heading_path :: index :: text)` — derived from the note's
*identity*, never its path (`chunker.py:20-28`), which is what lets a rename
become a payload update rather than a re-embed.

**Cognitive pairing.** The heading path is a **retrieval cue hierarchy**. Tulving
and Thomson's *encoding specificity principle* (Psychological Review, 1973) says
recall succeeds to the degree that cues present at retrieval overlap cues present
at encoding — so storing the `h1 > h2 > h3` trail alongside the text preserves
cues the raw prose has already dropped. The overlap window is a crude
**contextual bridge**: it keeps adjacent chunks sharing surface features, which
is what supports the contiguity effect in free recall (Howard & Kahana, *Journal
of Mathematical Psychology*, 2002).

Identity-stable ids map to something more specific: the **hippocampal index**.
Under indexing theory, the hippocampus stores a pointer, not the content; the
content lives distributed in cortex. Your `note_uuid` is exactly that — a stable
pointer that survives the content moving. This is why the M-mem UUID cutover was
architecturally the right call, not merely an engineering convenience.

**ML lineage.** Recursive/structure-aware splitting is standard RAG practice, but
the identity-stable id is not: most pipelines key chunks by path+offset and
therefore re-embed on every move. The closest published relative is the
`passage_id`-stable indexing in late-interaction retrievers (Khattab & Zaharia,
ColBERT, SIGIR 2020), where stable ids are load-bearing for the index.

**What it tells you.** The chunker is already doing cue preservation. The obvious
unexploited cue is **time** — chunks carry no encoding timestamp into retrieval,
which is the gap Part III.3 names.

### II.2 — Hybrid seed retrieval: dense + sparse, fused by RRF

**What runs.** `seed_search` embeds the query with BGE-small-en-v1.5, encodes it
again with BM42 (`Qdrant/bm42-all-minilm-l6-v2-attentions`), and hands both to
Qdrant, which fuses the two rankings server-side with Reciprocal Rank Fusion.
Default `top_k=8`, `embeddings.hybrid: true`.

**Cognitive pairing.** Two retrieval routes over one memory is not an
implementation detail in the brain either — it is the standard finding that
recognition and recall are dissociable, and that **familiarity** (fast,
signal-strength, gist-like) and **recollection** (slower, cue-specific,
detail-bearing) are separable processes. Dense embeddings behave like
familiarity: they respond to overall semantic gist and are robust to paraphrase.
Sparse lexical matching behaves like recollection of a specific surface form: it
fires hard on the exact rare term and not at all on its synonym. A system with
only the dense route has the failure mode of a person who remembers the sense of
what they read and not one proper noun in it.

RRF's rank-based combination — indifferent to each system's score scale — is a
reasonable analogue of the brain's practice of comparing evidence across
modalities that have no common unit.

**ML lineage.** RRF is Cormack, Clarke & Buettcher, SIGIR 2009, *"Reciprocal rank
fusion outperforms Condorcet and individual rank learning methods"* — a two-page
poster whose central claim is that a parameter-free rank combination beats both
Condorcet fusion and learned rank aggregation. That the simplest thing wins is
the reason it is now the default in essentially every hybrid vector database.
Sparse-neural retrieval descends from Spärck Jones's IDF (1972) through BM25
(Robertson & Zaragoza's monograph, 2009) to learned sparse expansion (SPLADE;
Formal, Piwowarski & Clinchant, SIGIR 2021). BM42 is Qdrant's attention-derived
variant — a vendor engineering artifact rather than a peer-reviewed method, which
is worth knowing when you reason about its behaviour.

**What it tells you.** You have the two routes, but you fuse them **once, at
fixed weight, with no query-dependent arbitration**. Humans arbitrate: a question
containing a rare proper noun should lean lexical; a vague "what was that thing
about…" should lean dense. See Part IV.2.

### II.3 — Graph expansion as weighted spreading activation

**What runs.** For every seed, `neighbors_within` (`graph.py:493`) does two
passes. First an unweighted BFS establishes a **hop budget** — nothing beyond
`depth=2` integer hops is reachable, full stop. Then, if `weighted=True`,
Dijkstra runs over an edge cost of `1 / max(parallel edge weights)`, floored so
cost never exceeds 10 (`graph.py:548-552`). Expanded chunks score
`seed_score × decay^distance` with `decay = 0.5` (`expand.py:67`). Daemon-authored
`theme/` tags are excluded from traversal so the system's own conclusions cannot
steer the retrieval that produced them.

**Cognitive pairing.** This is **spreading activation**, in almost its textbook
form. Collins & Loftus (*Psychological Review*, 1975) proposed that activating a
concept in semantic memory spreads activation to linked concepts, attenuated by
link strength and by distance from the source. Your `decay^distance` *is* the
attenuation term; your learned `weight` *is* the link strength; your `depth=2`
cap is the empirical observation that spreading activation is sharply limited in
extent rather than propagating indefinitely.

The two-pass structure — hard hop cap first, then weighted distance — is a more
principled choice than it may look. Pure weighted spread lets a single strongly
reinforced chain pull in material from arbitrarily far away, which is
associative rumination, not recall. Capping hops and *then* weighting is the
architecture's way of saying "strength modulates ranking within a bounded
neighbourhood," which is closer to what the psychological data supports.

The seed→expand division also maps cleanly onto Raaijmakers & Shiffrin's SAM
model (*Psychological Review*, 1981): retrieval as **cue-dependent probabilistic
sampling** from an associative network, where the cue set determines which images
are sampled and associative strengths determine sampling probability. Your seeds
are the sampling step; your expansion is the associative structure that decides
what gets sampled alongside them.

**ML lineage.** Graph-augmented retrieval is now a busy field. The most direct
relative is **HippoRAG** (Gutiérrez et al., NeurIPS 2024), which is explicitly
built on hippocampal indexing theory and runs **Personalized PageRank** over an
LLM-built knowledge graph at query time — the same architectural bet you made,
with a different propagation operator. **GraphRAG** (Edge et al., Microsoft,
2024) takes the community-summarisation route instead, which is closer to your
consolidation phase than to your expansion. Random-walk-with-restart as a
relevance measure on graphs goes back to Tong, Faloutsos & Pan (ICDM 2006).

**What it tells you.** Decayed Dijkstra and Personalized PageRank answer *related
but different* questions. Dijkstra asks "how cheap is the single best route to
this note?"; PPR asks "how much of a random walker's time is spent here?" — which
aggregates over *all* routes and is therefore robust to a single lucky edge.
Given that your edge weights are learned from a sparse and noisy signal, the
aggregate is likely the better estimator. Part IV.6.

### II.4 — Adaptive edge weighting: reinforcement and decay

**What runs.** When the user picks a candidate, `apply_selection`
(`weights.py:45`) finds the shortest *unweighted* path from the seed note to the
selected note on the undirected projection and bumps every edge along it by
`α × hop_decay^hop_index` — 0.5 at the first hop, 0.35 at the second, 0.245 at
the third — clamped at a ceiling of 5.0. Every parallel edge in both directions
gets the bump, and each is stamped `last_reinforced_at`.

Nightly, `decay_unused_edges` (`weights.py:99`) pulls every stamped edge back
toward the 1.0 baseline: `w ← 1 + (w − 1) × 0.5^(days_since / 30)`. Edges never
reinforced are skipped — they are already neutral.

**Cognitive pairing.** Three separate findings converge here.

*Hebbian potentiation with a ceiling.* "Cells that fire together wire together"
(Hebb, *The Organization of Behavior*, 1949) is the reinforcement rule. The
ceiling at 5.0 is the saturation that real synapses exhibit — LTP does not
increase without bound, and a model without a ceiling produces runaway
winner-take-all dynamics.

*Credit decaying with distance from the reinforced event.* Your
`hop_decay^hop_index` is an eligibility trace in all but name: the edge nearest
the outcome gets most of the credit, and credit falls off along the path. This is
the same structure as TD(λ) in reinforcement learning and as the dopaminergic
reward-prediction-error signal it was built to model.

*Forgetting as an active, adaptive process.* The decay half-life is the piece
with the deepest literature. Ebbinghaus (1885) established the forgetting curve;
Anderson & Schooler (*Psychological Science*, 1991) made the far stronger claim
that the shape of human forgetting is not a limitation but an **adaptation to the
statistics of the environment** — the probability that an item will be needed
again decays with time since last use in exactly the way memory strength does,
across corpora as different as New York Times headlines, parental speech, and
email. Your decay toward 1.0 is a bet on the same statistic. Tononi & Cirelli's
synaptic homeostasis hypothesis supplies the reason to do it *at night
specifically*: sleep-dependent global downscaling renormalizes synaptic weight so
that the day's potentiation does not saturate the network.

Bjork & Bjork's *New Theory of Disuse* (1992) adds a distinction your model
currently collapses: **storage strength** (durability, which only ever
increases) versus **retrieval strength** (accessibility right now, which
fluctuates). Your single `weight` scalar is doing both jobs. That is a real
modelling decision with consequences — see Part III.4.

**ML lineage.** Learning a ranker from clicks is Joachims (KDD 2002) and the
large literature after it. The specific hazard your architecture has already run
into — **position bias** — is the subject of Joachims, Swaminathan & Schnabel,
*"Unbiased Learning-to-Rank with Biased Feedback"* (WSDM 2017), which shows that
naïvely training on clicks converges to the *presentation* policy rather than to
relevance, and gives an inverse-propensity-weighted estimator that does not.
Radlinski, Kurup & Joachims (CIKM 2008) show that no absolute click metric
reliably reflects retrieval quality at realistic sample sizes, and introduce
Team-Draft interleaving as the alternative.

**What it tells you — and what was done about it.** This was the sharpest
finding in the first draft, and it landed directly on the open design question
recorded in PROJECT_MANAGEMENT.md ("seeds dominate the candidate pool and
seed-picks reinforce nothing"). The IR field went through this exact problem
twenty years ago and the answer is *not* to tune the reinforcement constant. It
is that clicks measure examination and relevance jointly, so you must either
model the examination term (propensity weighting) or design the presentation so
that it cancels (interleaving).

**Interleaving is now implemented** — `retrieval/interleave.py`, on by default
via `retrieval.interleave`. Propensity weighting was the other option and was
rejected on evidence: it needs an examination-probability estimate per rank,
which needs either result randomization or a large click corpus, and Radlinski
et al.'s negative result is specifically that absolute click metrics do not
track quality at realistic sample sizes. At one user there is no corpus to
estimate from. Team draft sidesteps the estimate entirely.

The mechanism: the seed ranking and the expansion ranking alternate picks,
whichever team has drafted fewer goes next, ties are broken by a coin, and each
drafted candidate carries the team that took it (`RetrievedChunk.team`,
persisted into the feedback row by `build_retrieval_summary`). Because both
teams contribute equally often and their positions are symmetric in
expectation, a pick is an unbiased comparison of the two policies.

The second half is `stores/policy.py`, and it is what actually closes the loop.
`DaemonCore.endorse` now records a win against the picking candidate's team.
A seed pick still reinforces no edge — a zero-length path has none — but it is
no longer discarded: it is recorded as evidence that the seed policy beat the
expansion policy on that query. `daemon policy` shows the win rates. The store
deliberately only counts; it does not feed back into ranking, because tuning the
draft from its own win rate would be a closed loop with nothing outside it.

### II.5 — The activation ledger and query fingerprints

**What runs.** Every retrieval records which notes fired, by which route, at what
strength: `strength = STRENGTH_BY_SOURCE[source] × 1/log₂(rank + 1)`
(`activations.py:107-117`). A user-confirmed pick is worth 1.5, a vector seed
1.0, a graph expansion 0.6, an ambient recall injection 0.3. Rank-based, not
score-based, and the docstring gives the correct reason: raw scores are
incomparable across the dense→hybrid transition, across model changes, and across
backfills, while rank survives all three.

The per-query **fingerprint** is that vector, IDF-weighted
(`v × log(1 + N/(1 + df))`, `activations.py:284`) and L2-normalized. `similar()`
(`activations.py:290`) finds related queries by an inverted-index scan in SQLite:
gather every query that touched any of the probe's notes, accumulate dot
products, divide by the stored norm.

**Cognitive pairing.** A fingerprint is a **context vector**, and this is the most
interesting structure in the system. Howard & Kahana's Temporal Context Model
(2002) represents the state of memory at retrieval as a drifting vector, and
explains recency and contiguity effects as consequences of how much that vector
overlaps the vector present at encoding. Polyn, Norman & Kahana extended this to
source and semantic context (CMR, *Psychological Review*, 2009). Your fingerprint
is a context vector over *note space* rather than over time or item space — but
the retrieval logic is identical: two events are related when their context
vectors overlap, regardless of surface form.

Which means the docstring's claim is stronger than it looks. "A question worded
nothing like an earlier one, but which lights up the same notes, is the same
underlying concern" is precisely **encoding specificity** (Tulving & Thomson,
1973): the cue that succeeds is the one that reinstates the encoding context, not
the one that resembles the words. You built a context-reinstatement retrieval
system without, as far as the code comments show, setting out to.

The rank-based `1/log₂(rank+1)` strength is worth its own note: it is the
discount function from nDCG, and it is also a good approximation of the
serial-position advantage — the top item of a list is disproportionately
retrievable, and the advantage falls off logarithmically rather than linearly.

**ML lineage.** IDF weighting over an inverted index with cosine scoring is the
vector-space model (Salton), and the `log(1 + N/(1+df))` form is a smoothed
Robertson-style IDF. Doing it over *activation patterns* rather than over terms
is unusual; the nearest published relative is collaborative filtering's
item-based similarity, where two users are similar because they touched the same
items. Read that way, your fingerprint recall is **collaborative filtering with a
population of one, where the "users" are your own past questions**. That framing
is genuinely useful — it tells you which parts of the CF literature transfer
(implicit-feedback weighting, popularity debiasing, cold-start) and which do not
(anything relying on cross-user signal).

**What it tells you.** Two things, one a bug that has since been fixed and one
a design lever that is still open.

*The bug (fixed 2026-07-28).* `record()` stored an `l2_norm` computed from
**pre-IDF** strengths, and `similar()` accumulated an **IDF-weighted** dot
product and divided by that norm. The probe side was weighted; the candidate
side was not. The consequence was not random noise — it systematically inflated
queries built from rare notes relative to queries built from common ones, and
every downstream consumer (recall → themes → the observer letter) inherited it.

The fix required a structural change, not a coefficient. A candidate's magnitude
cannot be computed from the notes it happens to share with the probe, so
`similar()` now runs two passes: the first finds which queries share a note, the
second reads those queries' **full** activation sets and weights them with the
same `idf()` the probe was built from. The old single pass was fast *because* it
was wrong — reusing the stored norm avoided ever reading the rest of the
candidate. `queries.l2_norm` is still written, since a vector backend would want
it, but is no longer used for scoring. Two invariants are now pinned by tests: a
query whose fingerprint matches another's exactly scores 1.0, and score(A→B)
equals score(B→A) even when the two are built from notes of very different
document frequency.

One consequence worth watching: scores are now true cosines and are generally
*lower* than the old inflated ones for rare-note queries, so
`memory.min_score = 0.15` may want revisiting against real usage.

*The lever, still open.* `STRENGTH_BY_SOURCE` is a hand-set salience table. It
is the only place in the system that says "this evidence matters more than that
evidence," and it is five constants chosen by judgement. Part IV.9.

### II.6 — Fingerprint recall

**What runs.** `recall_related` (`recall.py:32`) takes the current query's
fingerprint, finds the top 3 past queries above cosine 0.15 within a 180-day
window, restricted to *intentional* surfaces (CLI, GUI, Hermes tool-call, MCP —
never ambient prefetch), and resolves shared note UUIDs to paths for display. The
result is both injected into the model's context and shown to the user as a
"You've been here before" panel, toggled independently.

**Cognitive pairing.** This is **involuntary autobiographical memory** given a
deliberate trigger — the experience of a present situation cueing an earlier one
without the person having tried to recall it. That the two toggles are
independent (inject / display) is a design choice with a clinical analogue:
whether to *show the person* that they have been here before is a different
decision from whether the system should act on it, and for a user with cognitive
decline the display is arguably the whole therapeutic point.

The 180-day window and the exclusion of ambient surfaces together implement
something the literature would call **source monitoring** — distinguishing "I
asked this" from "the system looked this up on my behalf." Failures of source
monitoring are a well-documented feature of confabulation, and a memory prosthetic
that conflated its own lookups with the user's questions would manufacture false
autobiographical memories at scale. The `INTENTIONAL_SURFACES` filter is, in that
light, a safety mechanism and not merely a quality filter.

**ML lineage.** This has no close published relative in the RAG literature, which
is a point in the design's favour. Agent-memory systems from the last three years
retrieve *content* by recency, importance, and semantic relevance — Park et al.,
*"Generative Agents: Interactive Simulacra of Human Behavior"* (UIST 2023) is the
canonical instance, scoring memories as an equal-weighted sum of exponential
recency decay, an LLM-assigned importance score, and embedding cosine. None of
them retrieve *past retrievals* by activation-pattern overlap. You are indexing
the system's own history of attention, which is a different object.

**What it tells you.** The comparison to Generative Agents is instructive in one
direction: they have an **importance** term and you do not. Their importance is
LLM-assigned at write time and admittedly crude, but it is a real attempt at the
salience gap. Part III.1.

### II.7 — Emergent themes: HDBSCAN over fingerprints

**What runs.** Inside `daemon consolidate`, `cluster_fingerprints`
(`themes.py:70`) builds a dense `1 − cosine` distance matrix over up to 4000
fingerprints and runs HDBSCAN with `metric="precomputed"`,
`min_cluster_size=3`, and — deliberately — `allow_single_cluster=False`. Members
then face a **leave-one-out** filter: a query must resemble the centroid built
*without its own contribution* at cosine ≥ 0.10, or it is dropped. Centroids are
the top-12 notes by summed weight, L2-normalized.

**Cognitive pairing.** Clustering the traces of retrieval, offline, at rest, into
higher-order structures the system did not previously have is **schema
formation** — Bartlett's (1932) account of memory as organized around
reconstructive schemata rather than stored as episodes. More specifically it is
the *systems consolidation* half of the complementary-learning-systems account
(McClelland, McNaughton & O'Reilly, *Psychological Review*, 1995): fast
hippocampal storage of individual episodes, slow neocortical extraction of the
regularities across them, with the second process running offline because doing
it online would interfere with the first.

The single most defensible decision in this module is keeping HDBSCAN's noise
label. The docstring states it plainly — "most queries should not get a theme" —
and that is right for reasons beyond tidiness. A system that forces every episode
into a schema is a system that confabulates; the ability to leave an experience
unassimilated is a feature of healthy memory, not a shortfall.

**ML lineage.** HDBSCAN is Campello, Moulavi & Sander, PAKDD 2013,
*"Density-Based Clustering Based on Hierarchical Density Estimates"* — a
hierarchical density estimate from which a flat partition is extracted by a
cluster-stability criterion. Its relevant properties for you: it does not require
`k`, it admits noise, and it handles clusters of differing density. The
leave-one-out member filter is not from that paper — it is your own guard against
HDBSCAN's small-corpus degeneracy, and the reasoning in the comment at
`themes.py:120-124` is correct.

**What it tells you.** The `O(n²)` distance matrix at 4000 fingerprints is 16M
float32 entries built in a Python double loop (`themes.py:97-101`). At single-user
scale that is survivable — roughly 8M cosine calls, each over sparse dicts — but
it is the first thing that will hurt, and it is nightly. A sparse
inverted-index construction of the same matrix would exploit the fact that most
pairs share zero notes and score exactly 0.0. Part IV.4 note.

### II.8 — Theme stability: matching, dormancy, churn

**What runs.** `reconcile_themes` (`themes.py:163`) computes cosine between every
new cluster centroid and every existing theme centroid, sorts all pairs
descending, and greedily claims matches above 0.60 — each cluster and each theme
claimable once. A match inherits the existing theme's id, label, and summary, so
no LLM call and no visible churn. Unmatched clusters are created with a
provisional label pending naming; unmatched themes are marked dormant, never
deleted. `_churn` (`themes.py:235`) reports `1 − mean Jaccard` of theme membership
against the previous run, with dormant themes scored 0.

**Cognitive pairing.** The distinction the module is enforcing — the *label* must
be stable even though the *partition* cannot be — is the distinction between a
concept and its extension. A person's sense of "my work stuff" persists across
years during which essentially every constituent memory has changed. Dormancy
rather than deletion is the same commitment: a theme you have not returned to in
months is not gone, it is **inaccessible but stored**, which is exactly the
Bjork & Bjork storage/retrieval-strength distinction applied at the schema level.

Reporting churn honestly to the observer, with instructions to flag readings as
provisional above ~0.5, is **metacognitive calibration** — knowing what you know.
It is the rarest property in this class of system and it is implemented here as a
number that flows into the prompt (`agents.py:370`).

**ML lineage.** Two literatures apply.

The matching step is an **assignment problem**. Your greedy best-first pass is a
reasonable approximation, but the optimal solution to maximum-weight bipartite
matching is the Hungarian algorithm (Kuhn, 1955), available as
`scipy.optimize.linear_sum_assignment`. Greedy can be arbitrarily worse than
optimal: one high-scoring pair claiming a theme can force two good pairs to fail,
producing a spurious "new theme" plus a spurious "dormant theme" in the same run
— and both are user-visible.

The stability problem in general is **evolutionary clustering** (Chakrabarti,
Kumar & Tomkins, KDD 2006), which formalizes exactly your dilemma as a single
objective with two terms: *snapshot quality* — how well the clustering fits the
current data — and *history cost* — how far it moved from the previous
clustering. Your `_churn` is a measurement of the history cost that you report
but do not optimize. That is the gap Part IV.8 addresses.

**What it tells you.** You independently arrived at the right problem
decomposition and are one step short of the published solution. Swapping greedy
for Hungarian is a few lines against a BSD-licensed dependency you may already
have; adopting the evolutionary-clustering objective is a real project.

**What was done (2026-07-28).** Neither of those yet — but the prerequisite for
choosing between them shipped. `DEFAULT_MATCH_THRESHOLD = 0.60` was the
highest-leverage untuned constant in the system, and `analysis/theme_tuning.py`
(`daemon themes tune`) now turns it into a measurement: it replays the user's
own ledger in sequential cumulative windows, reconciles at each candidate
threshold against a throwaway store, and reports mean churn, surviving themes,
and the created/matched/dormant split per threshold. Two disciplines matter and
are pinned by tests — it never touches the live theme store (it runs against the
real ledger, and a tuning run that minted themes would change what it measures),
and each threshold starts from an empty store (a shared one would let the first
threshold's themes seed the second, making the sweep a measurement of evaluation
order).

Two implementation findings are worth recording because both were wrong first:

- The window has to be bounded in **SQL**, not by filtering afterward. Clustering
  the whole corpus and discarding clusters that reach past the cut gives a
  different answer, because the partition itself was computed with knowledge of
  queries that had not happened yet. `query_fingerprints` gained an `until`
  parameter for this; consolidation never needs it, since "now" is always its
  right edge.
- The **first window's churn must be excluded** from the mean. `reconcile_themes`
  reports 0.0 when there is no previous partition, which means "nothing to
  compare against" and not "perfectly stable". Averaging it in flattered every
  threshold, and by an amount that shrank as the window count grew — so the same
  ledger would have scored differently at `--windows 2` and `--windows 8`, making
  the sweep partly a measurement of its own parameter.

### II.9 — Structural analysis: communities, bridges, orphans

**What runs.** `compute_report` (`structural.py:61`) runs Louvain communities
(`nx.community.louvain_communities`, `seed=0`) on the undirected projection with
daemon-authored tag nodes removed; sampled betweenness centrality with `k=200` for
bridging notes; `nx.bridges` for load-bearing links whose removal disconnects the
graph; orphans as undirected degree ≤ 1; dangling wikilink targets ranked by
in-degree; and the warmest reinforced edges above 1.5. Tags are clustering glue
but only notes are reported.

**Cognitive pairing.** Communities are **semantic neighbourhoods** — the
categorical structure of Collins & Quillian's (1969) hierarchical semantic
network, discovered bottom-up rather than declared. Bridging notes, high in
betweenness, are the ones through which activation must pass to travel between
neighbourhoods; the psychological construct closest to that is the **remote
associate**, and Mednick's classic result is that the ability to traverse between
distant semantic neighbourhoods is what creative association *is*. Surfacing a
user's own bridging notes is therefore not a graph statistic dressed up — it is a
report on where their thinking connects domains that would otherwise be separate.

Dangling wikilink targets — notes named but never written — are the most
psychologically loaded object in the report. They are **prospective memory**: an
intention formed and not yet executed. The AI-suggestions section of
PROJECT_MANAGEMENT.md already frames these as "notes you keep meaning to write,"
which is the correct reading, and the planned `daemon graph todos` would make the
system a prospective-memory prosthesis — a capability with direct relevance to
the assistive use case, since prospective memory degrades early in both normal
ageing and mild cognitive impairment.

**ML lineage.** Louvain is Blondel, Guillaume, Lambiotte & Lefebvre (*JSTAT*,
2008) — greedy modularity optimization by local moving and aggregation.
Betweenness centrality is Freeman (1977) with Brandes's (2001) fast algorithm
underneath NetworkX; the `k`-sampling you use is the standard approximation.
Both are load-bearing in the modern graph-RAG stack: GraphRAG builds its
hierarchy from community detection over an entity graph.

**What it tells you — and what was done (2026-07-29).** One correctness caveat
that mattered, now fixed. Traag, Waltman & van Eck (*Scientific Reports*, 2019) — *"From Louvain to Leiden: guaranteeing
well-connected communities"* — show that Louvain can produce **arbitrarily badly
connected and even disconnected communities**, measuring up to 25% badly
connected and up to 16% disconnected in their experiments. A disconnected
"community" fed to the observer becomes a sentence in a letter to your user
asserting a relationship between notes that has no path between them. That is a
real, cited failure mode of a component you were shipping.
`structural.split_disconnected` now repairs it (§IV.3).

The dangling-wikilink half of this report also finally escaped it:
`daemon graph todos` (`analysis/todos.py`) surfaces unwritten targets ranked by
how many notes are waiting on them, and names the waiting notes so the entry is
actionable. As §II.9 argues above, that makes the daemon a **prospective-memory**
prosthesis — the one memory system it touches that nothing else in the pipeline
serves, and one that degrades early in both normal ageing and MCI.

### II.10 — Snapshot isolation and the consolidation phase

**What runs.** `daemon consolidate` follows a fixed order (`agent_observe.py:2`):
snapshot → analyze → cluster themes → write letter → refresh index → decay. The
snapshot bundle is a Qdrant native snapshot, a `copy2` of the graph pickle, an
online-safe SQLite `.backup()` of the feedback DB, and a manifest.
`open_readonly` returns handles whose every write path *raises* — the analyzer
cannot contaminate live state even by mistake. Decay is the single place
consolidation touches the live graph, and it runs last, after analysis has read
the frozen copy.

`simulate_evolution` (`structural.py:365`) deep-copies the snapshot graph into a
shadow store, replays every `candidate_selected` event from the last 7 days
through `apply_selection`, and diffs the result against the baseline — a
counterfactual preview of what the next cycle would do, computed without doing
it.

**Cognitive pairing.** This is the piece of the architecture with the strongest
biological warrant, and it is worth being precise about which parts map to what.

*The offline phase itself* is systems consolidation (McClelland et al., 1995):
regularity extraction must happen offline because doing it during encoding causes
catastrophic interference with the episodes being encoded. Your snapshot boundary
is the computational statement of that constraint.

*Replay against a frozen copy* is hippocampal replay — the reactivation of
waking activity sequences during slow-wave sleep, first shown in rodent place
cells by Wilson & McNaughton (*Science*, 1994) and the substrate for the
consolidation account reviewed by Diekelmann & Born (*Nature Reviews
Neuroscience*, 2010).

*Decay running last, after analysis* is synaptic homeostasis (Tononi & Cirelli):
the renormalization happens after the day's traces have been read out, not
before, or the read-out would be of an already-downscaled network.

*`simulate_evolution` specifically* is the one that has no standard name in this
domain and deserves one. Running a policy forward on an internal model to see
what it would do, without acting, is **mental simulation** — and the fact that
your version is fully isolated (deep copy, read-only bundle, no live mutation)
makes it a stronger analogue than most, since the defining property of
imagination is that it does not commit.

**ML lineage.** Experience replay from a buffer is the DQN lineage (Mnih et al.,
2015). Off-policy counterfactual evaluation — estimating what a policy *would*
have done from logged data — is the counterfactual-learning literature that also
supplies Part IV.7's answer. Snapshot isolation as a correctness property is
straight database systems, and it is unusual and commendable to see it applied to
an ML pipeline's analysis phase.

**What it tells you.** This subsystem is, in my reading, the most
architecturally mature thing in the repository and the closest to the vision
document's ambitions. The one thing it lacks relative to the biology is a **REM
analogue**: your consolidation extracts regularities from what happened, but does
not recombine remote material to propose associations that never occurred. Part
IV.13.

### II.11 — Write-back discipline

**What runs.** `vault/writer.py` enforces containment (no writes outside the
vault), honours `daemon: ignore` frontmatter, applies a 30-minute grace period so
the daemon never edits a file the user is actively in, takes snapshot backups
before mutation, and — since M-mem-8 — appends frontmatter list values
*textually*, preserving flow-vs-block style, comments, CRLF, and trailing
newlines rather than round-tripping through PyYAML.

**Cognitive pairing.** The relevant frame here is not a memory mechanism but a
clinical constraint. Memory is **reconstructive**: Bartlett (1932) showed that
recall is re-assembly rather than replay, and Nader, Schafe & LeDoux (*Nature*,
2000) showed that reactivating a consolidated memory returns it to a labile state
in which it can be altered before re-storage. A system that rewrites the user's
own notes is intervening in a reconstructive process. For a user whose
autobiographical memory is degrading and who therefore cannot independently
verify what their notes used to say, the daemon's edits become
indistinguishable from their own. The containment rules, the grace period, the
opt-out, and the byte-preserving writer are, in that light, not hygiene — they
are the ethics of the project expressed as code.

**Evidence base for the whole enterprise.** Worth recording explicitly, because
it is the strongest external support for the assistive claim in CLAUDE.md: Berry,
Kapur, Williams, Hodges, Watson, Smyth, Srinivasan, Smith, Wilson & Wood,
*"The use of a wearable camera, SenseCam, as a pictorial diary to improve
autobiographical memory in a patient with limbic encephalitis"* (*Neuropsychological
Rehabilitation*, 2007, 17(4–5), 582–601). The patient recalled roughly 80% of
recent personally-experienced events after reviewing passively-captured images,
against a written-diary control — and the effect persisted after review stopped,
implying genuine consolidation rather than momentary cueing. The mechanism was
*cue reinstatement*: rich, personally-specific cues from the original encoding
context. That is the same mechanism your fingerprint recall implements over text.
The related fMRI follow-up (Hodges/Berry group, 2009) is indexed in PubMed as
*"The neural basis of effective memory therapy in a patient with limbic
encephalitis."*

---

## Part III — Where the pairing is honest about a gap

Four places where the analogy does not currently hold in this code. Each is a
premise for Part IV.

### III.1 — No salience layer (the amygdala gap)

The earlier research document named this as "probably the single most important
design opportunity," and it remains unimplemented. The system's only notion of
importance is `STRENGTH_BY_SOURCE` — five hand-set constants describing *which
retrieval route* found a note, which is a statement about the machinery, not
about the user. Nothing anywhere asks whether a note *mattered*.

Amygdala modulation of hippocampal consolidation is one of the better-established
findings in the field: emotionally arousing material is consolidated
preferentially, and the effect is mediated by noradrenergic signalling into the
hippocampus (Cahill & McGaugh's programme through the 1990s). The functional
consequence is that human memory is not a uniform recorder — it allocates storage
by significance.

Retrieval by recency and lexical match, which is what a memory aid without a
salience layer offers, is precisely the wrong affordance for the target user. A
person with cognitive decline does not need help finding the note they wrote
yesterday. They need help finding the one that mattered.

### III.2 — Credit assignment — *closed 2026-07-28*

~~Recorded in PROJECT_MANAGEMENT.md as the deepest open question: seeds dominate
the candidate pool, and a seed pick reinforces nothing because `apply_selection`
no-ops when seed == selected.~~

Addressed by team-draft interleaving plus the policy ledger (§II.4, §IV.7). The
diagnosis was that this was never a parameter problem: a click is a joint
observation of *examination* and *relevance*, rank-1 gets examined far more than
rank-8 regardless of quality, and training on the raw signal converges toward
reproducing your own presentation order (Joachims et al., 2017). Interleaving
makes the two rankings' positions symmetric by construction, so the pick's team
is an unbiased policy comparison with no propensity model.

**What remains genuinely open here**, and should not be forgotten now that the
loop closes mechanically: interleaving removes *position bias*, not *sampling
error*. At one user generating a handful of picks a week, the win rates in
`daemon policy` will be noise for a long time — the command says so below twenty
picks. And the decision about what to *do* with a measured imbalance is
deliberately not made: nothing feeds the win rate back into the draft ratio or
the expansion decay, because a policy tuned on its own win rate is a closed loop.
Watch the numbers for a few months before wiring anything to them.

### III.3 — No episodic time-binding

The earlier document flagged that "episodic memory has no clean analog because
event-time-place binding is harder than embedding similarity," and the
implementation confirms it. `Chunk` (`models.py`) carries `note_uuid`,
`note_path`, `heading_path`, `text`, `chunk_index`, `tags`, `wikilinks` — and no
time. The ledger timestamps *queries*; nothing timestamps the *content*, and the
retrieval path never sees a date. "What was I working on last spring?" is not a
question this architecture can currently answer by retrieval; it can only answer
it by luck, if the note happens to say so in prose.

Tulving's (1972) original semantic/episodic distinction turns on exactly this
property: episodic memory is memory *for events located in subjective time*.
Without a temporal index the system is a semantic memory with an episodic
interface.

### III.4 — Forgetting exists, but only for edges, and only as one scalar

`decay_unused_edges` is real forgetting and rarer than it should be in this class
of system. But its scope is narrow in two ways.

*Scope.* Only graph edges decay. Chunks never do; the ledger only compacts, never
forgets; themes go dormant but their centroids persist unchanged. The vault
itself grows monotonically. A system whose only forgetting is on one of five
stores accumulates exactly the undifferentiated mass the earlier document warned
against.

*Representation.* A single `weight` per edge is doing the work of both storage
strength and retrieval strength (Bjork & Bjork, 1992). Collapsing them makes one
useful behaviour unrepresentable: an association that is *durable but currently
inaccessible* — the thing you have not thought about in a year and would
recognize instantly. With one scalar, "not recently used" and "not important"
are the same state, and the decay curve cannot distinguish a topic you have
finished with from one you will return to.

The fixed 30-day half-life compounds this: it applies the same forgetting rate to
a passing curiosity and to a decade-long preoccupation. Anderson & Schooler's
finding was not that forgetting follows *a* curve — it was that the curve tracks
the environmental statistics of need, which differ per item.

---

## Part IV — Costed candidate methods

Prioritized. Each entry: **what it fixes** in this codebase → **the method** →
**where it lands** → **effort** → **licence**. Effort is S (a sitting), M (a
focused session or two), L (a milestone).

Licence column matters: CLAUDE.md rule 10 requires Apache-2.0 compatibility, and
one item below fails it.

### Tier 1 — Correctness and cheap wins

**IV.1 — Symmetrize the fingerprint cosine. ✅ Shipped 2026-07-28.** Implemented
as a two-pass scan rather than the stored-post-IDF-norm alternative, because a
stored norm goes stale as `df` moves and would need refreshing in consolidation.
The candidate's magnitude genuinely cannot be derived from the shared notes, so
the second pass reads the candidates' full activation sets. New public
`ActivationLedger.idf()` exists so both sides are weighted by one code path —
the asymmetry was possible precisely because the weighting lived in two places.
*Where:* `stores/activations.py`. Pinned by three tests, including one that had
to be rewritten because the first version used notes of equal document frequency
and the IDF factors cancelled, letting a broken denominator still score 1.0.

**IV.2 — MMR over the candidate pool.** *Fixes:* seed domination of the
candidate pool, from the presentation side rather than the learning side. *Method:*
Maximal Marginal Relevance (Carbonell & Goldstein, SIGIR 1998): iteratively select
the item maximizing `λ·relevance − (1−λ)·max similarity to already-selected`.
Applied to your ranked list before the token-budget trim, it guarantees the
three-candidate floor is not three chunks of the same note. *Where:*
`retrieval/orchestrator.py:76-88`. *Effort:* S — the similarity term can reuse
chunk embeddings you already have, or degrade to note-identity overlap if you
want zero extra Qdrant traffic. *Licence:* none (numpy). *Note:* this also makes
IV.7 possible, since interleaving needs a pool that isn't monoculture.

**IV.3 — Better communities than Louvain. ⚠ Corrected — no licence change needed.**
*Fixes:* the disconnected-community failure mode from Traag et al. (2019), which
can put a false relational claim into a user-facing letter.

*The correction.* The first draft of this section said every Python Leiden
implementation is GPL. That is **wrong**, and the error mattered because it
framed a licence change as the price of correctness. `leidenalg` is indeed
GPL-3.0 and `python-igraph` GPL-2.0 — but **`graspologic-native` is MIT**
(verified on PyPI: v1.3.1, June 2026), implements Leiden and hierarchical Leiden
in Rust, and is the implementation Microsoft's own GraphRAG uses. There is a
permissively-licensed Leiden and it is the one the reference graph-RAG system
runs on.

*The real constraint is Python versions, not licences.* `graspologic-native`
publishes wheels for CPython 3.9–3.13. This project declares
`requires-python = ">=3.11,<3.15"` and the librarian subtree pins `>=3.14`. On
3.14 there would be no wheel today. That is a compatibility question with a
straightforward answer (an optional extra, or waiting for the wheel) rather than
a licensing one.

**✅ Option 1 shipped 2026-07-29** — `structural.split_disconnected` splits any
Louvain community whose induced subgraph is not connected, before anything
downstream reads it as "these notes belong together". Ten lines, no dependency,
and it eliminates the disconnected case outright rather than reducing it. It
does **not** give Leiden's guarantee about merely *badly* connected communities;
whether that residue is worth a dependency is now a question real data can
answer instead of a bet.

*Remaining alternatives, in order of preference:*
  1. ~~**Post-hoc connectivity repair.**~~ **Shipped.** Keep Louvain; after it
     returns, split any community whose induced subgraph is disconnected into its
     connected components via `nx.connected_components`. This eliminates the
     *disconnected* case entirely — the more damaging half of the Traag finding —
     for about ten lines and no dependency. *Effort:* S.
  2. **Consensus clustering.** Run Louvain with `k` different seeds, build a
     co-association matrix, cluster that. More robust partitions, `k×` the cost,
     nightly so who cares. *Effort:* M.
  3. **`nx.community.greedy_modularity_communities`** (Clauset–Newman–Moore) as a
     cross-check — different failure modes, already installed, and disagreement
     between the two is itself a signal the partition is unstable. *Effort:* S.

  4. **`graspologic-native` Leiden**, once the Python-version question is
     settled. *Effort:* M. *Licence:* MIT ✓.

  *Where:* `analysis/structural.py:133-182`. *Licence summary:* NetworkX BSD-3 ✓;
  scipy BSD-3 ✓; graspologic-native MIT ✓; leidenalg GPL-3.0 ✗ / python-igraph
  GPL-2.0 ✗ (neither needed).

  *Unrelated but found here:* `python-louvain>=0.16` is a declared dependency in
  `pyproject.toml` and is **never imported** — the code calls NetworkX's built-in
  `nx.community.louvain_communities`. It can be dropped.

**IV.4 — Optimal theme matching. ✅ Shipped 2026-07-29.** Hungarian algorithm
(Kuhn, 1955) via `scipy.optimize.linear_sum_assignment` over the cluster×theme
cosine matrix, in `themes._optimal_pairs`. The threshold is applied *after*
assignment, so maximising the total can never smuggle in a pairing the
threshold rejects.

Worth recording how the test went, because it is the same trap as §IV.1: the
first version passed against the greedy implementation, since the case I
constructed happened to be one greedy solves optimally. The discriminating case
needs the *highest-scoring* pair to be the one that blocks a better total —
X↔P at 0.90 strands Y, whose only viable partner was P, where optimal pairs
X↔Q (0.44) and Y↔P (0.80) and matches both.

*Still open in this file:* the `O(n²)` Python double loop at
`themes.py:97-101` can be built from an inverted index — most fingerprint pairs
share zero notes and score exactly 0, and skipping them is close to free.

### Tier 2 — Mechanisms with real behavioural payoff

**IV.5 — Learned per-edge forgetting rates.** *Fixes:* the global 30-day
half-life applying one forgetting curve to every association (Part III.4).
*Method:* half-life regression (Settles & Meeder, ACL 2016) — model the half-life
as a function of features you already log (times reinforced, time since last
reinforcement, edge type, the notes' `note_activation_stats`) and fit it to
whether the edge was subsequently traversed-and-selected. The mature descendant
is FSRS's DSR model (difficulty / stability / retrievability), whose reference
implementation `py-fsrs` is **MIT** — verified — and therefore Apache-compatible.
*Where:* `retrieval/weights.py:99-139`, plus a features table. *Effort:* M.
*Caveat:* single-user data is thin. Start with the two-parameter version (a
per-edge-type half-life, plus a stability term that grows with reinforcement
count) before fitting anything learned; that alone captures most of the
behavioural difference.

**IV.6 — Personalized PageRank for expansion.** *Fixes:* the fragility of
single-shortest-path scoring under noisy learned weights (Part II.3). *Method:*
replace `seed_score × decay^distance` with a Personalized PageRank / random-walk-
with-restart score, personalization vector seeded on the seed notes weighted by
their retrieval scores, restart probability standing in for your decay. This is
HippoRAG's (NeurIPS 2024) core operator and the theory is Tong, Faloutsos & Pan
(ICDM 2006). `nx.pagerank(G, personalization=..., weight="weight")` does it with
**zero new dependencies** — you already call `nx.pagerank` in `GraphStore.stats`.
*Where:* `retrieval/expand.py`, `stores/graph.py:493`. *Effort:* M. *Licence:*
none. *Design note:* keep the integer hop budget as a pre-filter — PPR alone will
happily assign mass to distant hubs, which is the rumination failure mode the
current two-pass design was built to avoid. Run both and compare on your own
vault before switching; this is exactly what `simulate_evolution` is for.

**IV.7 — Debias the selection signal. ✅ Shipped 2026-07-28.** Interleaving
(Radlinski, Kurup & Joachims, CIKM 2008) was chosen over propensity weighting
(Joachims, Swaminathan & Schnabel, WSDM 2017) for the reason given in §III.2:
the propensity estimator needs randomization or a large click corpus, and a
single user supplies neither.

*Delivered as four pieces.* `retrieval/interleave.py` (`team_draft`);
`RetrievedChunk.team` carried through `build_retrieval_summary` into the
persisted feedback row, so `daemon select` can attribute a pick made days later;
`stores/policy.py` + migration 6 (`retrieval_policy_stats`) recording impressions
and wins; and `pipeline/policy.py`, a second listener on the existing retrieval
seam — separate from `ActivationRecorder` because the two answer different
questions and should fail independently. Surfaced by `daemon policy`.
Config: `retrieval.interleave`, default true.

*Three decisions worth recording.*
  - **Zero-count impressions are not recorded.** A retrieval that surfaced no
    expansion never put that policy in front of the user, and counting it would
    dilute the win rate with queries where the policy had no chance.
  - **A missing team is never guessed.** Historical rows and score-ordered pools
    have `team = None`, and those picks are excluded rather than assigned to a
    default — a pick from an unfair ordering is confounded, and counting it would
    poison exactly the measurement interleaving exists to make honest.
  - **The rng is unseeded in production.** A predictable toss would reintroduce
    the position bias the draft exists to cancel; tests inject a scripted coin so
    they can assert on draft order rather than on a distribution.

*One bug worth remembering.* The first version dispatched a draft to a team that
could already be exhausted, where drafting is a no-op — an infinite loop on the
query hot path, which would freeze the daemon on every search. The test suite
caught it by hanging. Termination is now structural: `_draft` returns whether it
moved, and exhaustion is checked before fairness.

**IV.15 — Measure the theme match threshold. ✅ Shipped 2026-07-28.**
*Fixes:* the highest-leverage judgement call in the appendix table. Not a method
from the literature so much as the empirical prerequisite for choosing one:
without a way to see what a threshold does to churn on *this user's* history,
both §IV.4 (Hungarian matching) and §IV.8 (evolutionary clustering) would be
adopted on faith.

*Method:* replay the ledger in sequential cumulative windows — cumulative
because that is what consolidate actually does, re-clustering the whole lookback
each night rather than only the new queries — reconciling at each candidate
threshold against a throwaway store, and report mean churn, surviving themes,
and the created/matched/dormant split. `recommend()` picks the lowest-churn
threshold that still finds themes, ties broken toward the higher (more
conservative) value; churn alone would recommend the degenerate low end where
everything matches because nothing is ever distinguished. It returns `None`
rather than a number when there is too little history, which is the honest
answer on a young vault.

*Where:* `analysis/theme_tuning.py`, `daemon themes tune`. *Licence:* none.
See §II.8 for the two implementation subtleties (SQL-bounded windows; excluding
the first window's churn) that were both wrong in the first attempt.

**IV.8 — Themes as an evolutionary-clustering objective.** *Fixes:* churn being
measured but not optimized (Part II.8). *Method:* Chakrabarti, Kumar & Tomkins
(KDD 2006): minimize `snapshot_cost + γ · history_cost` rather than clustering
fresh and reconciling afterward. Concretely, add the previous run's centroids as
soft anchors — e.g. seed the distance matrix with a bonus for pairs that were
co-clustered last night, tuned by γ. *Where:* `analysis/themes.py`. *Effort:*
M–L. *Licence:* none. *Sequencing:* do IV.4 first; optimal matching plus honest
churn may get you enough stability that this is not worth the complexity. Measure
before building.

**IV.9 — A salience layer.** *Fixes:* Part III.1, the amygdala gap — the single
most important gap for the assistive use case. *Method:* a per-note significance
score, orthogonal to retrieval strength, combining signals you already have or
can cheaply get:
  - *behavioural* — revisit count and dwell from `note_activation_stats`, edit
    frequency from the ingest manifest, whether the user ever endorsed a
    candidate from it;
  - *structural* — betweenness from the structural report (bridging notes are
    load-bearing in the user's own thinking);
  - *semantic* — a batch-model pass at consolidation scoring personal
    significance, in the spirit of Generative Agents' importance term but written
    to a store rather than into a prompt;
  - *surprise* — Bayesian surprise as KL divergence between the fingerprint
    distribution before and after a query, which flags notes that *changed what
    the system expected* rather than merely appearing often.

  Then feed it into ranking, into what the observer letter foregrounds, and into
  what decays slowly. *Where:* new `analysis/salience.py`, consumed by
  `orchestrator.py` and `agent_observe.py`. *Effort:* L. *Licence:* none.
  *Recommendation:* build the behavioural and structural components first — they
  are free, they are honest, and they do not require the LLM to introspect about
  the user's feelings. The semantic component should be opt-in.

**IV.10 — Episodic time-binding.** *Fixes:* Part III.3. *Method:* carry note
mtime and any date parsed from frontmatter or filename into the `Chunk` payload
and the Qdrant index (payload index on a timestamp field, cheap); add a temporal
term to the fingerprint so two queries asked in the same period are slightly
closer, per TCM's drifting context vector (Howard & Kahana, 2002); optionally
support explicit temporal filtering at query time. *Where:* `vault/chunker.py`,
`stores/vector.py`, `stores/activations.py`. *Effort:* M. *Licence:* none. This
converts a semantic store with an episodic interface into something that can
actually answer "what was I thinking about last spring."

### Tier 3 — Worth knowing about, not worth building yet

**IV.11 — Conformal prediction over observer claims.** The letter is instructed
to express uncertainty in prose ("take it as a hunch"). Conformal prediction
(Angelopoulos & Bates, arXiv 2107.07511, 2021) gives distribution-free coverage
guarantees — you could, in principle, calibrate a threshold such that structural
claims surfaced to the user are right 90% of the time. *Blocked on:* having
labelled outcomes, which means asking the user whether a letter's claims landed.
Cheap to start collecting; expensive to act on.

**IV.12 — Discrete curvature for bridge detection.** `nx.bridges` finds edges
whose removal disconnects the graph — a binary, brittle criterion. Ollivier-Ricci
curvature gives a continuous measure of how "bridge-like" an edge is: negatively
curved edges lie between communities, positively curved ones sit inside them (Ni,
Lin, Luo & Gao, *Scientific Reports* 9:9984, 2019). A better bridging-notes
report, and a plausible second opinion on community structure. `GraphRicciCurvature`
on PyPI implements it — **licence unverified, check before adopting.**

**IV.13 — A REM analogue: generative recombination.** Consolidation currently
extracts regularities from what happened. It does not recombine distant material
to *propose* an association that has not occurred. The biological warrant is
strong — REM sleep preferentially benefits remote-associate performance — and the
computational version is straightforward: sample pairs of notes that are
semantically near but graph-far, ask the batch model whether a real connection
exists, and write high-confidence proposals into the letter as questions rather
than assertions. This is the natural next thing your dream phase could do, and
it is deliberately last on this list because it is the item most likely to
manufacture plausible falsehoods. It needs IV.11 or an equivalent calibration
story first.

**IV.14 — Two-timescale consolidation.** Full CLS (McClelland et al., 1995) has
two stores, not one: fast episodic and slow semantic. Your daemon has the fast
store and a report. A slow store would be *distilled notes* — summaries written
during consolidation that become first-class retrievable objects, so retrieval
can hit a consolidated abstraction instead of re-deriving it from episodes every
time. RAPTOR (Sarthi et al., ICLR 2024) is the RAG-side version of this idea
(recursive clustering-and-summarization into a retrievable tree). *Blocked on:*
this is architecturally large and interacts with write-back discipline, since
those summaries land in the user's vault.

---

## Part V — Bibliography

**Verification status.** ✓ = authors, venue, and year confirmed against a
primary or bibliographic source while writing this document. ○ = cited from
established knowledge, not independently re-verified in this session; treat the
claim as sound and the exact page numbers as worth checking before you quote
them anywhere formal.

### Memory and cognition

- ✓ Anderson, J. R., & Schooler, L. J. (1991). Reflections of the environment in memory. *Psychological Science*, 2, 396–408.
- ✓ Bjork, R. A., & Bjork, E. L. (1992). A new theory of disuse and an old theory of stimulus fluctuation. In *From Learning Processes to Cognitive Processes*.
- ✓ Howard, M. W., & Kahana, M. J. (2002). A distributed representation of temporal context. *Journal of Mathematical Psychology*, 46, 269–299.
- ✓ Raaijmakers, J. G. W., & Shiffrin, R. M. (1981). Search of associative memory. *Psychological Review*, 88(2), 93–134.
- ○ Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373.
- ○ Tulving, E. (1972). Episodic and semantic memory. In *Organization of Memory*.
- ○ Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407–428.
- ○ Collins, A. M., & Quillian, M. R. (1969). Retrieval time from semantic memory. *Journal of Verbal Learning and Verbal Behavior*, 8(2), 240–247.
- ○ Polyn, S. M., Norman, K. A., & Kahana, M. J. (2009). A context maintenance and retrieval model of organizational processes in free recall. *Psychological Review*, 116(1), 129–156.
- ○ McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. *Psychological Review*, 102(3), 419–457.
- ○ Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*, 265(5172), 676–679.
- ○ Diekelmann, S., & Born, J. (2010). The memory function of sleep. *Nature Reviews Neuroscience*, 11(2), 114–126.
- ○ Tononi, G., & Cirelli, C. (2003/2014). Synaptic homeostasis hypothesis. *Brain Research Bulletin* 62(2); *Neuron* 81(1).
- ○ Nader, K., Schafe, G. E., & LeDoux, J. E. (2000). Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval. *Nature*, 406(6797), 722–726.
- ○ Bartlett, F. C. (1932). *Remembering: A Study in Experimental and Social Psychology.*
- ○ Hebb, D. O. (1949). *The Organization of Behavior.*
- ○ Ebbinghaus, H. (1885). *Über das Gedächtnis.*
- ○ Cahill, L., & McGaugh, J. L. (1998). Mechanisms of emotional arousal and lasting declarative memory. *Trends in Neurosciences*, 21(7), 294–299.

### Clinical / assistive

- ✓ Berry, E., Kapur, N., Williams, L., Hodges, S., Watson, P., Smyth, G., Srinivasan, J., Smith, R., Wilson, B., & Wood, K. (2007). The use of a wearable camera, SenseCam, as a pictorial diary to improve autobiographical memory in a patient with limbic encephalitis. *Neuropsychological Rehabilitation*, 17(4–5), 582–601.
- ○ Follow-up fMRI study indexed in PubMed (PMID 19286742) as "The neural basis of effective memory therapy in a patient with limbic encephalitis" (2009).

### Information retrieval and ranking

- ✓ Cormack, G. V., Clarke, C. L. A., & Buettcher, S. (2009). Reciprocal rank fusion outperforms Condorcet and individual rank learning methods. *SIGIR '09*, 758–759.
- ✓ Carbonell, J., & Goldstein, J. (1998). The use of MMR, diversity-based reranking for reordering documents and producing summaries. *SIGIR '98*, 335–336.
- ✓ Joachims, T., Swaminathan, A., & Schnabel, T. (2017). Unbiased learning-to-rank with biased feedback. *WSDM '17*, 781–789.
- ✓ Radlinski, F., Kurup, M., & Joachims, T. (2008). How does clickthrough data reflect retrieval quality? *CIKM '08*, 43–52.
- ○ Joachims, T. (2002). Optimizing search engines using clickthrough data. *KDD '02*.
- ○ Spärck Jones, K. (1972). A statistical interpretation of term specificity and its application in retrieval. *Journal of Documentation*, 28(1), 11–21.
- ○ Robertson, S., & Zaragoza, H. (2009). The probabilistic relevance framework: BM25 and beyond. *FnTIR*, 3(4).
- ○ Formal, T., Piwowarski, B., & Clinchant, S. (2021). SPLADE: Sparse lexical and expansion model for first stage ranking. *SIGIR '21*.
- ○ Khattab, O., & Zaharia, M. (2020). ColBERT: Efficient and effective passage search via contextualized late interaction over BERT. *SIGIR '20*.
- ○ Xiao, S., et al. (2023). C-Pack: Packaged resources to advance general Chinese embedding. (Source of the BGE model family used here.)

### Graphs, clustering, and algorithms

- ✓ Traag, V. A., Waltman, L., & van Eck, N. J. (2019). From Louvain to Leiden: guaranteeing well-connected communities. *Scientific Reports*, 9, 5233.
- ✓ Campello, R. J. G. B., Moulavi, D., & Sander, J. (2013). Density-based clustering based on hierarchical density estimates. *PAKDD 2013*, LNCS 7819, 160–172.
- ✓ Chakrabarti, D., Kumar, R., & Tomkins, A. (2006). Evolutionary clustering. *KDD '06*, 554–560.
- ✓ Tong, H., Faloutsos, C., & Pan, J.-Y. (2006). Fast random walk with restart and its applications. *ICDM '06*.
- ✓ Ni, C.-C., Lin, Y.-Y., Luo, F., & Gao, J. (2019). Community detection on networks with Ricci flow. *Scientific Reports*, 9, 9984.
- ○ Blondel, V. D., Guillaume, J.-L., Lambiotte, R., & Lefebvre, E. (2008). Fast unfolding of communities in large networks. *JSTAT*, P10008.
- ○ Freeman, L. C. (1977). A set of measures of centrality based on betweenness. *Sociometry*, 40(1), 35–41.
- ○ Brandes, U. (2001). A faster algorithm for betweenness centrality. *Journal of Mathematical Sociology*, 25(2), 163–177.
- ○ Kuhn, H. W. (1955). The Hungarian method for the assignment problem. *Naval Research Logistics Quarterly*, 2(1–2), 83–97.

### Machine learning and LLM systems

- ✓ Gutiérrez, B. J., et al. (2024). HippoRAG: Neurobiologically inspired long-term memory for large language models. *NeurIPS 2024*.
- ✓ Edge, D., Trinh, H., Cheng, N., Bradley, J., Chao, A., Mody, A., Truitt, S., & Larson, J. (2024). From local to global: A Graph RAG approach to query-focused summarization. Microsoft Research / arXiv 2404.16130.
- ✓ Park, J. S., O'Brien, J., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. (2023). Generative agents: Interactive simulacra of human behavior. *UIST '23*, 1–22.
- ✓ Settles, B., & Meeder, B. (2016). A trainable spaced repetition model for language learning. *ACL 2016*, 1848–1858.
- ✓ Angelopoulos, A. N., & Bates, S. (2021). A gentle introduction to conformal prediction and distribution-free uncertainty quantification. arXiv 2107.07511.
- ○ Sarthi, P., et al. (2024). RAPTOR: Recursive abstractive processing for tree-organized retrieval. *ICLR 2024*.
- ○ Mnih, V., et al. (2015). Human-level control through deep reinforcement learning. *Nature*, 518(7540), 529–533.
- ○ Kirkpatrick, J., et al. (2017). Overcoming catastrophic forgetting in neural networks. *PNAS*, 114(13), 3521–3526.
- ○ Itti, L., & Baldi, P. (2009). Bayesian surprise attracts human attention. *Vision Research*, 49(10), 1295–1306.
- ○ FSRS / Free Spaced Repetition Scheduler (open-spaced-repetition project, 2022–). DSR model; `py-fsrs` reference implementation is **MIT** (verified).

---

## Appendix — Parameter provenance

Which constants in the system have a principled basis, and which are judgement
calls waiting for data. Worth keeping current: it is the difference between a
tuned system and one that merely has numbers in it.

| Constant | Value | Where | Basis |
|---|---|---|---|
| `distance_decay` | 0.5 | `config.py:142` | Judgement. Spreading-activation attenuation is real; this specific rate is not derived. |
| `expansion_depth` | 2 | `config.py:141` | Defensible. Spreading activation is empirically shallow; 2 hops is a reasonable operationalization. |
| `DEFAULT_ALPHA` | 0.5 | `weights.py:25` | Judgement. |
| `DEFAULT_HOP_DECAY` | 0.7 | `weights.py:26` | Judgement, but the *shape* is right — eligibility traces do decay geometrically. |
| `DEFAULT_CEILING` | 5.0 | `weights.py:27` | Principled in kind (synaptic saturation is real), arbitrary in magnitude. |
| `DEFAULT_HALF_LIFE_DAYS` | 30 | `weights.py:28` | Judgement, and the weakest of the set — Anderson & Schooler's point is that the rate is item-specific. See IV.5. |
| `STRENGTH_BY_SOURCE` | 1.5/1.0/0.6/0.5/0.3 | `activations.py:40` | Judgement. Ordering is clearly right; magnitudes are unvalidated. |
| `1/log₂(rank+1)` | — | `activations.py:116` | **Principled.** nDCG discount; approximates the serial-position advantage. |
| IDF form | `log(1 + N/(1+df))` | `activations.py:284` | **Principled.** Smoothed Robertson IDF. |
| `max_df_ratio` | 0.25 | `config.py:257` | Judgement, with a good rationale in the docstring. |
| `_MIN_CORPUS_FOR_DF_PRUNING` | 50 | `activations.py:72` | **Principled reasoning**, arbitrary threshold — and the reasoning (df ratios are meaningless on a tiny corpus) is correct and well documented. |
| `min_cluster_size` | 3 | `config.py:297` | Minimum defensible value; HDBSCAN cannot do less. |
| `DEFAULT_MATCH_THRESHOLD` | 0.60 | `themes.py:29` | **Now measurable** — `daemon themes tune` (§IV.15) replays your ledger and reports churn per threshold. Still 0.60 until real history says otherwise. |
| `retrieval.interleave` | true | `config.py` | **Principled.** Interleaved evaluation is the published answer to position-biased implicit feedback (§IV.7). |
| Draft ratio | 1:1 | `interleave.py` | Judgement, but the *fair* default. Deliberately not tuned from its own win rate — see §III.2. |
| `DEFAULT_MIN_MEMBER_SIMILARITY` | 0.10 | `themes.py:37` | Judgement, with an excellent rationale (leave-one-out, against HDBSCAN small-corpus degeneracy). |
| `DEFAULT_WARM_THRESHOLD` | 1.5 | `structural.py:53` | **Derived** — sits meaningfully between the 1.0 baseline and the 5.0 ceiling. |
| `betweenness_sample_k` | 200 | `config.py:324` | Standard approximation practice. |
| Louvain connectivity repair | on | `structural.py` | **Principled.** Removes the disconnected-community case Traag et al. (2019) measured. |
| Theme assignment | Hungarian | `themes.py` | **Principled.** Exact maximum-weight bipartite matching (Kuhn, 1955), replacing greedy. |
| `churn` alarm | ~0.5 | `agents.py:370` | Judgement, and honest about being one. |
| `max_tokens` / `overlap` | 512 / 50 | `config.py:72-73` | Conventional RAG defaults. |

The pattern worth noticing: everything derived from information retrieval (rank
discount, IDF, warm threshold, and now the draft) has a principled basis, and
everything describing *memory dynamics* (decay rate, reinforcement magnitude,
source strengths) is judgement. That is exactly the boundary where the field has
literature this system does not yet use — which is what the remaining Part IV
items are for. The two biggest are §IV.5 (learned per-edge forgetting rates, to
replace the one global half-life) and §IV.9 (a salience layer, to replace
`STRENGTH_BY_SOURCE`'s five hand-set constants with something that knows what
mattered to the user rather than which code path found it).
