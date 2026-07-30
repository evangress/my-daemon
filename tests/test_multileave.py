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
        {
            "x": [_rc(f"x{i}") for i in range(5)],
            "y": [_rc(f"y{i}") for i in range(5)],
            "z": [_rc(f"z{i}") for i in range(5)],
        },
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
    out = multileave(
        {"x": [_rc("x1")], "y": [_rc(f"y{i}") for i in range(4)]},
        rng=random.Random(0),
    )
    assert len(out) == 5


def test_empty_input_terminates():
    assert multileave({}, rng=random.Random(0)) == []
    assert multileave({"x": []}, rng=random.Random(0)) == []
