# Episodic time-binding (§IV.10) — design

- **Started:** 2026-07-29 · **Completed:** 2026-07-30
- **Status:** **APPROVED.** Scope, four locked decisions, three resolved open
  questions, three approved amendments, and all three design sections drafted.
  Ready for `writing-plans`.
- **Author:** Evan Gress, with Claude
- **Tracks:** §IV.10 in [MY-DAEMON-RESEARCH-APPLIED.md](../../../MY-DAEMON-RESEARCH-APPLIED.md)

## Problem

`Chunk` (`models.py:98`) carries `note_uuid`, `note_path`, `heading_path`,
`text`, `chunk_index`, `tags`, `wikilinks` — and no time. The ledger timestamps
*queries*; nothing timestamps the *content*, and the retrieval path never sees a
date. "What was I working on last spring?" is not answerable by retrieval, only
by luck if a note happens to say so in prose.

Tulving's (1972) semantic/episodic distinction turns on exactly this property:
episodic memory is memory for events located in subjective time. Without a
temporal index this is a semantic memory wearing an episodic interface — Part
III.3 of the applied-research document.

## Findings from the survey (the expensive part to re-derive)

These were established by reading the code and measuring the actual vault. They
are the reason the decisions below came out the way they did.

**1. mtime is not episodic time on this vault — it is an import artifact.**
Sampling notes that carry a frontmatter date:

```
front=2024-09-02  mtime=2026-07-01  2024-09-02.md
front=2026-03-31  mtime=2026-07-01  2026-03-31 Journal Entry.md
front=2026-04-26  mtime=2026-07-01  2026-04-26.md
front=2026-05-25  mtime=2026-07-01  Memorial Day Weekend.md
```

Every mtime in that sample is `2026-07-01` (a bulk copy) while the frontmatter
dates span 2024-09 to 2026-07. An mtime-based temporal axis would date a 2024
journal entry to 2026. Compounding it: `vault/writer.py` mutates notes for tag
write-back, so the daemon bumps mtime on notes it touches — the axis would drift
as a side effect of the daemon's own activity.

*Refined 2026-07-30 by the full-vault measurement in finding 9. The sample above
is real but not representative: across the whole vault mtime takes 8+ distinct
values, and a narrow slice of it is trustworthy. See finding 9.*

**2. The vault's real temporal signal.** 134 ingestable notes (excluding
`exclude_dirs`); 61 named `YYYY-MM-DD…`; 63 carrying a frontmatter date key.
Frontmatter keys observed: `date:`, `created_at:`, `Date:`, `created:`. Mixed
case, ISO values, usually quoted. Only 2 of the 61 date-named files are
daemon-generated (`link-suggestions-*`). **67 of 134 notes (50%) carry a date
under the parsing rule below; 67 do not.**

**3. PyYAML returns three different types** for the same-looking frontmatter,
depending on quoting — the coercion must handle all three:

```
date: "2024-09-02"                → str
date: 2024-09-02                  → datetime.date
created_at: 2024-09-02T10:30:00Z  → datetime.datetime (tz-aware)
```

**4. This is a payload change, not a re-embed.** `_stable_chunk_id`
(`chunker.py:20`) hashes `note_uuid :: heading_path :: index :: text`. No
timestamp in the hash, so chunk ids are unchanged and vectors need not be
recomputed.

**5. But a backfill is required, and it is real work.** `ingest.py:169` skips
any note whose `body_sha256` is unchanged, so re-running `daemon ingest` will
**not** populate a new payload field on existing points. The pattern to copy is
`set_note_path` (`vector.py:317`), which does a `set_payload` filtered by
`note_uuid` without touching vectors. *The ingest manifest assumes chunk payload
is a pure function of chunk content; a payload field derived from frontmatter or
filename breaks that assumption quietly. Worth a comment at the skip site — the
next payload field will hit this too.*

