# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

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
