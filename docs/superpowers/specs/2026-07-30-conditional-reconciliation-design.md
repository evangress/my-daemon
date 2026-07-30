# Conditional reconciliation in the synthesis prompt (§IV.17) — design

- **Date:** 2026-07-30
- **Status:** **APPROVED** (voice chosen by the user from three options).
- **Author:** Evan Gress, with Claude
- **Tracks:** §IV.17 in [MY-DAEMON-RESEARCH-APPLIED.md](../../../MY-DAEMON-RESEARCH-APPLIED.md)
- **Depends on:** §IV.10 amendment 2 — the model cannot reconcile on dates it is
  never shown.

## Problem

`llm/prompts.py` already states the right *policies*. Its `SYSTEM_PROMPT` says
"do not invent details, dates, or names", "say so plainly" when the context does
not contain the answer, and "if multiple excerpts conflict, surface the conflict
instead of papering over it." The module docstring is candid about its maturity:
*"Intentionally boring for v0.1 — tune later."*

The competitor review found that both Hindsight and Honcho started from the same
policies and independently concluded that **stating a policy is not enough** —
the model has to be made to do visible work:

- **Hindsight** requires the answering model to write out, per candidate,
  `<event> (<date>) vs authoritative (<date>) → BEFORE/AFTER → KEEP/DROP` before
  answering, annotated in-repo: *"This is the single most common mistake — do not
  skip this step even if you feel confident."*
- **Honcho** mandates grep-first enumeration, a deduplication table, a
  verification pass, and a hard abstention contract: *"A confident 'I don't know'
  is ALWAYS correct."*

Neither is subtle, and both are visibly benchmark-fitted. But the underlying
finding transfers: a policy the model may satisfy silently is a policy it can
skip, and neither the user nor the developer can tell when it did.

For this project the stakes are higher than for either competitor. A general
agent memory that guesses wrong is annoying. A memory prosthetic aimed at
cognitive decline that reports a superseded decision as current has handed its
user a false autobiographical memory, in their own voice, with a citation
attached. That is the failure mode CLAUDE.md's framing makes unacceptable.

## Decision: conditional audit

Chosen by the user over "invisible discipline" and "always-visible audit".

**No ceremony on straightforward questions.** When excerpts genuinely compete, or
when the answer turns on which statement is more recent, the daemon shows a short
explicit reconciliation *before* the answer.

```
── Reconciling dated statements ──
  Denver   (2026-06-14, Moving Plans.md)  ← most recent
  Portland (2026-03-02, Journal.md)       superseded

You decided on Denver. You'd been set on Portland in March, then
changed your mind in mid-June.
  (Moving Plans.md › Decision)
  (Journal.md › March 2 — earlier view)
```

Rationale. Always-on ceremony trains the user to skip it, which destroys the
value precisely when it matters; and it makes every trivial exchange clinical,
which is the wrong register for a companion that is supposed to feel like a
daemon rather than a database. Invisible discipline improves accuracy but leaves
the user unable to check the one thing most worth checking. The conditional form
spends attention where the risk is.

## Design

All changes are in `llm/prompts.py`. No new module, no new dependency, no schema
change.

### 1. The reconciliation procedure

Added to `SYSTEM_PROMPT`, gated on a condition the model evaluates:

> **When two or more excerpts make competing claims about the same thing, or when
> the answer depends on which statement is more recent, you must reconcile them
> visibly before answering.** Emit a short `── Reconciling dated statements ──`
> block listing each competing excerpt with its date and note, marking one
> `← most recent` and the others `superseded`. Then answer. If no excerpts
> compete, do not emit the block at all.

### 2. Update versus contradiction — two policies, not one

Borrowed from Honcho, which is the only one of the three systems to separate
these, and the single best product decision found in either competitor:

> **An update resolves by recency.** If the same fact changed over time — a plan,
> a preference, a decision — the statement with the later date supersedes the
> earlier one. Say what changed and when.
>
> **A contradiction escalates to the user.** If two excerpts conflict and
> recency cannot settle it — undated, same date, or a genuine disagreement of
> fact rather than a change of mind — present **both**, say plainly that the
> notes disagree, and ask which is correct. Do not choose. Do not average.