**5b. Ongoing maintenance is nearly free (found 2026-07-30).** The skip site
already branches on `renamed` and `fm_changed` (the M-mem-era metadata-refresh
path). A filename-derived date is therefore refreshed by the `renamed` branch and
a frontmatter-derived date by the `fm_changed` branch, both without embedding.
Only the *initial* backfill is real work: existing manifest entries have both an
unchanged body and unchanged frontmatter, so neither branch fires for them.
**The metadata-refresh path must be extended to write `occurred_at`** or these
two branches will silently go stale.

**6. Embedded Qdrant supports the filter we need.** `_ensure_payload_indexes`
(`vector.py:197`) returns early in embedded mode, which reads as "no filtering"
but is not — embedded mode lacks payload *indexes*, not filters. Verified
against the installed client: `qdrant_client.local.payload_filters` handles both
`Range` and `DatetimeRange`. So a true **pre-filter** (applied before top-k, not
after) is available in the default deployment. At ~134 notes the missing index
costs nothing.

**7. One seam covers every surface.** `RetrievalOrchestrator.retrieve()`
(`orchestrator.py:53`) is the only point all surfaces pass through — documented
as such in `retrieval/trace.py`. Two call sites: `integration/core.py:224`
(Hermes/MCP-facing `DaemonCore`) and `pipeline/query.py:125` (ask/chat). Both
retrieval routes rebuild `Chunk` from the Qdrant payload — `seed.py:32` and
`expand.py:57` — so one payload field propagates to both for free.

**8. Time will be a property of retrieved content, not of the association
structure.** Expansion scrolls Qdrant by `note_uuid` rather than reading graph
nodes, so the graph store never learns the timestamp. That is the right boundary
for §IV.10, but it is a deliberate choice, not an oversight.

**9. Filesystem timestamps measured against ground truth (2026-07-30).** Run
over all 134 notes, scoring each candidate axis against the 67 notes where a
parsed date supplies ground truth:

| Candidate axis | Median error | Within 1 day | Within 7 days |
|---|---|---|---|
| Birth time (`stat -c %w`) | 28 days | 14/67 | 26/67 |
| Raw mtime | 35 days | 14/67 | 23/67 |
| First `YYYY-MM-DD` in note body | 0 days | 7/7 (n=7) | 7/7 |
| **mtime, restricted to mtime < birth time** | **0 days** | **6/6** | **6/6** |
| mtime, where mtime >= birth time | 38 days | 8/61 | — |

Three conclusions, each load-bearing below:

- **Birth time is structurally the worst candidate**, which is counterintuitive
  enough to record: *copying a file resets birth time but preserves mtime.* The
  birth-time distribution is three bulk-copy events — `2026-06-21` ×49,
  `2026-07-01` ×37, `2026-06-23` ×19 — so it dates the vault's arrival on this
  machine, never any authorship. It is also not portably reachable: Python
  exposes no `st_birthtime` on Linux (verified on the dev box), and Windows and
  macOS each differ. Rejected outright.
- **mtime predating birth time is trustworthy** — it survived a copy, so it
  records real authoring activity. The split against ground truth is clean:
  0-day median where mtime < birth, 38-day median where it is not.
- **First-date-in-body is accurate but unsafe.** 7/7 on a sample of 7, and it
  would rescue only 5 undated notes. Its failure mode is confidently dating a
  reference note to a date discussed *inside* it — a note about the Apollo
  program becoming a 1969 memory. That is the manufactured-autobiographical-
  memory failure decisions 2 and 3 exist to prevent. Hindsight ships this
  heuristic (`_infer_temporal_date`); this project will not.

## Decisions locked

| # | Decision | Chosen |
|---|---|---|
| 1 | Scope | **Foundation + query-time filtering.** The TCM fingerprint temporal term (two queries asked in the same period scoring closer) is explicitly deferred to a follow-up — it changes recall and theme behaviour and deserves its own measurement pass. |
| 2 | Time source | **Parsed only; mtime kept but separate.** `occurred_at` = frontmatter date → filename date → *(amendment 1: guarded mtime)* → `None`. `modified_at` = mtime, stored but never the episodic axis. Undated notes are honestly undated. |
| 3 | Filter rule | **Exclude undated, and report the count.** Hard pre-filter; the result reports "N undated chunks not considered." |
| 4 | Range source | **Explicit structured parameter; the LLM converts.** `--since`/`--until` (ISO) on the CLI, optional args on the Hermes/MCP tool schema. **No natural-language date parser ships in Python.** |

