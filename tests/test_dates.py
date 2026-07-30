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
        "2024-09-02",  # PyYAML str (quoted)
        date(2024, 9, 2),  # PyYAML date (bare)
        datetime(2024, 9, 2, 10, 30, tzinfo=UTC),  # PyYAML tz-aware datetime
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