The asymmetry is the point. For a person whose own recall is unreliable, silent
resolution is the dangerous behaviour and "your notes disagree — which is right?"
is the useful one. The user is the authority on their own vault; the daemon is
not.

This is the **prompt-level** version. A durable mechanism — a stored supersession
marker, so the conclusion is not re-derived by an LLM on every read — is tracked
separately as §IV.19. Neither competitor has the durable form either; both
re-derive current truth from raw timestamps on every single read, which is
non-cacheable and non-auditable. Doing the cheap version first is deliberate.

### 3. The anti-arithmetic rule

Lifted almost verbatim from Hindsight's consolidation prompt, which is the most
reusable artifact found in either repository:

> **Do not compute.** Never calculate, derive, or adjust a numeric value across
> excerpts. If one note says "I have 2 dogs" and another says "I have a dog named
> Rex", do **not** conclude there are 3. Report what the notes say. Arithmetic
> across separate notes invents facts that are in none of them.

This is a gap in the current prompt with a specific and plausible failure: two
individually true notes yielding a confidently stated number that appears
nowhere in the vault. Nothing in the existing wording forbids it.

### 4. Abstention hardening

The current "say so plainly" becomes an explicit contract, in Honcho's framing:

> **A confident "I don't know" is always a correct answer.** If the excerpts do
> not contain the answer, say so and stop. Do not reason toward a plausible
> answer from adjacent material. Partial beats invented: "your notes cover the
> decision but not the date" is a good answer.

### 5. Undated material must be flagged, not silently ranked

Depends on §IV.10 amendment 2, which renders `[ISO]`, `(undated)`, or `[ISO~]`
for an inferred date:

> Excerpts marked `(undated)` carry no date. Never assign them a position in a
> sequence, and never treat them as recent. An excerpt marked `~` has an
> **inferred** date — usable for ordering, but say it is approximate if the answer
> depends on it.

## Testing

Prompt behaviour cannot be pinned deterministically, and pretending otherwise
would be worse than admitting it. Three layers, in increasing cost:

**1. Structural tests (always run).** The assembled prompt contains each required
rule. Brittle against rewording by design — the point is that a future edit
cannot *delete* the reconciliation procedure, the anti-arithmetic rule, or the
abstention contract without a test going red. Asserted against named constants,
not free-floating string literals, so a deliberate rewording is a one-line change
and an accidental deletion is not.

**2. Assembled-prompt snapshot (always run).** A fixture of dated, conflicting,
and undated chunks through `build_user_message`, asserted against a stored
expected string. Catches formatting regressions in the seam between §IV.10's
context block and this prompt — which is where the two changes actually meet.

**3. Live-LLM behavioural tests (opt-in, skipped by default).** Marked
`@pytest.mark.live_llm`, requiring a real key, excluded from the default run and
from CI. Both competitors do exactly this — Honcho has `tests/live_llm/` behind a
manual gate; Hindsight runs a real-provider matrix behind a marker — because it
is the only way to check that the model actually complies. Cases:

| Fixture | Expected behaviour |
|---|---|
| Two dated excerpts, later one supersedes | Reconciliation block emitted; later date wins; both cited |
| Two undated conflicting excerpts | No recency claim; conflict surfaced; user asked which is correct |
| Single excerpt, direct answer | **No** reconciliation block — the conditional actually stays off |
| "2 dogs" + "a dog named Rex" | Does not answer 3 |
| Question with no supporting excerpt | Abstains; does not reason from adjacent material |
| Mixed dated and `(undated)` excerpts | Undated one not placed in the sequence |

The value of layer 3 is not a green tick in CI — it is that the fixtures exist and
can be re-run against a new model. Model upgrades are exactly when prompt
compliance silently changes, and this project has already been bitten by a
model-behaviour change once (`temperature` rejection on Opus 4.7).

## Out of scope

- **A structured machine-readable reconciliation signal** (emitting the audit as
  JSON for the GUI to render). YAGNI until something consumes it.
- **The durable supersession mechanism** — §IV.19.
- **Enumeration and deduplication procedures.** Honcho mandates grep-first
  enumeration and dedup tables; both are fitted to LongMemEval's question
  categories rather than to anything a vault owner asks. Not adopted.