**Rationale worth preserving.** Decisions 2 and 3 are the same argument in two
places. A memory prosthetic that asserts a confidently wrong date has
manufactured an autobiographical memory — the failure the `INTENTIONAL_SURFACES`
filter exists to prevent in §II.6, and the concern Part II.11 raises about the
daemon's edits becoming indistinguishable from the user's own. "Undated" is a
safe answer; "2026-07-01" for a 2024 journal entry is not. Reporting the
excluded count is the same metacognitive move as §II.8's churn reporting and
`theme_tuning.recommend()` returning `None` on thin history: the system says
what it does not know.

Decision 4 avoids shipping a date-phrase parser whose every entry is a judgement
about the user's calendar intent — "spring" is hemisphere-dependent, "last year"
is ambiguous in January, and the phrase set is never finished. Models are good
at this conversion; the tool-call surface already exists.

## Open questions — resolved 2026-07-30

**Q1 — filename prefix only, or a date anywhere in the filename?**
**Resolved: prefix only.** A `YYYY-MM-DD` anchored at the start covers all 61
dated notes including `2026-03-31 Journal Entry.md`. A date buried mid-name is
much less likely to mean "when this happened" (`Notes from 2024-09-02
meeting.md` is *about* that date, not necessarily written then). The refusal gets
an explicit test, so mid-name dates are undated **on purpose** rather than by
accident.

**Q2 — UTC midnight or local midnight for a bare date?**
**Resolved: UTC**, and the competitor review strengthened the reasoning
considerably. Hindsight's entire timezone bug class lives at exactly this seam:
`dateparser` returns naive local time, then `start_date.replace(tzinfo=UTC)`
reinterprets server-local wall clock as UTC, so "yesterday" shifts by the host
offset — and their internal reflect path hits it on every call, because it never
passes a reference date. The safe property is not *which* midnight but **the
same normalisation on both sides of the comparison**. Store UTC; normalise query
bounds to UTC in the same module; the interval is then self-consistent on any
machine. A `vault.timezone` setting for *display* is deliberately out of scope.

**Q3 (new) — what happens to a partial date such as `date: 2024-09`?**
**Resolved: it yields `None`.** This is the decision that removes any need for a
granularity or precision enum: every stored `occurred_at` is day-precision **by
construction**, so there is no vague value for a scorer to mishandle. The
counter-example is Hindsight, which accepts vague dates and pays for it — "some
time in 2024" is stored as `2024-01-01 → 2024-12-31`, indistinguishable from a
genuine year-long event, and its `temporal_proximity` then scores it at the
range *midpoint*, so a year-granularity note behaves as though it happened on
about 2 July. Storing a fabricated point is worse than storing nothing.
Recorded here as *considered and rejected with a reason* so it is not re-opened.

## Approach chosen: A

Parse in `parse_note()`, flow through `Note` → `Chunk` → payload + registry.

Rejected alternatives, with the reason:

- **B — parse in `chunk_note()`, payload only.** Smaller diff, no migration, but
  strands the derivation inside chunking so nothing else can use it, and gives a
  clean content-derived unit a metadata-parsing job.
- **C — registry-only, resolve the range to a `note_uuid` `MatchAny` filter at
  query time.** Cheapest path to the user-visible feature: no payload work, no
  backfill, works on the existing collection immediately. Rejected because the
  chunk never learns its own date, so citations, the GUI source list, and the
  observer letter cannot display or reason about *when* material is from — which
  is the thing §IV.10 exists to enable. Also splits the source of truth across
  two stores, and the `MatchAny` grows with the vault.

## Section 1 — derivation and data model

**New unit: `vault/dates.py`.** One public function,
`derive_occurred_at(frontmatter, relative_path, mtime, trusted_before) -> tuple[datetime | None, str | None]`
returning the timestamp and its **source**. Pure, no filesystem access —
`parse_note` passes what it already has. Isolated because it has fiddly edge
cases and is the piece most worth testing exhaustively.

Precedence, first match wins:

