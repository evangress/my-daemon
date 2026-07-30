# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

from my_daemon.llm import prompts
from my_daemon.llm.prompts import build_context_block
from my_daemon.models import Chunk, RetrievedChunk


def _rc(**kw):
    defaults = dict(id="c", note_uuid="u", note_path="n.md", text="body", chunk_index=0)
    return RetrievedChunk(chunk=Chunk(**{**defaults, **kw}), vector_score=0.8, combined_score=0.4)


def test_dated_chunk_renders_an_iso_date():
    out = build_context_block(
        [_rc(occurred_at=datetime(2026, 3, 31, tzinfo=UTC), occurred_at_source="frontmatter")]
    )
    assert "[2026-03-31]" in out


def test_inferred_date_is_marked_approximate():
    out = build_context_block(
        [_rc(occurred_at=datetime(2026, 2, 21, tzinfo=UTC), occurred_at_source="mtime")]
    )
    assert "[2026-02-21~]" in out


def test_undated_chunk_says_so_explicitly():
    """The model must distinguish 'no date' from 'I wasn't told'."""
    assert "(undated)" in build_context_block([_rc()])


def test_internal_retrieval_scores_are_not_shown():
    out = build_context_block([_rc(occurred_at=datetime(2026, 3, 31, tzinfo=UTC))])
    assert "score=" not in out and "vector=" not in out and "graph_distance" not in out


def test_filename_sourced_date_renders_without_tilde():
    """Frontmatter and filename dates are both stated by the user; only mtime is a guess."""
    out = build_context_block(
        [_rc(occurred_at=datetime(2026, 5, 1, tzinfo=UTC), occurred_at_source="filename")]
    )
    assert "[2026-05-01]" in out
    assert "[2026-05-01~]" not in out


def test_all_three_date_markers_are_distinguishable_in_one_block():
    """The model sees these side by side; stated, inferred, and absent dates must
    not be confusable, because reconciliation ordering depends on the difference."""
    out = build_context_block(
        [
            _rc(
                note_path="stated.md",
                occurred_at=datetime(2026, 3, 31, tzinfo=UTC),
                occurred_at_source="frontmatter",
            ),
            _rc(
                note_path="inferred.md",
                occurred_at=datetime(2026, 2, 21, tzinfo=UTC),
                occurred_at_source="mtime",
            ),
            _rc(note_path="absent.md"),
        ]
    )
    assert "[2026-03-31]" in out
    assert "[2026-02-21~]" in out
    assert "(undated)" in out
    # and the tilde must not leak onto the stated one
    assert "[2026-03-31~]" not in out


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


def test_the_prompt_tells_the_model_when_NOT_to_emit_the_audit():
    """The audit is conditional by design — always-on ceremony trains the user to
    skip it, which destroys its value exactly when it matters. So the prompt must
    state the negative case, not merely describe the positive one."""
    rule = prompts._RECONCILIATION_RULE.lower()
    assert "do not emit" in rule or "if no excerpts compete" in rule
    # And the composed prompt must carry that negative instruction too.
    assert "do not emit" in prompts.SYSTEM_PROMPT.lower() or "if no excerpts compete" in (
        prompts.SYSTEM_PROMPT.lower()
    )


def test_assembled_prompt_carries_date_markers_and_omits_scores():
    """Pins the seam between the context block and the prompt.

    The brief's version of this test references a `snapshot_fixture` parameter
    that does not exist in this repo's test setup, so this is written as a plain
    assertion-based test over build_user_message's output instead."""
    msg = prompts.build_user_message(
        "where did I decide to move?",
        [
            _rc(
                occurred_at=datetime(2026, 6, 14, tzinfo=UTC),
                note_path="Moving Plans.md",
                text="Denver it is.",
            ),
            _rc(
                occurred_at=datetime(2026, 3, 2, tzinfo=UTC),
                note_path="Journal.md",
                text="Portland feels right.",
            ),
            _rc(note_path="Old Notes.md", text="no date here"),
        ],
    )
    assert "[2026-06-14]" in msg and "[2026-03-02]" in msg and "(undated)" in msg
    assert "score=" not in msg


def test_the_general_conflict_bullet_defers_to_the_specific_rules():
    """Two instructions for one trigger is worse than one. The general bullet
    must route the model to the specific update-vs-contradiction rule rather
    than offer a second, vaguer answer."""
    prompt = prompts.SYSTEM_PROMPT
    # The old standalone phrasing must be gone.
    assert "surface the conflict instead of papering over it" not in prompt
    # And the general mention must point at the specific rules.
    assert "rules below" in prompt or "update-vs-contradiction" in prompt.lower()
