# Episodic time-binding (§IV.10) — design

- **Date:** 2026-07-29
- **Status:** **PARKED mid-brainstorm — not approved.** Scope and four design
  decisions are settled; design sections 2–3 are undrafted. Resume at
  "Where we stopped".
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

Every mtime is `2026-07-01` (a bulk copy) while the frontmatter dates span
2024-09 to 2026-07. An mtime-based temporal axis would date a 2024 journal
entry to 2026. Compounding it: `vault/writer.py` mutates notes for tag
write-back, so the daemon bumps mtime on notes it touches — the axis would drift
as a side effect of the daemon's own activity.

**2. The vault's real temporal signal.** 148 notes total; 61 named
`YYYY-MM-DD…`; frontmatter keys `date:` ×53, `created_at:` ×7, `Date:` ×7,
`created:` ×5. Mixed case, ISO values, usually quoted. Only 2 of the 61
date-named files are daemon-generated (`link-suggestions-*`).

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

**6. Embedded Qdrant supports the filter we need.** `_ensure_payload_indexes`
(`vector.py:197`) returns early in embedded mode, which reads as "no filtering"
but is not — embedded mode lacks payload *indexes*, not filters. Verified
against the installed client: `qdrant_client.local.payload_filters` handles both
`Range` and `DatetimeRange`. So a true **pre-filter** (applied before top-k, not
after) is available in the default deployment. At ~148 notes the missing index
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

## Decisions locked

| # | Decision | Chosen |
|---|---|---|
| 1 | Scope | **Foundation + query-time filtering.** The TCM fingerprint temporal term (two queries asked in the same period scoring closer) is explicitly deferred to a follow-up — it changes recall and theme behaviour and deserves its own measurement pass. |
| 2 | Time source | **Parsed only; mtime kept but separate.** `occurred_at` = frontmatter date → filename date → `None`. `modified_at` = mtime, stored but never the episodic axis. Undated notes are honestly undated. |
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

## Design — section 1 of 3: derivation and data model (proposed, **not approved**)

**New unit: `vault/dates.py`.** One public function,
`derive_occurred_at(frontmatter, relative_path) -> datetime | None`. Pure, no
filesystem access — `parse_note` passes what it already parsed. Isolated because
it has fiddly edge cases and is the piece most worth testing exhaustively.

Precedence, first match wins:

1. **Frontmatter**, keys tried in configured order, matched case-insensitively:
   `occurred_at`, `date`, `created`, `created_at`. Case-insensitive because the
   vault has both `date:` and `Date:`. The key list lives in config as
   `vault.date_frontmatter_keys` so it is tunable per vault.
2. **Filename**, a `YYYY-MM-DD` prefix only, anchored at the start. Matches all
   61 dated notes including `2026-03-31 Journal Entry.md`, and refuses to fire on
   a date buried mid-name, where the odds it means "when this happened" drop.
3. **Otherwise `None`.**

Normalisation: everything becomes a timezone-aware UTC `datetime`. A bare date
becomes midnight **UTC**, not local midnight — deterministic across machines;
local midnight silently shifts a note by a day for anyone east of Greenwich. A
garbage value (`date: soon`) yields `None` rather than raising: a malformed date
is an undated note, not a failed ingest.

Model changes:

- `Note` gains `occurred_at: datetime | None`. `mtime` unchanged.
- `Chunk` gains `occurred_at` and `modified_at`, both `datetime | None`, both
  defaulted so no existing construction site breaks.
- `NoteRecord` gains `occurred_at`, persisted by **migration 7**
  (`ALTER TABLE note_registry ADD COLUMN occurred_at TEXT`), ISO-stored like the
  existing `mtime` column.
- Qdrant payload gains `occurred_at` and `modified_at` as ISO strings, **omitted
  when `None`** — range semantics are cleaner with an absent key than a null
  one, and the payload stays smaller.

`modified_at` rides along despite nothing filtering on it yet because the
payload write is happening anyway, and it answers "what changed recently?" — a
real question `occurred_at` deliberately cannot. Storing it now avoids a second
backfill later.

## Where we stopped

**Two open questions on section 1**, flagged but unanswered:

1. Is the **filename-prefix-only** rule right, or should a date anywhere in the
   filename count?
2. Is **UTC-midnight** normalisation right, or should bare dates use local
   midnight?

**Undrafted:**

- **Section 2 — the filter path and surfaces.** How the range reaches
  `retrieve()`, the `DateRange` type, where the pre-filter is applied in
  `seed_search` / `hybrid_search`, how the excluded-undated count is threaded
  back into `RetrievalResult`, and what the CLI and tool schemas look like.
- **Section 3 — backfill, migration, and testing.** The `set_payload` backfill
  command, migration 7, the comment at the ingest skip site, and the test plan.

**Then:** spec self-review → user review → `writing-plans` skill →
implementation (TDD, per house style).

No code has been written. The repository is unchanged apart from this document.