1. **Frontmatter**, keys tried in configured order, matched case-insensitively:
   `occurred_at`, `date`, `created`, `created_at`. Case-insensitive because the
   vault has both `date:` and `Date:`. The key list lives in config as
   `vault.date_frontmatter_keys` so it is tunable per vault.
   → source `"frontmatter"`.
2. **Filename**, a `YYYY-MM-DD` prefix only, anchored at the start.
   → source `"filename"`.
3. **Guarded mtime** (amendment 1). Only when `vault.mtime_trusted_before` is
   set *and* the note's mtime is strictly earlier than it.
   → source `"mtime"`.
4. **Otherwise `(None, None)`.**

Normalisation: everything becomes a timezone-aware UTC `datetime`. A bare date
becomes midnight **UTC** (Q2). A garbage value (`date: soon`) or a partial date
(`date: 2024-09`) yields `None` rather than raising: a malformed or imprecise
date is an undated note, not a failed ingest (Q3).

Model changes:

- `Note` gains `occurred_at: datetime | None` and `occurred_at_source: str | None`.
  `mtime` unchanged.
- `Chunk` gains `occurred_at`, `occurred_at_source`, and `modified_at`, all
  optional and defaulted so no existing construction site breaks.
- `NoteRecord` gains `occurred_at` and `occurred_at_source`, persisted by
  **migration 7** (`ALTER TABLE notes ADD COLUMN …`; verified — the table is
  `notes` and the current `user_version` is 6). ISO-stored, like `mtime`.
- Qdrant payload gains `occurred_at`, `occurred_at_source`, and `modified_at` as
  ISO strings / plain strings, **omitted when `None`** — range semantics are
  cleaner with an absent key than a null one, and the payload stays smaller.

`modified_at` rides along despite nothing filtering on it yet because the
payload write is happening anyway, and it answers "what changed recently?" — a
real question `occurred_at` deliberately cannot. Storing it now avoids a second
backfill later.

### Amendment 1 — the guarded-mtime fallback

**`vault.mtime_trusted_before: <ISO date> | None`, default `None` (off).**

When set, an otherwise-undated note whose mtime is strictly earlier than this
date takes `occurred_at` from mtime, with `occurred_at_source = "mtime"`.

The guard expresses one idea: *mtime is trustworthy exactly when it predates the
import.* Finding 9 validates it at 0-day median error and 6/6 within one day on
ground truth, versus 38-day median for mtime at-or-after the import.

It is phrased as a **configured cutoff rather than a birth-time comparison** on
purpose, even though birth time is what revealed the effect:

- Portable. No `st_birthtime` (absent on Linux), no shelling out to `stat -c %w`,
  no per-platform branch.
- Inspectable. The assumption sits in `config.yaml` where it can be challenged,
  rather than inside a heuristic.
- Tunable per vault, which matters because the correct value is a fact about the
  user's migration history, not about this codebase.

`daemon migrate backfill-dates` reports the **detected import clusters** (the
birth-time histogram, best-effort and skipped where unavailable) so the value is
chosen from evidence rather than memory.

Honest sizing, to be repeated in the docs: on the current vault this rescues
**9 of 67 undated notes** — coverage moves 50% → 57%, not to 100% — and the
validation sample is 6 notes. `occurred_at_source` exists so that if the
fallback proves unreliable it can be filtered out downstream **without a second
backfill**.

## Section 2 — the filter path and surfaces

**`DateRange` in `models.py`.** Frozen, `since: datetime | None`,
`until: datetime | None`, with a validator rejecting `since > until`. Hindsight
has no such validation (its gap 11) and parses the two ends independently, so a
hallucinated range fails silently there.

Semantics: **`since` inclusive, `until` exclusive.** The CLI converts a bare
`--until 2026-05-31` into `2026-06-01T00:00:00Z`, so the user's obvious intent —
include that day — is honoured **at the edge** while the core keeps a clean
half-open interval. Putting the human-intent fudge anywhere but the boundary is
how off-by-one-day bugs become unlocatable.

