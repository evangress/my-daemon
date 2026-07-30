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


def test_a_normal_range_is_active_and_not_empty():
    r = DateRange(
        since=datetime(2026, 3, 1, tzinfo=UTC),
        until=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert r.is_active is True
    assert r.is_empty is False


def test_the_model_is_frozen():
    """Callers pass this into retrieval; it must not be mutable en route."""
    import pydantic

    r = DateRange(since=datetime(2026, 3, 1, tzinfo=UTC))
    with pytest.raises(pydantic.ValidationError):
        r.since = datetime(2026, 4, 1, tzinfo=UTC)
