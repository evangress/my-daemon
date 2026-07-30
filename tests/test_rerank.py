# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest

from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.retrieval.rerank import CrossEncoderReranker, build_pair_text


def _rc(text="body", **kw):
    d = dict(id="c", note_uuid="u", note_path="Journal.md", text=text, chunk_index=0)
    return RetrievedChunk(chunk=Chunk(**{**d, **kw}))


def test_pair_text_includes_path_heading_and_date():
    t = build_pair_text(
        Chunk(
            id="c",
            note_uuid="u",
            note_path="Journal.md",
            heading_path=["March", "2nd"],
            text="Portland feels right.",
            chunk_index=0,
            occurred_at=datetime(2026, 3, 2, tzinfo=UTC),
        )
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


def test_extreme_logits_do_not_overflow():
    """A cross-encoder emits large negative logits for confidently irrelevant
    pairs; the naive sigmoid raises OverflowError below about -746."""
    r = CrossEncoderReranker("stub", _model=_StubModel([-800.0, 800.0]))
    out = r.rank("q", [_rc("a"), _rc("b")])
    assert out[0] == pytest.approx(0.0, abs=1e-9)
    assert out[1] == pytest.approx(1.0, abs=1e-9)
    assert all(0.0 <= s <= 1.0 for s in out)


def test_returns_one_score_per_candidate_in_order():
    r = CrossEncoderReranker("stub", _model=_StubModel([0.1, 0.2, 0.3]))
    assert r.rank("q", [_rc("a"), _rc("b"), _rc("c")]) == [0.1, 0.2, 0.3]


def test_empty_candidates_does_not_load_the_model():
    r = CrossEncoderReranker("does-not-exist")
    assert r.rank("q", []) == []  # must not raise or download


class _StubModel:
    def __init__(self, scores):
        self.scores = scores
        self.seen_pairs = None

    def predict(self, pairs, **_):
        self.seen_pairs = list(pairs)
        return self.scores