**Both retrieval arms are filtered.** This is the most consequential borrowing
from the competitor review. In Hindsight only **one of four** arms receives the
date bounds, so a temporally-scoped question does not actually exclude
out-of-window material — the filter degrades into a soft ranking nudge while
*looking* like a filter (their findings 1 and 5). Therefore:

- `seed_search(..., date_range=)` → `vector_store.search(...)` /
  `hybrid_search(...)` build a Qdrant `Filter` carrying
  `DatetimeRange(gte=since, lt=until)` on `occurred_at`. A true **pre-filter**,
  applied before top-k (finding 6).
- `expand_from_seeds(..., date_range=)` applies the same filter to its
  `note_uuid` scroll.

The cost is real and stated rather than hidden: while a temporal filter is
active, a highly relevant note linked from an in-range note is dropped if it is
out of range. For "what was I working on last spring?" that is the correct
answer rather than a regression — but the graph's reach is deliberately narrowed
whenever a range is supplied.

**The filter reads `occurred_at` only.** It must never fall back to
`modified_at`, and must never `OR` the two. Hindsight's temporal `WHERE` clause
unions event time with assertion time (their finding 4), so a query for
"March 2024" matches things *said* then as well as things that *happened* then —
which silently undoes the benefit of storing both. A test pins this.

**Reporting.** `RetrievalResult` gains `date_range: DateRange | None` and
`undated_excluded: int`. The count is **vault-wide**, obtained by an `IsEmpty`
count on `occurred_at`, and computed only when a range is active, so an
unfiltered query pays nothing. It is rendered as coverage rather than as loss:

```
Temporal filter: 2026-03-01 → 2026-06-01 (UTC)
Coverage: 806 of 1,590 chunks carry a date — 784 undated chunks not considered.
```

(Illustrative, and deliberately consistent with finding 2: about half the notes
carry a date, so about half the chunks should. If the shipped output shows a
markedly higher dated fraction than the note-level 50%, that is a signal the
derivation is firing somewhere it should not — worth checking rather than
celebrating.)

Per-query precision was considered and rejected: knowing which undated chunks
*would* have matched requires also running the unfiltered query, doubling cost
to produce a number that says substantially the same thing.

**Surfaces.** `--since` / `--until` on `daemon query` and `daemon ask`; optional
`since` / `until` on `DaemonCore.recall`, which carries them into the Hermes tool
schema. No natural-language parsing anywhere in Python (decision 4).

### Amendment 2 — the date must reach the model

Currently `build_context_block` (`llm/prompts.py`) emits **retrieval machinery
scores** and **no date**:

```
[1] Journal.md › March  (score=0.421, vector=0.832)
```

Both competitors do the opposite, independently: Honcho prefixes every injected
observation with `[YYYY-MM-DD HH:MM:SS]` and forces absolute dates into
conclusion text specifically so time survives embedding; Hindsight uses
`[Date: June 05, 2022 (2022-06-05)]`, human-readable and ISO together.

Without this the filter can work perfectly and the model still cannot say *when*
anything is from — and item §IV.17's conditional reconciliation would have
nothing to reconcile on. Two changes, both in `build_context_block`:

- **Add the date** when `occurred_at` is present: `[2026-03-31]`, ISO, with
  `(undated)` when absent so the model can distinguish "no date" from "I wasn't
  told." When `occurred_at_source == "mtime"` the marker is `[2026-03-31~]`, the
  tilde meaning inferred — the model is told what it means, and told not to
  present an inferred date as certain.
- **Remove `score=` and `vector=`.** They are facts about the machinery, they
  invite the model to trust the rank order, and no prompt instruction consumes
  them.

### Amendment 3 — recorded requirement for the deferred NL step

Locked decision 1 defers natural-language → `DateRange` conversion, and locked
decision 4 assigns it to the model. That follow-up **must** carry Hindsight's
best temporal decision, recorded here so it is not lost:

> An open-ended expression ("from March onward", "since I started the new job")
> or a recurring one ("every Wednesday") must resolve to a **half-open or absent
> bound**, never to an invented one. Hindsight implements this as an explicit
> `NO_TEMPORAL_CONSTRAINT` sentinel with the stated reasoning that "the API model
> only represents closed ranges, and inventing an end date would be misleading."

