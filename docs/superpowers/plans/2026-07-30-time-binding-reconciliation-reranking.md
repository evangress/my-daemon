# Episodic time-binding, conditional reconciliation, and cross-encoder reranking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give retrieved content an honest date, make the answering model reconcile competing statements visibly, and add relevance reranking that measures its own worth.

**Architecture:** Three sequential phases. Phase A (§IV.10) derives `occurred_at` in a new pure unit, flows it `Note → Chunk → Qdrant payload + SQLite registry`, and applies it as a true pre-filter to **both** retrieval arms. Phase B (§IV.17) turns the synthesis prompt's existing policies into a conditional procedure — it depends on Phase A task 10, because there is nothing to reconcile on until the model sees dates. Phase C (§IV.18) generalises team-draft interleaving to n rankers and enters a cross-encoder as a third competing ordering, so `daemon policy` reports whether it helps.

**Tech Stack:** Python 3.11–3.14, pydantic v2, Typer, embedded Qdrant (`qdrant-client`), NetworkX, SQLite via `stores/db.py` migration ladder, `sentence-transformers` (already a declared dependency), pytest.

## Global Constraints

- **Every new code file starts with `# SPDX-License-Identifier: Apache-2.0`** (CLAUDE.md rule 9).
- **Every new dependency must be licence-checked** with `.venv/bin/python scripts/license_check.py` before use (CLAUDE.md rule 10). This plan adds **no new pip package**; Phase C adds a model *artifact* whose licence must be verified manually — see Task 14 Step 1.
- **TDD throughout.** Write the failing test, watch it fail, implement minimally, watch it pass, commit.
- **`ruff format` + `ruff check` + `mypy` must be clean** before each commit. Run: `.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src`.
- **Run the full suite before each commit:** `.venv/bin/pytest -q`. Baseline is **789 passing**. Never commit a red suite.
- **No new construction site may break.** Every new model field is optional with a default; every new constructor parameter defaults to `None`, matching how `sparse_embedder` and `listeners` already work on `RetrievalOrchestrator`.
- **All timestamps are timezone-aware UTC `datetime`.** A bare date becomes UTC midnight. Never construct a naive datetime.
- **Config changes land in BOTH `config.yaml` and `config.example.yaml`**, which must stay in sync (there is a standing bug class here — `agent.enabled` once diverged).
- **Docs:** update `docs-source/cli.md` and `docs-source/configuration.md` for any new command or config key (CLAUDE.md rule 8).
- Tests live flat in `tests/test_*.py`.

---

## File Structure

**Created:**
- `src/my_daemon/vault/dates.py` — derives `occurred_at` + its source from frontmatter, filename, or guarded mtime. Pure; no filesystem access. One public function.
- `src/my_daemon/retrieval/rerank.py` — `CrossEncoderReranker`: lazy model load, `download()`, `rank()` returning scores only. Knows nothing about `RetrievedChunk` or config.
- `tests/test_dates.py`, `tests/test_date_filter.py`, `tests/test_backfill_dates.py`, `tests/test_prompts.py`, `tests/test_multileave.py`, `tests/test_rerank.py`, `tests/test_live_prompts.py` (opt-in).

**Modified:**
- `src/my_daemon/models.py` — `Note`, `Chunk`, `RetrievalResult` gain fields; new `DateRange`.
- `src/my_daemon/config.py` — `VaultConfig` and `RetrievalConfig` gain keys.
- `src/my_daemon/vault/parser.py:36` — `parse_note` calls `derive_occurred_at`.
- `src/my_daemon/vault/chunker.py:80` — `Chunk(...)` carries the dates through.
- `src/my_daemon/stores/vector.py` — `upsert` payload; `search`/`hybrid_search` accept a filter; new `count_undated`, `set_occurred_at`.
- `src/my_daemon/stores/db.py` — migration 7.
- `src/my_daemon/stores/registry.py` — persist `occurred_at`, `occurred_at_source`.
- `src/my_daemon/retrieval/seed.py`, `expand.py`, `orchestrator.py` — thread the filter; rebuild `Chunk` with dates.
- `src/my_daemon/retrieval/interleave.py` — `team_draft` → `multileave`.
- `src/my_daemon/llm/prompts.py` — context block dates; the reconciliation procedure.
- `src/my_daemon/pipeline/ingest.py:169` — metadata-refresh path writes dates; comment at the skip site.
- `src/my_daemon/cli.py` — `--since`/`--until`; `migrate backfill-dates`; `models download` pulls the reranker.
- `src/my_daemon/integration/core.py` — `recall(since=, until=)`.
- `src/my_daemon/doctor.py` — reranker-available-but-disabled check.

---

# Phase A — §IV.10 Episodic time-binding

Spec: `docs/superpowers/specs/2026-07-29-episodic-time-binding-design.md`

### Task 1: `vault/dates.py` — derive the date and its source

**Files:**
- Create: `src/my_daemon/vault/dates.py`
- Test: `tests/test_dates.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `derive_occurred_at(frontmatter: dict, relative_path: str, mtime: datetime, *, keys: Sequence[str], trusted_before: date | None) -> tuple[datetime | None, str | None]`. Second element is `"frontmatter" | "filename" | "mtime" | None`. Also exports `DEFAULT_DATE_KEYS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dates.py
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, date, datetime

import pytest

from my_daemon.vault.dates import DEFAULT_DATE_KEYS, derive_occurred_at

MTIME = datetime(2026, 7, 1, tzinfo=UTC)


def _derive(fm=None, path="note.md", mtime=MTIME, trusted_before=None):
    return derive_occurred_at(
        fm or {}, path, mtime, keys=DEFAULT_DATE_KEYS, trusted_before=trusted_before
    )


@pytest.mark.parametrize(
    "value",
    [
        "2024-09-02",                                    # PyYAML str (quoted)
        date(2024, 9, 2),                                # PyYAML date (bare)
        datetime(2024, 9, 2, 10, 30, tzinfo=UTC),        # PyYAML tz-aware datetime
    ],
)
def test_all_three_pyyaml_types_coerce(value):
    got, source = _derive({"date": value})
    assert got is not None
    assert got.date() == date(2024, 9, 2)
    assert got.tzinfo is not None, "must be timezone-aware"
    assert source == "frontmatter"


def test_bare_date_becomes_utc_midnight():
    got, _ = _derive({"date": date(2024, 9, 2)})
    assert got == datetime(2024, 9, 2, 0, 0, tzinfo=UTC)


def test_garbage_value_is_undated_not_an_error():
    assert _derive({"date": "soon"}) == (None, None)


def test_partial_date_is_undated():
    """A month-precision date is rejected, so every stored date is day-precision."""
    assert _derive({"date": "2024-09"}) == (None, None)
    assert _derive({"date": "2024"}) == (None, None)


def test_frontmatter_keys_are_case_insensitive():
    got, source = _derive({"Date": "2024-09-02"})
    assert got is not None and source == "frontmatter"


def test_key_precedence_prefers_occurred_at():
    got, _ = _derive({"created_at": "2020-01-01", "occurred_at": "2024-09-02"})
    assert got is not None and got.date() == date(2024, 9, 2)


def test_filename_prefix_fires_when_frontmatter_absent():
    got, source = _derive(path="2026-03-31 Journal Entry.md")
    assert got == datetime(2026, 3, 31, tzinfo=UTC)
    assert source == "filename"


def test_filename_date_mid_name_is_refused():
    """A date buried mid-name is much less likely to mean 'when this happened'."""
    assert _derive(path="Notes from 2024-09-02 meeting.md") == (None, None)


def test_frontmatter_beats_filename():
    got, source = _derive({"date": "2024-09-02"}, path="2026-03-31.md")
    assert got is not None and got.date() == date(2024, 9, 2)
    assert source == "frontmatter"


def test_nested_path_filename_prefix_still_matches():
    got, source = _derive(path="Journal/2026-04-26.md")
    assert got == datetime(2026, 4, 26, tzinfo=UTC) and source == "filename"


def test_no_signal_is_undated():
    assert _derive(path="Welcome.md") == (None, None)


# --- guarded mtime fallback ---

def test_mtime_fallback_is_off_by_default():
    assert _derive(path="Welcome.md", mtime=datetime(2026, 2, 21, tzinfo=UTC)) == (None, None)


def test_mtime_used_when_it_predates_the_trusted_cutoff():
    got, source = _derive(
        path="Welcome.md",
        mtime=datetime(2026, 2, 21, tzinfo=UTC),
        trusted_before=date(2026, 6, 21),
    )
    assert got == datetime(2026, 2, 21, tzinfo=UTC)
    assert source == "mtime"


def test_mtime_rejected_when_at_or_after_the_cutoff():
    """mtime at-or-after the import IS the import — 38-day median error."""
    assert _derive(
        path="Welcome.md",
        mtime=datetime(2026, 7, 1, tzinfo=UTC),
        trusted_before=date(2026, 6, 21),
    ) == (None, None)


def test_mtime_never_overrides_a_parsed_date():
    got, source = _derive(
        {"date": "2024-09-02"},
        mtime=datetime(2020, 1, 1, tzinfo=UTC),
        trusted_before=date(2026, 6, 21),
    )
    assert got is not None and got.date() == date(2024, 9, 2)
    assert source == "frontmatter"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dates.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'my_daemon.vault.dates'`

- [ ] **Step 3: Implement `vault/dates.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Derive a note's *episodic* date — when the thing happened, not when the file changed.

Three sources, in order: an explicit frontmatter key, a `YYYY-MM-DD` filename
prefix, and — only when a trusted cutoff is configured — mtime.

Two rules carry most of the weight, and both are refusals:

*A partial date yields nothing.* `date: 2024-09` is not month-precision data, it
is an absent day. Accepting it would force a granularity flag through every
downstream scorer, and the alternative that competitors ship is worse: store
`2024-01-01 .. 2024-12-31` and a proximity scorer places it at the midpoint, so a
year-granularity note behaves as though it happened on 2 July. Every date this
module returns is day-precision by construction.

*mtime is not episodic time* unless something vouches for it. Measured over 134
real notes against the 67 with a parsed date, raw mtime is 35 days out at the
median; restricted to values predating the vault import it is 0 days out. File
*birth* time is worse still (28-day median) because copying a file resets birth
time while preserving mtime — so the copy date is all it ever records.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime

#: Tried in this order, matched case-insensitively — real vaults carry both
#: `date:` and `Date:`. `occurred_at` leads so a user can override explicitly.
DEFAULT_DATE_KEYS: tuple[str, ...] = ("occurred_at", "date", "created", "created_at")

