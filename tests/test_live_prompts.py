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
        [
            (
                "Moving Plans.md",
                datetime(2026, 6, 14, tzinfo=UTC),
                "Denver it is — signed the lease.",
            ),
            ("Journal.md", datetime(2026, 3, 2, tzinfo=UTC), "Portland feels right."),
        ],
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
