# SPDX-License-Identifier: Apache-2.0
"""Derive a note's *episodic* date — when the thing happened, not when the file changed.

Three sources, in order: an explicit frontmatter key, a `YYYY-MM-DD` filename
prefix, and — only when a trusted cutoff is configured — mtime.

Two rules carry most of the weight, and both are refusals:

*A partial date yields nothing.* `date: 2024-09` is not month-precision data, it
is an absent day. Accepting it would force a granularity flag through every
downstream scorer, and the alternative that competitors ship is worse: store
`2024-01-01 .. 2024-12-31` and a proximity scorer places it at the midpoint, so a
year-granularity note behaves as though it happened on 2 July. No date this
module returns is ever *coarser* than a day: a partial value like `2024-09` is
rejected rather than widened into a range. A frontmatter timestamp may be finer
than a day, which is harmless — it is the coarse case that would have forced a
precision flag through every downstream scorer.

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
        # Normalised like the other two branches: the docstring's
        # day-precision invariant has to hold for every source, and a real
        # mtime carries a time-of-day that would otherwise leak through.
        return _to_utc_midnight(mtime.date()), "mtime"

    return None, None