`DateRange` already supports this: either bound may be `None`. The requirement is
that the conversion step *use* that rather than fabricating a bound, and that its
prompt say so explicitly.

## Section 3 — backfill, migration, and testing

**Migration 7.** `ALTER TABLE notes ADD COLUMN occurred_at TEXT` and
`ADD COLUMN occurred_at_source TEXT`, following the additive pattern already used
for the `feedback` and `queries` tables. ISO-stored, matching `mtime`.

**`daemon migrate backfill-dates`.** Walks the vault, derives
`(occurred_at, occurred_at_source)` per note, and writes via `set_payload`
filtered on `note_uuid` — copying `set_note_path` (`vector.py:317`) so vectors
are never touched (finding 4). Also updates the `notes` registry rows. Properties:

- **Idempotent.** Re-running changes nothing and reports the same counts.
- **Reports the derivation breakdown**: `frontmatter / filename / mtime / undated`,
  so the result can be sanity-checked against a vault the user knows.
- **Reports detected import clusters** (birth-time histogram, best-effort;
  skipped silently where birth time is unavailable) to inform
  `vault.mtime_trusted_before`.
- `--dry-run` prints the breakdown and writes nothing, consistent with
  `daemon consolidate --dry-run`.

**Comment at the ingest skip site** (`ingest.py:169`), per finding 5: the
manifest's assumption that chunk payload is a pure function of chunk *content* is
now formally broken, and the next payload field derived from metadata will hit
the same wall.

**Extend the metadata-refresh path** to write `occurred_at` /
`occurred_at_source`, per finding 5b — otherwise the `renamed` and `fm_changed`
branches go stale and only a full rebuild would correct them.

### Test plan

Weighted toward derivation, which is where the edge cases live.

| Unit | What gets pinned |
|---|---|
| `derive_occurred_at` | all 3 PyYAML types (`str`, `date`, tz-aware `datetime`); `date: soon` → `None`; **`date: 2024-09` → `None`** (Q3); precedence order across all four keys; case-insensitive `Date:` vs `date:`; filename prefix fires; **mid-name date refuses** (Q1); UTC-midnight normalisation (Q2); returned `source` is correct for each branch |
| guarded mtime | off by default; fires only when mtime < `mtime_trusted_before`; does **not** override frontmatter or filename; source is `"mtime"` |
| `DateRange` | `since > until` rejected; open-ended on either side accepted (amendment 3) |
| Filter path | pre-filter reaches **both** arms; an out-of-range note is absent from expansion too; filter reads `occurred_at` and **never** `modified_at`; `undated_excluded` reported; no filter constructed when `date_range is None` |
| Backfill | idempotent; vectors unchanged (compare a point's vector before/after); registry and payload agree; `--dry-run` writes nothing |
| Ingest paths | `fm_changed` refreshes `occurred_at`; `renamed` refreshes a filename-derived date |
| Context block | date rendered as `[ISO]`; `(undated)` when absent; `~` suffix when source is mtime; `score=`/`vector=` absent |
| CLI | bare `--until 2026-05-31` becomes exclusive `2026-06-01T00:00:00Z` |

### Out of scope, deliberately

- **The TCM fingerprint temporal term** (locked decision 1).
- **Natural-language date parsing in Python** (locked decision 4; amendment 3
  records the requirement for whoever builds the model-side conversion).
- **Timestamps on graph edges / a temporal link type** (finding 8). Hindsight
  has these and they are its buggiest area — undirected proximity edges whose
  before/after split is computed in SQL and then discarded, with no window
  predicate, so a "temporal" edge can span years.
- **Coverage-spread bucketing of the temporal arm.** Hindsight buckets its
  in-window pool into 8 time buckets and drafts round-robin, because a
  near-uniformly-dated corpus otherwise degrades to a random sample plus a
  disk-spilling sort at ~660k rows. Irrelevant at 134 notes; revisit on a bulk
  import of years of journals.
- **`valid_to` / supersession / as-of queries.** Tracked separately as §IV.19.
- **`vault.timezone` for display** (Q2).