#: Anchored at the start only. A date mid-name usually describes what the note is
#: *about* rather than when it happened.
_FILENAME_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

#: A full ISO day. Anything shorter is an absent day, not a coarse one.
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)")


def _to_utc_midnight(value: date) -> datetime:
    """A bare date becomes midnight **UTC**, never local midnight.

    Local midnight silently shifts a note by a day for anyone east of Greenwich,
    and — worse — makes the stored value depend on which machine ingested it. The
    property that matters is not *which* midnight but that both sides of the
    later comparison use the same one.
    """

    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _coerce(value: object) -> datetime | None:
    """PyYAML returns str, date, or datetime for the same-looking frontmatter."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return _to_utc_midnight(value)
    if isinstance(value, str):
        match = _ISO_DAY.match(value.strip())
        if not match:
            return None
        try:
            return _to_utc_midnight(date(*(int(g) for g in match.groups())))
        except ValueError:
            return None  # 2024-13-45 — a malformed date is an undated note
    return None


def derive_occurred_at(
    frontmatter: dict,
    relative_path: str,
    mtime: datetime,
    *,
    keys: Sequence[str] = DEFAULT_DATE_KEYS,
    trusted_before: date | None = None,
) -> tuple[datetime | None, str | None]:
    """Return ``(occurred_at, source)``; ``(None, None)`` when honestly undated.

    Pure: the caller supplies everything, so this never touches the filesystem
    and is exhaustively testable.
    """

    lowered = {str(k).lower(): v for k, v in frontmatter.items()}
    for key in keys:
        if key.lower() in lowered:
            got = _coerce(lowered[key.lower()])
            if got is not None:
                return got, "frontmatter"

    basename = relative_path.rsplit("/", 1)[-1]
    match = _FILENAME_PREFIX.match(basename)
    if match:
        try:
            return _to_utc_midnight(date(*(int(g) for g in match.groups()))), "filename"
        except ValueError:
            pass  # 2024-99-99 as a prefix — fall through to mtime/None

    if trusted_before is not None and mtime.date() < trusted_before:
        return mtime, "mtime"

    return None, None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dates.py -q`
Expected: all pass (18 tests).

- [ ] **Step 5: Lint, typecheck, full suite, commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add src/my_daemon/vault/dates.py tests/test_dates.py
git commit -m "IV.10: derive a note's episodic date, or honestly nothing"
```

---

### Task 2: Config keys

**Files:**
- Modify: `src/my_daemon/config.py:61` (`VaultConfig`)
- Modify: `config.yaml`, `config.example.yaml`
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `DEFAULT_DATE_KEYS` from Task 1.
- Produces: `settings.vault.date_frontmatter_keys: list[str]`, `settings.vault.mtime_trusted_before: date | None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_config.py
from datetime import date

from my_daemon.config import Settings


def test_date_config_defaults_are_conservative():
    s = Settings()
    assert s.vault.date_frontmatter_keys == ["occurred_at", "date", "created", "created_at"]
    assert s.vault.mtime_trusted_before is None, "mtime fallback must be off by default"


def test_mtime_trusted_before_parses_an_iso_date():
    s = Settings.model_validate({"vault": {"mtime_trusted_before": "2026-06-21"}})
    assert s.vault.mtime_trusted_before == date(2026, 6, 21)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py -k date -q`
Expected: FAIL — `AttributeError: 'VaultConfig' object has no attribute 'date_frontmatter_keys'`

- [ ] **Step 3: Add the fields**

In `src/my_daemon/config.py`, add to `VaultConfig`:

```python
    # Frontmatter keys consulted for a note's episodic date, in order, matched
    # case-insensitively. Tunable because vaults differ; this list covers the
    # four keys observed in the author's.
    date_frontmatter_keys: list[str] = Field(
        default_factory=lambda: ["occurred_at", "date", "created", "created_at"]
    )
    # When set, an otherwise-undated note whose mtime predates this date takes
    # its date from mtime. Off by default, and deliberately a *cutoff* rather
    # than a birth-time comparison: Python exposes no st_birthtime on Linux, and
    # the assumption belongs somewhere the user can see and challenge it.
    #
    # The value is the earliest bulk-import event for this vault. Anything older
    # than it survived a copy and reflects real authoring; anything at or after
    # it is the copy. Measured on the author's vault: mtime before the import is
    # 0 days from the true date at the median, after it 38 days.
    # `daemon migrate backfill-dates` prints the detected import clusters.
    mtime_trusted_before: date | None = None
```

Add `from datetime import date` to the imports.

Then add to **both** yaml files, under `vault:`:

```yaml
  # Frontmatter keys consulted for a note's episodic date, in order.
  date_frontmatter_keys: ["occurred_at", "date", "created", "created_at"]
  # Optional. Set to your vault's earliest bulk-import date to let otherwise
  # undated notes take their date from mtime. Run
  # `daemon migrate backfill-dates --dry-run` to see the detected import dates.
  # mtime_trusted_before: 2026-06-21
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_config.py -k date -q`
Expected: PASS

- [ ] **Step 5: Verify the two yamls stayed in sync, then commit**

```bash
diff <(grep -A4 'date_frontmatter_keys' config.yaml) <(grep -A4 'date_frontmatter_keys' config.example.yaml) && echo "in sync"
.venv/bin/pytest -q
git add src/my_daemon/config.py config.yaml config.example.yaml tests/test_config.py
git commit -m "IV.10: config for date keys and the guarded mtime cutoff"
```

---

### Task 3: `Note` carries the date; `parse_note` derives it

**Files:**
- Modify: `src/my_daemon/models.py:33` (`Note`)
- Modify: `src/my_daemon/vault/parser.py:36` (`parse_note`)
- Test: `tests/test_parser.py` (append)

**Interfaces:**
- Consumes: `derive_occurred_at` (Task 1), config fields (Task 2).
- Produces: `Note.occurred_at: datetime | None`, `Note.occurred_at_source: str | None`; `parse_note(file_path, vault_root, *, date_keys=DEFAULT_DATE_KEYS, mtime_trusted_before=None)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_parser.py
from datetime import UTC, date, datetime

from my_daemon.vault.parser import parse_note


def test_parse_note_derives_occurred_at_from_frontmatter(tmp_path):
    p = tmp_path / "note.md"
    p.write_text('---\ndate: "2024-09-02"\n---\n# Title\nbody\n', encoding="utf-8")
    note = parse_note(p, tmp_path)
    assert note.occurred_at == datetime(2024, 9, 2, tzinfo=UTC)
    assert note.occurred_at_source == "frontmatter"


def test_parse_note_derives_occurred_at_from_filename(tmp_path):
    p = tmp_path / "2026-03-31 Journal Entry.md"
    p.write_text("# Entry\nbody\n", encoding="utf-8")
    note = parse_note(p, tmp_path)
    assert note.occurred_at == datetime(2026, 3, 31, tzinfo=UTC)
    assert note.occurred_at_source == "filename"


def test_parse_note_leaves_undated_notes_undated(tmp_path):
    p = tmp_path / "Welcome.md"
    p.write_text("# Welcome\nbody\n", encoding="utf-8")
    note = parse_note(p, tmp_path)
    assert note.occurred_at is None and note.occurred_at_source is None
    assert note.mtime is not None, "mtime is still recorded, just not as the episodic axis"


def test_parse_note_honours_the_mtime_cutoff(tmp_path):
    import os

    p = tmp_path / "Welcome.md"
    p.write_text("# Welcome\nbody\n", encoding="utf-8")
    old = datetime(2026, 2, 21, tzinfo=UTC).timestamp()
    os.utime(p, (old, old))
    note = parse_note(p, tmp_path, mtime_trusted_before=date(2026, 6, 21))
    assert note.occurred_at == datetime(2026, 2, 21, tzinfo=UTC)
    assert note.occurred_at_source == "mtime"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_parser.py -k occurred -q`
Expected: FAIL — `AttributeError: 'Note' object has no attribute 'occurred_at'`

- [ ] **Step 3: Add the model fields and wire the parser**

In `models.py`, after `Note.mtime`:

```python
    # When the described thing *happened*, derived from frontmatter or filename
    # (see `vault/dates.py`). None means honestly undated — never a guess.
    # `mtime` above is when the *file* changed and is deliberately a separate
    # axis: on a copied vault it records the copy, so using it here would date a
    # 2024 journal entry to the import.
    occurred_at: datetime | None = None
    #: "frontmatter" | "filename" | "mtime" | None — so a lower-confidence date
    #: stays distinguishable downstream without a second backfill.
    occurred_at_source: str | None = None
```

In `parser.py`, add the import and change the signature:

```python
from my_daemon.vault.dates import DEFAULT_DATE_KEYS, derive_occurred_at


def parse_note(
    file_path: Path,
    vault_root: Path,
    *,
    date_keys: Sequence[str] = DEFAULT_DATE_KEYS,
    mtime_trusted_before: date | None = None,
) -> Note:
```

(Add `from collections.abc import Sequence` and `date` to the `datetime` import.)

Then, after the existing `mtime = ...` line:

```python
    occurred_at, occurred_at_source = derive_occurred_at(
        fm, rel_path, mtime, keys=date_keys, trusted_before=mtime_trusted_before
    )
```

and pass both into the `Note(...)` constructor.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_parser.py -q`
Expected: PASS, including all pre-existing parser tests (the new kwargs are keyword-only with defaults).

- [ ] **Step 5: Thread the config through the reader**

Find the `parse_note` call sites and pass the config values:

```bash
grep -rn "parse_note(" src/my_daemon/ | grep -v "def parse_note"
```

For each caller that has `Settings` in scope, pass
`date_keys=settings.vault.date_frontmatter_keys, mtime_trusted_before=settings.vault.mtime_trusted_before`.
Callers without settings in scope keep the defaults — which is the conservative
behaviour (no mtime fallback).

- [ ] **Step 6: Full suite, commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add src/my_daemon/models.py src/my_daemon/vault/parser.py tests/test_parser.py
git commit -m "IV.10: Note carries occurred_at and its provenance"
```

---

### Task 4: `Chunk` carries the dates into the Qdrant payload

**Files:**
- Modify: `src/my_daemon/models.py` (`Chunk`)
- Modify: `src/my_daemon/vault/chunker.py:80`
- Modify: `src/my_daemon/stores/vector.py:244` (`upsert` payload)
- Modify: `src/my_daemon/retrieval/seed.py:32`, `src/my_daemon/retrieval/expand.py:57`
- Test: `tests/test_chunker.py`, `tests/test_vector_store.py` (append)

**Interfaces:**
- Consumes: `Note.occurred_at`, `Note.occurred_at_source` (Task 3).
- Produces: `Chunk.occurred_at`, `Chunk.occurred_at_source`, `Chunk.modified_at`; payload keys `occurred_at`, `occurred_at_source`, `modified_at` (ISO strings, **omitted when None**).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_chunker.py
def test_chunks_inherit_the_notes_dates(tmp_path):
    from datetime import UTC, datetime

    from my_daemon.vault.chunker import chunk_note
    from my_daemon.vault.parser import parse_note

    p = tmp_path / "2026-03-31.md"
    p.write_text("# A\ntext one\n\n## B\ntext two\n", encoding="utf-8")
    note = parse_note(p, tmp_path)
    chunks = chunk_note(note, note_uuid="u1")
    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert c.occurred_at == datetime(2026, 3, 31, tzinfo=UTC)
        assert c.occurred_at_source == "filename"
        assert c.modified_at == note.mtime
```

```python
# append to tests/test_vector_store.py
def test_payload_omits_date_keys_when_undated(tmp_path):
    """An absent key gives cleaner range semantics than an explicit null."""
    from my_daemon.models import Chunk

    store = _embedded_store(tmp_path)          # existing helper in this file
    store.ensure_collection()
    dated = Chunk(
        id="c1", note_uuid="u1", note_path="a.md", text="x", chunk_index=0,
        occurred_at=datetime(2024, 9, 2, tzinfo=UTC), occurred_at_source="frontmatter",
    )
    undated = Chunk(id="c2", note_uuid="u2", note_path="b.md", text="y", chunk_index=0)
    store.upsert([dated, undated], [[0.1] * store.dim, [0.2] * store.dim])

    payloads = {p["chunk_id"]: p for p in store.all_payloads()}   # add if absent
    assert payloads["c1"]["occurred_at"] == "2024-09-02T00:00:00+00:00"
    assert payloads["c1"]["occurred_at_source"] == "frontmatter"
    assert "occurred_at" not in payloads["c2"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_chunker.py -k dates tests/test_vector_store.py -k omits -q`
Expected: FAIL — `Chunk` has no `occurred_at`.

- [ ] **Step 3: Implement**

`models.py`, in `Chunk` after `wikilinks`:

```python
    # Carried from the Note (see `vault/dates.py`). Optional and defaulted so
    # every existing construction site keeps working untouched.
    occurred_at: datetime | None = None
    occurred_at_source: str | None = None
    #: File mtime. Stored because the payload write is happening anyway and it
    #: answers "what changed recently?" — a real question `occurred_at`
    #: deliberately cannot. **Never used as the episodic axis, and never OR'd
    #: with `occurred_at` at query time**: conflating "happened then" with "was
    #: written then" is what makes a competitor's date filter meaningless.
    modified_at: datetime | None = None
```

`chunker.py:80`, add to the `Chunk(...)` call:

```python
                    occurred_at=note.occurred_at,
                    occurred_at_source=note.occurred_at_source,
                    modified_at=note.mtime,
```

`vector.py`, in `upsert` after building `payload`:

```python
            # Omitted rather than null when absent: `IsEmpty` then means exactly
            # "undated", and range filters have no null case to reason about.
            if chunk.occurred_at is not None:
                payload["occurred_at"] = chunk.occurred_at.isoformat()
            if chunk.occurred_at_source is not None:
                payload["occurred_at_source"] = chunk.occurred_at_source
            if chunk.modified_at is not None:
                payload["modified_at"] = chunk.modified_at.isoformat()
```

Add a module-level helper in `vector.py` and use it from **both** `seed.py` and `expand.py` so the two rebuild paths cannot drift:

```python
def chunk_from_payload(p: dict) -> Chunk:
    """Rebuild a Chunk from a Qdrant payload. Shared by seed and expansion.

    Both retrieval arms rebuild chunks from payloads, and they had drifted
    before; one function is what keeps a new payload field from reaching only
    half the pipeline.
    """

    def _dt(key: str) -> datetime | None:
        raw = p.get(key)
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None

    return Chunk(
        id=p["chunk_id"],
        note_uuid=p.get("note_uuid", ""),
        note_path=p["note_path"],
        heading_path=list(p.get("heading_path") or []),
        text=p.get("text", ""),
        chunk_index=int(p.get("chunk_index", 0)),
        tags=list(p.get("tags") or []),
        wikilinks=list(p.get("wikilinks") or []),
        occurred_at=_dt("occurred_at"),
        occurred_at_source=p.get("occurred_at_source"),
        modified_at=_dt("modified_at"),
    )
```

Replace the inline `Chunk(...)` construction in `seed.py:32` and `expand.py:57` with `chunk_from_payload(h)` / `chunk_from_payload(p)`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_chunker.py tests/test_vector_store.py -q`
Expected: PASS

- [ ] **Step 5: Full suite, commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add -A && git commit -m "IV.10: dates through Chunk into the payload, via one shared rebuild"
```

---

### Task 5: Migration 7 and registry persistence

**Files:**
- Modify: `src/my_daemon/stores/db.py`
- Modify: `src/my_daemon/stores/registry.py`
- Test: `tests/test_db_migrations.py`, `tests/test_registry.py` (append)

**Interfaces:**
- Consumes: `Note.occurred_at` (Task 3).
- Produces: `notes.occurred_at TEXT`, `notes.occurred_at_source TEXT`; schema `user_version = 7`; `NoteRecord.occurred_at`, `NoteRecord.occurred_at_source`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_db_migrations.py
def test_migration_7_adds_the_date_columns(tmp_path):
    import sqlite3

    from my_daemon.stores.db import migrate

    db = tmp_path / "state.db"
    migrate(db)
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
    assert {"occurred_at", "occurred_at_source"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 7


def test_migration_7_is_idempotent(tmp_path):
    from my_daemon.stores.db import migrate

    db = tmp_path / "state.db"
    migrate(db)
    migrate(db)          # must not raise "duplicate column name"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_db_migrations.py -k migration_7 -q`
Expected: FAIL — the columns are absent.

- [ ] **Step 3: Add migration 7**

Follow the existing additive pattern used for `feedback` and `queries` (the file
already does `ALTER TABLE … ADD COLUMN` guarded by a column-existence check —
copy that helper rather than inventing a new one).

```python
def _migration_7(conn: sqlite3.Connection) -> None:
    """Episodic dates on the note registry (§IV.10).

    Additive and nullable: every existing row is legitimately undated until
    `daemon migrate backfill-dates` runs. A NOT NULL default here would have
    manufactured a date for every note in the vault, which is the exact failure
    this feature exists to avoid.
    """

    for col, typ in (("occurred_at", "TEXT"), ("occurred_at_source", "TEXT")):
        if not _has_column(conn, "notes", col):
            conn.execute(f"ALTER TABLE notes ADD COLUMN {col} {typ}")
```

Register it in the migration ladder so `user_version` reaches 7.

- [ ] **Step 4: Persist the fields in the registry**

Add `occurred_at: datetime | None = None` and `occurred_at_source: str | None = None` to
`NoteRecord`, and include both in the registry's INSERT/UPDATE and row-mapping
code (ISO strings on the way in, `datetime.fromisoformat` on the way out —
matching how `mtime` is already handled).

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_db_migrations.py tests/test_registry.py -q`
Expected: PASS

- [ ] **Step 6: Bump the policy store's floor if needed, then commit**

`stores/policy.py` pins `_MIN_SCHEMA_VERSION = 6`. That stays 6 — it needs
migration 6, not 7. Verify no other store pins a version that must move:

```bash
grep -rn "_MIN_SCHEMA_VERSION" src/my_daemon/
.venv/bin/pytest -q
git add -A && git commit -m "IV.10: migration 7 — occurred_at on the note registry"
```

---

### Task 6: The `DateRange` type

**Files:**
- Modify: `src/my_daemon/models.py`
- Test: `tests/test_date_filter.py` (create)

**Interfaces:**
- Produces: `DateRange(since: datetime | None = None, until: datetime | None = None)`, frozen, validating `since <= until`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_date_filter.py
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from my_daemon.models import DateRange


def test_open_ended_on_either_side_is_allowed():
    """Required by the deferred NL step: an open-ended phrase must not invent a bound."""
    assert DateRange(since=datetime(2026, 1, 1, tzinfo=UTC)).until is None
    assert DateRange(until=datetime(2026, 1, 1, tzinfo=UTC)).since is None


def test_inverted_range_is_rejected():
    with pytest.raises(ValidationError):
        DateRange(
            since=datetime(2026, 6, 1, tzinfo=UTC),
            until=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_equal_bounds_are_allowed_and_select_nothing():
    """`until` is exclusive, so since == until is an empty half-open interval."""
    t = datetime(2026, 6, 1, tzinfo=UTC)
    assert DateRange(since=t, until=t).is_empty is True


def test_a_fully_open_range_is_inert():
    assert DateRange().is_active is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_date_filter.py -q`
Expected: FAIL — `ImportError: cannot import name 'DateRange'`

- [ ] **Step 3: Implement**

```python
class DateRange(BaseModel):
    """A half-open episodic interval: ``since <= occurred_at < until``.

    Exclusive ``until`` keeps interval arithmetic clean. The CLI converts a bare
    ``--until 2026-05-31`` into ``2026-06-01T00:00:00Z`` so the user's obvious
    intent — include that day — is honoured **at the boundary**, which is the
    only place a human-intent fudge belongs. Putting it deeper is how
    off-by-one-day bugs become unlocatable.

    Either bound may be ``None``. That is load-bearing rather than incidental:
    an open-ended phrase ("from March onward") or a recurring one ("every
    Wednesday") must resolve to an absent bound rather than an invented one.
    """

    model_config = ConfigDict(frozen=True)

    since: datetime | None = None
    until: datetime | None = None

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ValueError(f"since ({self.since}) must not be after until ({self.until})")
        return self

    @property
    def is_active(self) -> bool:
        """False when neither bound is set — no filter should be constructed."""
        return self.since is not None or self.until is not None

    @property
    def is_empty(self) -> bool:
        """True when the half-open interval cannot contain anything."""
        return self.since is not None and self.until is not None and self.since == self.until
```

Add `model_validator` to the pydantic imports.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_date_filter.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
.venv/bin/pytest -q
git add -A && git commit -m "IV.10: DateRange, half-open and validated"
```

---

### Task 7: The pre-filter reaches both arms, and reports coverage

**Files:**
- Modify: `src/my_daemon/stores/vector.py` (`search`, `hybrid_search`, new `count_undated`)
- Modify: `src/my_daemon/retrieval/seed.py`, `expand.py`, `orchestrator.py`
- Modify: `src/my_daemon/models.py` (`RetrievalResult`)
- Test: `tests/test_date_filter.py` (append)

**Interfaces:**
- Consumes: `DateRange` (Task 6), payload keys (Task 4).
- Produces: `search(vector, top_k, *, date_range=None)`, `hybrid_search(..., date_range=None)`, `count_undated() -> int`, `seed_search(..., date_range=None)`, `expand_from_seeds(..., date_range=None)`, `retrieve(query, *, surface, session_id, date_range=None)`, `RetrievalResult.date_range`, `RetrievalResult.undated_excluded`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_date_filter.py
from my_daemon.models import Chunk

DIM = 4


def _seeded_store(tmp_path):
    """Three notes: in-range, out-of-range, and undated. A links to B and C."""
    from my_daemon.stores.vector import VectorStore

    store = VectorStore(location=str(tmp_path / "q"), collection="chunks", dim=DIM, hybrid=False)
    store.ensure_collection()
    chunks = [
        Chunk(id="in", note_uuid="A", note_path="in.md", text="alpha", chunk_index=0,
              occurred_at=datetime(2026, 4, 1, tzinfo=UTC), occurred_at_source="frontmatter"),
        Chunk(id="out", note_uuid="B", note_path="out.md", text="alpha", chunk_index=0,
              occurred_at=datetime(2025, 1, 1, tzinfo=UTC), occurred_at_source="frontmatter"),
        Chunk(id="undated", note_uuid="C", note_path="undated.md", text="alpha", chunk_index=0),
    ]
    store.upsert(chunks, [[1.0, 0, 0, 0]] * 3)
    return store


RANGE = DateRange(since=datetime(2026, 3, 1, tzinfo=UTC), until=datetime(2026, 6, 1, tzinfo=UTC))


def test_seed_search_prefilters_by_date(tmp_path):
    store = _seeded_store(tmp_path)
    ids = {h["chunk_id"] for h in store.search([1.0, 0, 0, 0], top_k=10, date_range=RANGE)}
    assert ids == {"in"}, "out-of-range and undated must both be excluded"


def test_no_filter_is_built_when_the_range_is_inert(tmp_path):
    store = _seeded_store(tmp_path)
    ids = {h["chunk_id"] for h in store.search([1.0, 0, 0, 0], top_k=10, date_range=DateRange())}
    assert ids == {"in", "out", "undated"}


def test_the_filter_never_falls_back_to_modified_at(tmp_path):
    """A competitor ORs event time with assertion time, which voids the filter."""
    from my_daemon.stores.vector import VectorStore

    store = VectorStore(location=str(tmp_path / "q2"), collection="chunks", dim=DIM, hybrid=False)
    store.ensure_collection()
    store.upsert(
        [Chunk(id="mt", note_uuid="D", note_path="d.md", text="alpha", chunk_index=0,
               modified_at=datetime(2026, 4, 1, tzinfo=UTC))],
        [[1.0, 0, 0, 0]],
    )
    assert store.search([1.0, 0, 0, 0], top_k=10, date_range=RANGE) == [], (
        "modified_at inside the range must not satisfy an occurred_at filter"
    )


def test_expansion_is_filtered_too(tmp_path):
    """Only 1 of 4 arms is filtered in Hindsight; that degrades a filter to a nudge."""
    from my_daemon.retrieval.expand import expand_from_seeds
    from my_daemon.retrieval.seed import seed_search
    from my_daemon.stores import GraphStore

    store = _seeded_store(tmp_path)
    graph = GraphStore(tmp_path / "g.gpickle")
    for uuid in ("A", "B", "C"):
        graph.add_node_for_test(uuid)          # use the store's real add-note API
    graph.add_edge_for_test("A", "B")
    graph.add_edge_for_test("A", "C")

    seeds = [rc for rc in _fake_seeds(store) if rc.chunk.note_uuid == "A"]
    expanded = expand_from_seeds(seeds, graph, store, depth=2, decay=0.5, date_range=RANGE)
    assert [rc.chunk.id for rc in expanded] == [], (
        "B is out of range and C is undated — neither may arrive via the graph"
    )


def test_result_reports_the_undated_count(tmp_path):
    store = _seeded_store(tmp_path)
    assert store.count_undated() == 1
```

*(Adapt `add_node_for_test` / `_fake_seeds` to the real `GraphStore.add_note`
signature and the existing test helpers in `tests/test_expand_seam.py` — do not
add test-only methods to production classes.)*

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_date_filter.py -q`
Expected: FAIL — `search() got an unexpected keyword argument 'date_range'`

- [ ] **Step 3: Implement the filter in `vector.py`**

```python
def _date_filter(date_range: DateRange | None):
    """A Qdrant filter over `occurred_at`, or None when no range is active.

    Reads `occurred_at` and nothing else. It must never fall back to
    `modified_at` or OR the two together: a query for "March 2024" would then
    match things *written* then as well as things that *happened* then, which
    silently voids the distinction the two fields exist to draw.

    Undated points carry no `occurred_at` key at all, so a range condition
    excludes them without an explicit clause — which is decision 3, exclude the
    undated, expressed structurally.
    """

    if date_range is None or not date_range.is_active:
        return None
    from qdrant_client.http.models import DatetimeRange, FieldCondition, Filter

    return Filter(
        must=[
            FieldCondition(
                key="occurred_at",
                range=DatetimeRange(gte=date_range.since, lt=date_range.until),
            )
        ]
    )
```

Add `date_range: DateRange | None = None` to `search` and `hybrid_search`, and
pass `query_filter=_date_filter(date_range)` into `client.query_points(...)`.
Qdrant accepts `query_filter=None` as "no filter", so no branch is needed.

Add:

```python
    def count_undated(self) -> int:
        """Chunks with no `occurred_at`. Used only to report filter coverage."""
        from qdrant_client.http.models import Filter, IsEmptyCondition, PayloadField

        return self._client_().count(
            collection_name=self.collection,
            count_filter=Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="occurred_at"))]),
            exact=True,
        ).count
```

- [ ] **Step 4: Thread it through both arms and the orchestrator**

- `seed_search(..., date_range: DateRange | None = None)` → passes to
  `search` / `hybrid_search`.
- `expand_from_seeds(..., date_range: DateRange | None = None)` → merges
  `_date_filter(date_range)`'s conditions into the existing `scroll_filter`
  alongside the `note_uuid` `FieldCondition`.
- `RetrievalOrchestrator.retrieve(..., date_range: DateRange | None = None)` →
  passes to both, and populates the new result fields:

```python
        result = RetrievalResult(
            query=query, seeds=seeds, expanded=expanded, ranked=kept,
            date_range=date_range,
            # Only when a filter is active: an unfiltered query must pay nothing.
            undated_excluded=(
                self.vector_store.count_undated()
                if date_range is not None and date_range.is_active
                else 0
            ),
        )
```

Add to `RetrievalResult`:

```python
    date_range: DateRange | None = None
    #: Vault-wide count of chunks with no date, when a temporal filter was
    #: active. Vault-wide rather than per-query because knowing which undated
    #: chunks *would* have matched requires running the unfiltered query too —
    #: double the cost for substantially the same number.
    undated_excluded: int = 0
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_date_filter.py tests/test_expand_seam.py -q`
Expected: PASS

- [ ] **Step 6: Full suite, commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add -A && git commit -m "IV.10: pre-filter both retrieval arms on occurred_at only"
```

---

### Task 8: Backfill, the metadata-refresh path, and the skip-site comment

**Files:**
- Modify: `src/my_daemon/stores/vector.py` (new `set_occurred_at`)
- Modify: `src/my_daemon/pipeline/migrate_uuids.py` *or* new `src/my_daemon/pipeline/backfill_dates.py`
- Modify: `src/my_daemon/pipeline/ingest.py:169`
- Modify: `src/my_daemon/cli.py` (`migrate backfill-dates`)
- Test: `tests/test_backfill_dates.py` (create)

**Interfaces:**
- Produces: `set_occurred_at(note_uuid, occurred_at: datetime | None, source: str | None, modified_at: datetime | None) -> None`; `backfill_dates(settings, *, dry_run: bool = False) -> BackfillReport` with counts `frontmatter`, `filename`, `mtime`, `undated`, and `import_clusters: dict[str, int]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backfill_dates.py
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime


def test_backfill_reports_the_derivation_breakdown(tmp_path, settings_for):
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    (tmp_path / "dated.md").write_text('---\ndate: "2024-09-02"\n---\nbody\n', encoding="utf-8")
    (tmp_path / "Welcome.md").write_text("# W\nbody\n", encoding="utf-8")

    from my_daemon.pipeline.backfill_dates import backfill_dates

    report = backfill_dates(settings_for(vault=tmp_path))
    assert report.frontmatter == 1
    assert report.filename == 1
    assert report.undated == 1


def test_backfill_is_idempotent(tmp_path, settings_for):
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    from my_daemon.pipeline.backfill_dates import backfill_dates

    s = settings_for(vault=tmp_path)
    first = backfill_dates(s)
    second = backfill_dates(s)
    assert (first.frontmatter, first.filename, first.undated) == (
        second.frontmatter, second.filename, second.undated
    )


def test_backfill_does_not_touch_vectors(tmp_path, settings_for):
    """Chunk ids don't hash the timestamp, so this is a payload write, not a re-embed."""
    from my_daemon.pipeline.backfill_dates import backfill_dates

    s = settings_for(vault=tmp_path)
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    # ingest first, capture a vector, backfill, compare
    before = _first_vector(s)
    backfill_dates(s)
    assert _first_vector(s) == before


def test_dry_run_writes_nothing(tmp_path, settings_for):
    from my_daemon.pipeline.backfill_dates import backfill_dates

    s = settings_for(vault=tmp_path)
    (tmp_path / "2026-04-26.md").write_text("# A\nbody\n", encoding="utf-8")
    report = backfill_dates(s, dry_run=True)
    assert report.filename == 1
    assert _stored_occurred_at(s, "2026-04-26.md") is None


def test_frontmatter_edit_refreshes_the_date_without_re_embedding(tmp_path, settings_for):
    """The existing fm_changed branch must now carry occurred_at (finding 5b)."""
    from my_daemon.pipeline.ingest import ingest_vault

    s = settings_for(vault=tmp_path)
    p = tmp_path / "note.md"
    p.write_text('---\ndate: "2024-09-02"\n---\nbody\n', encoding="utf-8")
    ingest_vault(s)
    p.write_text('---\ndate: "2025-01-15"\n---\nbody\n', encoding="utf-8")
    ingest_vault(s)
    assert _stored_occurred_at(s, "note.md") == datetime(2025, 1, 15, tzinfo=UTC)
```

*(Reuse the existing ingest fixtures — `tests/test_ingest.py` already builds a
settings object over a temp vault with an embedded store; factor its helper into
a fixture named `settings_for` rather than duplicating it.)*

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_backfill_dates.py -q`
Expected: FAIL — `ModuleNotFoundError: my_daemon.pipeline.backfill_dates`

- [ ] **Step 3: Add `set_occurred_at` to `vector.py`**

Copy `set_note_path` (`vector.py:313`) exactly, changing the payload keys. Omit
a key by using `client.delete_payload` for the `None` case, so an undated note
that previously had a date does not keep a stale one.

- [ ] **Step 4: Implement `pipeline/backfill_dates.py`**

Walk the vault with the existing reader, derive per note, and for each:
`vector_store.set_occurred_at(...)` plus a registry update. Accumulate the
counts. For `import_clusters`, shell out to `stat -c %w` per note under a
`try/except` and skip silently when unavailable (it is a diagnostic, not a
dependency) — group by date and keep counts above 1.

- [ ] **Step 5: Extend the ingest metadata-refresh path and add the comment**

In `ingest.py`, inside the `fm_changed` / `renamed` branches, call
`vector_store.set_occurred_at(...)` with the freshly derived values. Then add at
the skip site:

```python
        # NOTE: this manifest treats chunk payload as a pure function of chunk
        # *content*, which stopped being true when `occurred_at` arrived (§IV.10).
        # A payload field derived from frontmatter or filename is refreshed by
        # the `fm_changed` / `renamed` branches below, but is invisible to a
        # body-hash comparison — which is why a one-time
        # `daemon migrate backfill-dates` exists. The next metadata-derived
        # payload field will hit this same wall.
```

- [ ] **Step 6: Wire the CLI**

```python
@migrate_app.command("backfill-dates")
def migrate_backfill_dates(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report and write nothing."),
) -> None:
    """Populate occurred_at on existing chunks without re-embedding."""
```

Print the breakdown and, when any were detected, the import clusters with a hint
that `vault.mtime_trusted_before` can use the earliest of them.

- [ ] **Step 7: Run to verify they pass, then commit**

```bash
.venv/bin/pytest tests/test_backfill_dates.py tests/test_ingest.py -q
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add -A && git commit -m "IV.10: one-time date backfill; refresh path carries occurred_at"
```

---

### Task 9: CLI and `DaemonCore` surfaces

**Files:**
- Modify: `src/my_daemon/cli.py` (`query`, `ask`, `_run_query`)
- Modify: `src/my_daemon/integration/core.py` (`recall`, `retrieve_only`)
- Modify: `docs-source/cli.md`, `docs-source/configuration.md`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `DateRange` (Task 6), `retrieve(date_range=)` (Task 7).
- Produces: `--since` / `--until` on `query` and `ask`; `DaemonCore.recall(..., since=None, until=None)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli.py
def test_bare_until_becomes_exclusive_next_midnight():
    """The human-intent fudge lives at the boundary and nowhere deeper."""
    from datetime import UTC, datetime

    from my_daemon.cli import _parse_date_bounds

    r = _parse_date_bounds("2026-03-01", "2026-05-31")
    assert r.since == datetime(2026, 3, 1, tzinfo=UTC)
    assert r.until == datetime(2026, 6, 1, tzinfo=UTC)


def test_no_bounds_gives_an_inert_range():
    from my_daemon.cli import _parse_date_bounds

    assert _parse_date_bounds(None, None).is_active is False


def test_inverted_bounds_exit_with_a_clear_message(cli_runner):
    result = cli_runner.invoke(app, ["query", "x", "--since", "2026-06-01", "--until", "2026-01-01"])
    assert result.exit_code != 0
    assert "must not be after" in result.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k bounds -q`
Expected: FAIL — `_parse_date_bounds` does not exist.

- [ ] **Step 3: Implement**

```python
def _parse_date_bounds(since: str | None, until: str | None) -> DateRange:
    """ISO dates from the CLI into a half-open UTC DateRange.

    A bare `--until 2026-05-31` becomes `2026-06-01T00:00:00Z`, because the user
    means "include that day" and the core interval is exclusive. This conversion
    belongs here, at the boundary — pushing it inward is how off-by-one-day bugs
    stop being locatable.
    """

    def _at_midnight(raw: str) -> datetime:
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC)
        except ValueError:
            raise typer.BadParameter(f"expected an ISO date like 2026-03-01, got {raw!r}") from None

    lo = _at_midnight(since) if since else None
    hi = _at_midnight(until) + timedelta(days=1) if until else None
    try:
        return DateRange(since=lo, until=hi)
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from None
```

Add the options to `query` and `ask`, pass through `_run_query`, and render the
coverage line when `result.undated_excluded` is non-zero:

```python
    if result.date_range is not None and result.date_range.is_active:
        total = vector_store.count()
        dated = total - result.undated_excluded
        console.print(
            f"[dim]Temporal filter: {result.date_range.since:%Y-%m-%d} → "
            f"{result.date_range.until:%Y-%m-%d} (UTC)\n"
            f"Coverage: {dated:,} of {total:,} chunks carry a date — "
            f"{result.undated_excluded:,} undated chunks not considered.[/dim]"
        )
```

Add `since` / `until` to `DaemonCore.recall`, converting with the same helper,
and add them to the Hermes tool schema in `hermes/provider.py` as optional ISO
strings with a description that says the model must supply absolute dates.

- [ ] **Step 4: Run, document, commit**

```bash
.venv/bin/pytest tests/test_cli.py -q
```

Update `docs-source/cli.md` (the two new flags, the new `migrate backfill-dates`
command) and `docs-source/configuration.md` (`vault.date_frontmatter_keys`,
`vault.mtime_trusted_before`), **including the honest coverage figure**: about
half the notes in a typical vault carry no derivable date, and the mtime fallback
recovers only a fraction of them.

```bash
.venv/bin/pytest -q
git add -A && git commit -m "IV.10: --since/--until, recall bounds, coverage reporting, docs"
```

---

### Task 10: Dates into the context block (amendment 2)

**Files:**
- Modify: `src/my_daemon/llm/prompts.py` (`build_context_block`)
- Test: `tests/test_prompts.py` (create)

**Interfaces:**
- Consumes: `Chunk.occurred_at`, `Chunk.occurred_at_source` (Task 4).
- Produces: a context block whose entries carry `[YYYY-MM-DD]`, `[YYYY-MM-DD~]`, or `(undated)`, and **no** `score=` / `vector=`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prompts.py
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

from my_daemon.llm.prompts import build_context_block
from my_daemon.models import Chunk, RetrievedChunk


def _rc(**kw):
    defaults = dict(id="c", note_uuid="u", note_path="n.md", text="body", chunk_index=0)
    return RetrievedChunk(chunk=Chunk(**{**defaults, **kw}), vector_score=0.8, combined_score=0.4)


def test_dated_chunk_renders_an_iso_date():
    out = build_context_block([_rc(occurred_at=datetime(2026, 3, 31, tzinfo=UTC),
                                  occurred_at_source="frontmatter")])
    assert "[2026-03-31]" in out


def test_inferred_date_is_marked_approximate():
    out = build_context_block([_rc(occurred_at=datetime(2026, 2, 21, tzinfo=UTC),
                                  occurred_at_source="mtime")])
    assert "[2026-02-21~]" in out


def test_undated_chunk_says_so_explicitly():
    """The model must distinguish 'no date' from 'I wasn't told'."""
    assert "(undated)" in build_context_block([_rc()])


def test_internal_retrieval_scores_are_not_shown():
    out = build_context_block([_rc(occurred_at=datetime(2026, 3, 31, tzinfo=UTC))])
    assert "score=" not in out and "vector=" not in out and "graph_distance" not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_prompts.py -q`
Expected: FAIL — the block contains `score=` and no date.

- [ ] **Step 3: Rewrite `build_context_block`**

```python
def _date_marker(chunk: Chunk) -> str:
    """How a chunk's date is shown to the model.

    `~` means inferred (from mtime) rather than stated. The distinction is not
    decoration: an inferred date is usable for ordering but must not be
    presented to the user as certain, and the system prompt says so.
    """

    if chunk.occurred_at is None:
        return "(undated)"
    stamp = chunk.occurred_at.date().isoformat()
    return f"[{stamp}~]" if chunk.occurred_at_source == "mtime" else f"[{stamp}]"


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for i, rc in enumerate(chunks, start=1):
        heading = " › ".join(rc.chunk.heading_path) if rc.chunk.heading_path else "(no heading)"
        # Retrieval scores are deliberately absent. They are facts about the
        # machinery, no instruction consumes them, and showing them invites the
        # model to treat our rank order as evidence about the world.
        parts.append(
            f"[{i}] {_date_marker(rc.chunk)} {rc.chunk.note_path} › {heading}\n"
            f"{rc.chunk.text.strip()}"
        )
    return "\n\n---\n\n".join(parts)
```

- [ ] **Step 4: Run, full suite, commit**

```bash
.venv/bin/pytest tests/test_prompts.py -q
.venv/bin/pytest -q          # some prompt snapshot tests may need updating
git add -A && git commit -m "IV.10: show the model dates, not retrieval scores"
```

- [ ] **Step 5: Mark §IV.10 shipped**

Tick `IV.10` in `MY-DAEMON-RESEARCH-APPLIED.md` (table marker → ✅, date, and the
body checkbox → `- [x]`), update the counts line, flip §III.3's heading from
*open* to *closed <date>* while keeping its coverage caveat, and add a Done entry
to `PROJECT_MANAGEMENT.md`. Commit.

---

# Phase B — §IV.17 Conditional reconciliation

Spec: `docs/superpowers/specs/2026-07-30-conditional-reconciliation-design.md`
**Requires Phase A Task 10.**

### Task 11: The prompt procedure

**Files:**
- Modify: `src/my_daemon/llm/prompts.py` (`SYSTEM_PROMPT`)
- Test: `tests/test_prompts.py` (append)

**Interfaces:**
- Produces: named constants `_RECONCILIATION_RULE`, `_UPDATE_VS_CONTRADICTION_RULE`, `_NO_COMPUTATION_RULE`, `_ABSTENTION_RULE`, `_UNDATED_RULE`, composed into `SYSTEM_PROMPT`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_prompts.py
from my_daemon.llm import prompts


def test_every_required_rule_is_present_in_the_system_prompt():
    """Structural, and deliberately brittle: a future edit must not silently
    delete one of these. Asserted against the named constants so a considered
    rewording is a one-line change while an accidental deletion is not."""
    for rule in (
        prompts._RECONCILIATION_RULE,
        prompts._UPDATE_VS_CONTRADICTION_RULE,
        prompts._NO_COMPUTATION_RULE,
        prompts._ABSTENTION_RULE,
        prompts._UNDATED_RULE,
    ):
        assert rule.strip() in prompts.SYSTEM_PROMPT


def test_the_reconciliation_rule_is_conditional_not_mandatory():
    text = prompts._RECONCILIATION_RULE.lower()
    assert "if no excerpts compete" in text or "do not emit" in text


def test_assembled_prompt_snapshot(snapshot_fixture):
    """Pins the seam between the context block and the prompt."""
    msg = prompts.build_user_message(
        "where did I decide to move?",
        [_rc(occurred_at=datetime(2026, 6, 14, tzinfo=UTC), note_path="Moving Plans.md",
             text="Denver it is."),
         _rc(occurred_at=datetime(2026, 3, 2, tzinfo=UTC), note_path="Journal.md",
             text="Portland feels right."),
         _rc(note_path="Old Notes.md", text="no date here")],
    )
    assert "[2026-06-14]" in msg and "[2026-03-02]" in msg and "(undated)" in msg
    assert "score=" not in msg
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_prompts.py -k rule -q`
Expected: FAIL — `AttributeError: module has no attribute '_RECONCILIATION_RULE'`

- [ ] **Step 3: Implement the rules**

```python
_RECONCILIATION_RULE = """
When two or more excerpts make competing claims about the same thing, or when the
answer depends on which statement is more recent, reconcile them visibly before
answering. Emit a short block:

── Reconciling dated statements ──
  <claim>  (<date>, <note>)   ← most recent
  <claim>  (<date>, <note>)   superseded

Then give the answer. If no excerpts compete, do not emit the block at all.
"""

_UPDATE_VS_CONTRADICTION_RULE = """
Treat a change of mind and a disagreement differently.

An UPDATE resolves by recency: if the same fact changed over time — a plan, a
preference, a decision — the later-dated statement supersedes the earlier one.
Say what changed and when.

A CONTRADICTION goes back to the user: if two excerpts conflict and recency
cannot settle it (both undated, same date, or a genuine disagreement of fact
rather than a change of mind), present BOTH, say plainly that the notes disagree,
and ask which is correct. Do not choose. Do not average.
"""

_NO_COMPUTATION_RULE = """
Do not compute. Never calculate, derive, or adjust a numeric value across
excerpts. If one note says "I have 2 dogs" and another says "I have a dog named
Rex", do NOT conclude there are 3. Report what the notes say. Arithmetic across
separate notes invents facts that appear in none of them.
"""

_ABSTENTION_RULE = """
A confident "I don't know" is always a correct answer. If the excerpts do not
contain the answer, say so and stop — do not reason toward a plausible answer
from adjacent material. Partial beats invented: "your notes cover the decision
but not the date" is a good answer.
"""

_UNDATED_RULE = """
An excerpt marked (undated) carries no date. Never place it in a sequence and
never treat it as recent. An excerpt whose date ends in ~ is INFERRED from when
the file changed, not stated in the note: usable for ordering, but say it is
approximate if the answer depends on it.
"""
```

Compose them into `SYSTEM_PROMPT` with an f-string, keeping the existing
persona, citation-format, and "Earlier, you asked" sections intact.

- [ ] **Step 4: Run, full suite, commit**

```bash
.venv/bin/pytest tests/test_prompts.py -q
.venv/bin/pytest -q
git add -A && git commit -m "IV.17: make the synthesis prompt reconcile, abstain, and refuse arithmetic"
```

---

### Task 12: Opt-in live-LLM behavioural fixtures

**Files:**
- Create: `tests/test_live_prompts.py`
- Modify: `pyproject.toml` (register the `live_llm` marker)

**Interfaces:**
- Consumes: `SYSTEM_PROMPT` (Task 11), the real Anthropic client.
- Produces: nothing consumed downstream.

- [ ] **Step 1: Register the marker**

In `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
    "live_llm: hits a real LLM provider. Deselected by default; needs a real key.",
]
addopts = "-m 'not live_llm'"
```

*(If `addopts` already exists, append `-m 'not live_llm'` to it rather than
replacing it.)*

- [ ] **Step 2: Write the fixtures**

```python
# tests/test_live_prompts.py
# SPDX-License-Identifier: Apache-2.0
"""Does the model actually comply with the prompt's procedures?

Deselected by default (`-m 'not live_llm'`) and absent from CI: these cost money
and are non-deterministic. Their value is not a green tick — it is that the cases
exist and can be re-run against a new model. A model upgrade is exactly when
prompt compliance changes silently, and this project has already been surprised
once by a model-behaviour change.

Run with: `.venv/bin/pytest tests/test_live_prompts.py -m live_llm -v`
"""

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.live_llm


def test_later_dated_statement_wins_and_the_audit_is_shown(live_answer):
    out = live_answer(
        "Where did I decide to move?",
        [("Moving Plans.md", datetime(2026, 6, 14, tzinfo=UTC), "Denver it is — signed the lease."),
         ("Journal.md", datetime(2026, 3, 2, tzinfo=UTC), "Portland feels right.")],
    )
    assert "Denver" in out
    assert "Reconciling" in out, "a competing pair must trigger the visible audit"


def test_a_single_excerpt_gets_no_audit(live_answer):
    out = live_answer(
        "What did I read in May?",
        [("Reading Log.md", datetime(2026, 5, 4, tzinfo=UTC), "Finished Piranesi.")],
    )
    assert "Piranesi" in out
    assert "Reconciling" not in out, "the conditional must actually stay off"


def test_undated_conflict_is_escalated_not_resolved(live_answer):
    out = live_answer(
        "What is my coffee order?",
        [("A.md", None, "I take it black."), ("B.md", None, "Flat white, always.")],
    )
    assert "black" in out.lower() and "flat white" in out.lower()


def test_does_not_do_arithmetic_across_notes(live_answer):
    out = live_answer(
        "How many dogs do I have?",
        [("A.md", None, "I have 2 dogs."), ("B.md", None, "I have a dog named Rex.")],
    )
    assert "3" not in out


def test_abstains_when_the_answer_is_absent(live_answer):
    out = live_answer(
        "What was my flight number?",
        [("Trip.md", datetime(2026, 4, 1, tzinfo=UTC), "Landed in Lisbon, took a taxi in.")],
    )
    assert any(p in out.lower() for p in ("don't", "do not", "not in", "no record", "cannot"))
```

Add a `live_answer` fixture to `tests/conftest.py` that builds real chunks,
calls the real client through the existing LLM wrapper, and returns the text.
Skip with a clear message when no API key resolves.

- [ ] **Step 3: Verify they are deselected by default, then run once manually**

```bash
.venv/bin/pytest -q                                  # count unchanged; live tests deselected
.venv/bin/pytest tests/test_live_prompts.py -m live_llm -v   # run once, by hand
```

Record the observed pass/fail in the commit message — including any case the
model fails, because a failing behavioural fixture is information, not a blocker.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "IV.17: opt-in live-LLM fixtures for prompt compliance"
```

- [ ] **Step 5: Mark §IV.17 shipped** in the research doc and add a Done entry to `PROJECT_MANAGEMENT.md`. Commit.

---

# Phase C — §IV.18 Cross-encoder reranking

Spec: `docs/superpowers/specs/2026-07-30-cross-encoder-multileaving-design.md`

### Task 13: Generalise the draft to n rankers

**Files:**
- Modify: `src/my_daemon/retrieval/interleave.py`
- Modify: `src/my_daemon/retrieval/orchestrator.py:122` (the one caller)
- Test: `tests/test_multileave.py` (create); existing `tests/test_interleave.py` must pass unchanged

**Interfaces:**
- Consumes: `RetrievedChunk` (unchanged).
- Produces: `multileave(rankings: Mapping[str, Sequence[RetrievedChunk]], *, rng: random.Random) -> list[RetrievedChunk]`, plus `SEED_TEAM`, `EXPANSION_TEAM`, and new `RERANK_TEAM = "rerank"`. `team_draft` is **removed**.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_multileave.py
# SPDX-License-Identifier: Apache-2.0
import random
from collections import Counter

from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.retrieval.interleave import multileave


def _rc(cid, score=0.5):
    return RetrievedChunk(
        chunk=Chunk(id=cid, note_uuid=cid, note_path=f"{cid}.md", text=cid, chunk_index=0),
        combined_score=score,
    )


def test_single_ranking_passes_through_in_order():
    out = multileave({"only": [_rc("a"), _rc("b")]}, rng=random.Random(0))
    assert [rc.chunk.id for rc in out] == ["a", "b"]
    assert {rc.team for rc in out} == {"only"}


def test_three_teams_draft_within_one_of_each_other():
    out = multileave(
        {"x": [_rc(f"x{i}") for i in range(5)],
         "y": [_rc(f"y{i}") for i in range(5)],
         "z": [_rc(f"z{i}") for i in range(5)]},
        rng=random.Random(1),
    )
    counts = Counter(rc.team for rc in out)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert len(out) == 15


def test_overlapping_teams_draft_each_candidate_once():
    """The rerank team contains the union of the others — this must not duplicate."""
    shared = [_rc("a"), _rc("b")]
    out = multileave({"src": shared, "rerank": list(reversed(shared))}, rng=random.Random(0))
    assert sorted(rc.chunk.id for rc in out) == ["a", "b"]


def test_tie_break_is_uniform_over_all_tied_teams():
    """A deterministic tie-break would hand one policy the first slot every time
    and reintroduce exactly the position bias the draft exists to cancel."""
    firsts = Counter()
    for seed in range(300):
        out = multileave(
            {"x": [_rc("x1")], "y": [_rc("y1")], "z": [_rc("z1")]},
            rng=random.Random(seed),
        )
        firsts[out[0].team] += 1
    assert len(firsts) == 3, f"only {sorted(firsts)} ever went first"
    assert min(firsts.values()) > 300 * 0.20, f"badly skewed: {firsts}"


def test_exhausted_teams_are_skipped_not_dispatched_to():
    out = multileave({"x": [_rc("x1")], "y": [_rc(f"y{i}") for i in range(4)]},
                     rng=random.Random(0))
    assert len(out) == 5


def test_empty_input_terminates():
    assert multileave({}, rng=random.Random(0)) == []
    assert multileave({"x": []}, rng=random.Random(0)) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_multileave.py -q`
Expected: FAIL — `ImportError: cannot import name 'multileave'`

- [ ] **Step 3: Implement `multileave`**

Keep the module docstring's existing Joachims/Radlinski paragraphs and add:

```
Extended 2026-07-30 to n rankings — team-draft *multileaving* (Schuth, Sietsma,
Whiteson, Lefortier & de Rijke, *Multileaved Comparisons for Fast Online
Evaluation*, CIKM 2014), which generalises the two-team draft while preserving
the per-team position symmetry that makes a pick attributable without a
propensity model. The third team is a cross-encoder's ordering of the whole pool
(§IV.18), so teams now *overlap* rather than partition the pool; the `seen` set
already handled that.
```

```python
def multileave(
    rankings: Mapping[str, Sequence[RetrievedChunk]],
    *,
    rng: random.Random,
) -> list[RetrievedChunk]:
    """Interleave n rankings by team draft, tagging each pick with its team."""

    queues = {team: list(items) for team, items in rankings.items()}
    cursors = dict.fromkeys(queues, 0)
    counts = dict.fromkeys(queues, 0)
    drafted: list[RetrievedChunk] = []
    seen: set[str] = set()

    def _exhausted(team: str) -> bool:
        return cursors[team] >= len(queues[team])

    def _draft(team: str) -> bool:
        moved = False
        while not _exhausted(team):
            candidate = queues[team][cursors[team]]
            cursors[team] += 1
            moved = True
            if candidate.chunk.id in seen:
                continue
            seen.add(candidate.chunk.id)
            drafted.append(candidate.model_copy(update={"team": team}))
            counts[team] += 1
            return True
        return moved

    while True:
        live = [t for t in queues if not _exhausted(t)]
        if not live:
            return drafted
        fewest = min(counts[t] for t in live)
        tied = [t for t in live if counts[t] == fewest]
        # Uniform over ALL tied teams, not the first of them. With two teams a
        # coin sufficed; with three, "first tied team wins" would quietly
        # privilege whichever key was inserted first.
        if not _draft(tied[0] if len(tied) == 1 else rng.choice(tied)):
            return drafted  # pragma: no cover — structural insurance
```

- [ ] **Step 4: Update the caller**

`orchestrator.py:122`:

```python
        return multileave(
            {
                SEED_TEAM: [rc for rc in by_score if rc.chunk.id in seed_ids],
                EXPANSION_TEAM: [rc for rc in by_score if rc.chunk.id not in seed_ids],
            },
            rng=self.rng,
        )
```

- [ ] **Step 5: Run both suites to verify no regression**

Run: `.venv/bin/pytest tests/test_multileave.py tests/test_interleave.py -q`
Expected: PASS — the existing two-team tests must be **unchanged**. If any fails,
the generalisation altered two-team behaviour and that is a bug, not a test to
edit.

- [ ] **Step 6: Commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add -A && git commit -m "IV.18: generalise the team draft to n rankers (multileaving)"
```

---

### Task 14: `CrossEncoderReranker`

**Files:**
- Create: `src/my_daemon/retrieval/rerank.py`
- Modify: `debug/license-compliance.md`
- Test: `tests/test_rerank.py` (create)

**Interfaces:**
- Consumes: `sentence_transformers.CrossEncoder` (already a declared dependency).
- Produces: `CrossEncoderReranker(model_name, cache_folder=None)` with `rank(query: str, candidates: Sequence[RetrievedChunk]) -> list[float]`, `download() -> Path`, and module function `build_pair_text(chunk: Chunk) -> str`.

- [ ] **Step 1: Verify the model licence — a blocking gate (CLAUDE.md rule 10)**

```bash
.venv/bin/python - <<'PY'
from huggingface_hub import model_info
i = model_info("cross-encoder/ms-marco-MiniLM-L-6-v2")
print("license:", (i.card_data or {}).get("license"))
print("tags:", i.tags)
PY
```

Record the finding **with its source** in `debug/license-compliance.md` under a
new "Model weights" section, noting that `scripts/license_check.py` covers Python
distributions and not model artifacts.

**If it is not Apache-2.0-compatible, stop.** Surface the tradeoff rather than
silently accepting a weaker model; check `BAAI/bge-reranker-base`, then the
`flashrank` ONNX family, and report which are permissive before choosing.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_rerank.py
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.retrieval.rerank import CrossEncoderReranker, build_pair_text


def _rc(text="body", **kw):
    d = dict(id="c", note_uuid="u", note_path="Journal.md", text=text, chunk_index=0)
    return RetrievedChunk(chunk=Chunk(**{**d, **kw}))


def test_pair_text_includes_path_heading_and_date():
    t = build_pair_text(
        Chunk(id="c", note_uuid="u", note_path="Journal.md", heading_path=["March", "2nd"],
              text="Portland feels right.", chunk_index=0,
              occurred_at=datetime(2026, 3, 2, tzinfo=UTC))
    )
    assert "Journal.md" in t and "March" in t and "2026-03-02" in t
    assert "Portland feels right." in t


def test_undated_pair_text_omits_the_date_without_breaking():
    t = build_pair_text(Chunk(id="c", note_uuid="u", note_path="a.md", text="x", chunk_index=0))
    assert "a.md" in t and "x" in t


def test_scores_already_in_unit_range_pass_through_unchanged():
    """A calibrated model scoring a top candidate 0.007 MEANS it. Sigmoiding
    unconditionally maps everything to ~0.5 and destroys that."""
    r = CrossEncoderReranker("stub", _model=_StubModel([0.007, 0.98]))
    assert r.rank("q", [_rc("a"), _rc("b")]) == [0.007, 0.98]


def test_raw_logits_are_sigmoided():
    r = CrossEncoderReranker("stub", _model=_StubModel([-4.0, 6.0]))
    out = r.rank("q", [_rc("a"), _rc("b")])
    assert all(0.0 < s < 1.0 for s in out)
    assert out[0] < 0.05 and out[1] > 0.95


def test_returns_one_score_per_candidate_in_order():
    r = CrossEncoderReranker("stub", _model=_StubModel([0.1, 0.2, 0.3]))
    assert r.rank("q", [_rc("a"), _rc("b"), _rc("c")]) == [0.1, 0.2, 0.3]


def test_empty_candidates_does_not_load_the_model():
    r = CrossEncoderReranker("does-not-exist")
    assert r.rank("q", []) == []          # must not raise or download


class _StubModel:
    def __init__(self, scores):
        self.scores = scores
        self.seen_pairs = None

    def predict(self, pairs, **_):
        self.seen_pairs = list(pairs)
        return self.scores
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_rerank.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 4: Implement**

```python
# SPDX-License-Identifier: Apache-2.0
"""Cross-encoder reranking: score (query, chunk) jointly rather than by cosine.

A bi-encoder embeds query and document independently, so relevance is whatever
survives compression into a single vector. A cross-encoder reads both together
and scores the pair, which is materially better at "is this passage actually
about this question" and materially slower — hence its use as a *reranker* over
a small candidate pool rather than as a retriever.

This unit scores and nothing else. It does not sort, does not know about config,
and does not know how its ordering will be used. That is what lets the
orchestrator tests run against a stub with no model download.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from my_daemon.models import Chunk, RetrievedChunk


def build_pair_text(chunk: Chunk) -> str:
    """The document side of the pair: context first, then the text.

    Heading path and date are included because they are real relevance signal a
    bare body omits — "March › 2nd" and a date help the model judge a temporal
    question that the prose alone leaves ambiguous.
    """

    heading = " › ".join(chunk.heading_path) if chunk.heading_path else ""
    stamp = f"  [{chunk.occurred_at.date().isoformat()}]" if chunk.occurred_at else ""
    header = f"{chunk.note_path}{' › ' + heading if heading else ''}{stamp}"
    return f"{header}\n{chunk.text.strip()}"


def _to_unit(score: float) -> float:
    return 1.0 / (1.0 + math.exp(-score))


class CrossEncoderReranker:
    def __init__(
        self,
        model_name: str,
        cache_folder: Path | None = None,
        *,
        _model: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_folder = cache_folder
        self._loaded = _model

    def _model_(self) -> Any:
        if self._loaded is None:
            from sentence_transformers import CrossEncoder

            kwargs: dict[str, Any] = {}
            if self.cache_folder is not None:
                kwargs["cache_folder"] = str(self.cache_folder)
            self._loaded = CrossEncoder(self.model_name, **kwargs)
        return self._loaded

    def rank(self, query: str, candidates: Sequence[RetrievedChunk]) -> list[float]:
        """One relevance score per candidate, in the order given.

        Scores already inside [0, 1] are returned **unchanged**. Some rerankers
        emit calibrated probabilities and some emit raw logits; sigmoiding
        unconditionally would squash a calibrated 0.007 to ~0.5 and throw away
        the model's own confidence.
        """

        if not candidates:
            return []
        pairs = [(query, build_pair_text(rc.chunk)) for rc in candidates]
        raw = [float(s) for s in self._model_().predict(pairs)]
        if all(0.0 <= s <= 1.0 for s in raw):
            return raw
        return [_to_unit(s) for s in raw]

    def download(self) -> Path:
        """Force the weights into the cache so later runs stay offline."""
        self._model_()
        return self.cache_folder or Path.home() / ".cache" / "huggingface"
```

- [ ] **Step 5: Run, commit**

```bash
.venv/bin/pytest tests/test_rerank.py -q
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
.venv/bin/pytest -q
git add -A && git commit -m "IV.18: CrossEncoderReranker — scores only, calibration preserved"
```

---

### Task 15: Wire the third team, and make the switch discoverable

**Files:**
- Modify: `src/my_daemon/config.py` (`RetrievalConfig`), `config.yaml`, `config.example.yaml`
- Modify: `src/my_daemon/retrieval/orchestrator.py`
- Modify: `src/my_daemon/integration/wiring.py`, `src/my_daemon/cli.py` (`models download`), `src/my_daemon/doctor.py`
- Modify: `docs-source/configuration.md`, `docs-source/cli.md`
- Test: `tests/test_orchestrator.py`, `tests/test_doctor.py` (append)

**Interfaces:**
- Consumes: `multileave`, `RERANK_TEAM` (Task 13); `CrossEncoderReranker` (Task 14).
- Produces: `RetrievalOrchestrator(..., reranker: CrossEncoderReranker | None = None)`; config `retrieval.rerank`, `retrieval.rerank_model`, `retrieval.rerank_max_candidates`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_orchestrator.py
def test_no_reranker_reproduces_two_team_behaviour(orchestrator_for):
    """Byte-for-byte, so enabling the feature is the only thing that changes."""
    a = orchestrator_for(reranker=None, rng=random.Random(7)).retrieve("q")
    b = orchestrator_for(reranker=None, rng=random.Random(7)).retrieve("q")
    assert [rc.chunk.id for rc in a.ranked] == [rc.chunk.id for rc in b.ranked]
    assert {rc.team for rc in a.ranked} <= {"seed", "expansion"}


def test_reranker_present_but_disabled_changes_nothing(orchestrator_for):
    plain = orchestrator_for(reranker=None, rng=random.Random(7)).retrieve("q")
    off = orchestrator_for(
        reranker=_StubReranker(), rerank=False, rng=random.Random(7)
    ).retrieve("q")
    assert [rc.chunk.id for rc in plain.ranked] == [rc.chunk.id for rc in off.ranked]


def test_enabled_reranker_adds_a_third_team(orchestrator_for):
    result = orchestrator_for(reranker=_StubReranker(), rerank=True).retrieve("q")
    assert "rerank" in {rc.team for rc in result.ranked}


def test_rerank_does_not_overwrite_combined_score(orchestrator_for):
    """combined_score is persisted and feeds the activation ledger's rank
    strength; overwriting it would silently change what fingerprints mean."""
    stub = _StubReranker(constant=0.99)
    result = orchestrator_for(reranker=stub, rerank=True).retrieve("q")
    assert all(rc.combined_score != 0.99 for rc in result.ranked)


def test_rerank_respects_the_candidate_cap(orchestrator_for):
    stub = _StubReranker()
    orchestrator_for(reranker=stub, rerank=True, rerank_max_candidates=2).retrieve("q")
    assert stub.last_n <= 2
```

```python
# append to tests/test_doctor.py
def test_doctor_flags_an_available_but_disabled_reranker(settings_for):
    from my_daemon.doctor import run_checks

    names = {c.name: c for c in run_checks(settings_for(rerank=False))}
    assert "reranking" in names
    assert "daemon policy" in names["reranking"].hint
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_orchestrator.py -k rerank tests/test_doctor.py -k rerank -q`
Expected: FAIL — `RetrievalOrchestrator.__init__` has no `reranker`.

- [ ] **Step 3: Add the config**

```python
    # Rerank the candidate pool with a cross-encoder and enter that ordering as
    # a third team in the draft (§IV.18), so `daemon policy` reports whether it
    # actually earns picks on this vault.
    #
    # Off by default only because enabling it without the weights cached would
    # download on first query and break the offline guarantee the embedder works
    # to provide. `daemon doctor` surfaces the switch so off-by-default does not
    # mean invisible; run `daemon models download` first.
    rerank: bool = False
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Bounds per-query latency. The cross-encoder is O(candidates), unlike the
    # bi-encoder retrieval it reorders.
    rerank_max_candidates: int = 100
```

Mirror into both yaml files, commented out with a pointer to `daemon models download`.

- [ ] **Step 4: Wire the orchestrator**

Add `reranker: CrossEncoderReranker | None = None` to `__init__` (last parameter,
defaulted). Rewrite the tail of `_pool`:

```python
        by_score = sorted(best.values(), key=lambda r: r.combined_score, reverse=True)
        if not self.s.retrieval.interleave:
            return by_score

        seed_ids = {s.chunk.id for s in seeds}
        rankings: dict[str, list[RetrievedChunk]] = {
            SEED_TEAM: [rc for rc in by_score if rc.chunk.id in seed_ids],
            EXPANSION_TEAM: [rc for rc in by_score if rc.chunk.id not in seed_ids],
        }

        if self.reranker is not None and self.s.retrieval.rerank:
            capped = by_score[: self.s.retrieval.rerank_max_candidates]
            t0 = time.perf_counter()
            scores = self.reranker.rank(query, capped)
            # Reported separately so the cost stays visible instead of being
            # folded into a total nobody can attribute.
            self._rerank_ms = int((time.perf_counter() - t0) * 1000)
            # Sorted into a new list; `combined_score` is deliberately untouched.
            rankings[RERANK_TEAM] = [
                rc for _, rc in sorted(zip(scores, capped, strict=True), key=lambda p: -p[0])
            ]

        return multileave(rankings, rng=self.rng)
```

`_pool` needs the query — change its signature to `_pool(self, query, seeds, expanded)`
and update the single call in `retrieve`.

- [ ] **Step 5: Build it in wiring, download it, surface it in doctor**

- `integration/wiring.py`: construct the reranker when `settings.retrieval.rerank`
  is true, passing the same cache folder the embedder uses; otherwise `None`.
- `cli.py` `models_download`: pull the reranker when `s.retrieval.rerank`,
  mirroring the existing sparse-model branch.
- `doctor.py`: a `reranking` check — pass when enabled and cached, and when
  disabled report *"available but disabled — set `retrieval.rerank: true` and run
  `daemon models download` to A/B it via `daemon policy`."*
- `cli.py` `query -v`: print `rerank {n}ms` when a rerank ran.
- `daemon policy` output: print the number of teams currently drafting, with a
  note that win rates gathered under a different team count are not directly
  comparable.

- [ ] **Step 6: Run everything, measure latency, commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/mypy src
```

Then measure for real and record the number in the commit message:

```bash
.venv/bin/python -c "
import yaml,pathlib
p=pathlib.Path('config.yaml'); c=yaml.safe_load(p.read_text())
c.setdefault('retrieval',{})['rerank']=True; p.write_text(yaml.safe_dump(c))
"
.venv/bin/daemon models download
.venv/bin/daemon query "what was I thinking about last week" -v      # note the rerank ms
```

```bash
git add -A && git commit -m "IV.18: enter the reranked ordering as a third drafting team"
```

- [ ] **Step 7: Mark §IV.18 shipped** in the research doc, add a Done entry to `PROJECT_MANAGEMENT.md` including the measured latency and the note that the two-team policy history is no longer directly comparable. Commit.

---

# Phase D — Deferred, and deliberately not planned in detail

These are the remaining items from the comparison. Each is already a checkboxed
entry in `MY-DAEMON-RESEARCH-APPLIED.md` so it cannot be forgotten. **No
bite-sized steps are written here on purpose** — none has a spec yet, and
inventing TDD steps for undesigned work produces plans that read as authoritative
and are wrong.

- [ ] **§IV.20 — Activation stats into ranking.** S. The cheap down payment on
  the salience gap: `note_activation_stats` is already populated and
  `daemon hot-notes` already reads it; nothing feeds it into ranking. Keep the
  term bounded (Hindsight's ±5% is the right order) and measure it through
  §IV.18's draft rather than asserting it. **Next after Phase C.**
- [ ] **§IV.19 — Durable supersession.** M. Depends on §IV.10. Move-don't-flag
  archive worth copying from Hindsight (MIT). Needs a spec: brainstorm first.
- [ ] **§IV.22 — Chunk-level delta re-ingestion.** M. Low value now (no LLM in
  the ingest path), mandatory immediately before §IV.21. Complication: the
  50-token chunk overlap means a one-paragraph edit dirties neighbours.
- [ ] **§IV.23 — Retrieval-quality evaluation harness.** M. Vault-local and
  honest; deliberately not LoCoMo/LongMemEval — see the item for why their
  published numbers do not survive inspection.
- [ ] **§IV.21 — Claim-level indexing over chunks.** L. **The one architectural
  call.** Read its research entry before starting and expect to brainstorm — the
  second-source-of-truth tension must be resolved in design, not during
  implementation. Blocked on §IV.22.

---

## Self-Review

**Spec coverage.** Walked all three specs:

- *IV.10:* section 1 → Tasks 1–4; amendment 1 → Tasks 1–2; migration/registry →
  Task 5; `DateRange` → Task 6; both-arms filter, occurred_at-only, coverage
  reporting → Task 7; backfill, refresh path, skip comment → Task 8; CLI/Hermes →
  Task 9; amendment 2 → Task 10; amendment 3 → Task 6's
  `test_open_ended_on_either_side_is_allowed` plus the `DateRange` docstring
  (that is all that *can* land here — the NL step is deferred by locked decision 1).
- *IV.17:* five rules → Task 11; three test layers → Tasks 11–12.
- *IV.18:* multileave → Task 13; reranker + licence gate → Task 14; wiring,
  config, doctor, download, latency, policy note → Task 15.

**Gap found and closed:** the spec's requirement that `daemon policy` report the
team count was initially missing; added to Task 15 Step 5.

**Placeholder scan.** No TBD/TODO. Every code step carries real code. Three
places deliberately say "adapt to the existing helper" rather than inventing a
signature — Task 7's graph fixtures, Task 8's `settings_for`, Task 15's
`orchestrator_for` — because those helpers already exist in the test suite and
guessing their shape would be worse than pointing at them.

**Type consistency.** `derive_occurred_at` returns `tuple[datetime | None, str | None]`
in Tasks 1, 3, and 8. `occurred_at_source` is spelled identically in `Note`,
`Chunk`, `NoteRecord`, the payload, and `_date_marker`. `date_range` is the
parameter name in `search`, `hybrid_search`, `seed_search`, `expand_from_seeds`,
and `retrieve`. `multileave` takes `Mapping[str, Sequence[RetrievedChunk]]` in
Tasks 13 and 15. `rank(query, candidates) -> list[float]` in Tasks 14 and 15.

**One risk the plan cannot remove.** Task 12's live-LLM fixtures may fail on
first run — the model may skip the conditional audit or comply inconsistently.
That is a finding to record, not a blocker, and the plan says so explicitly at
Task 12 Step 3. If compliance is poor, the honest response is to iterate on the
rule wording with the fixtures as the measure, not to weaken the test.
