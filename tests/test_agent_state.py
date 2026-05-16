"""Round-trip tests for stores/agent_state.py."""

from __future__ import annotations

from pathlib import Path

from my_daemon.stores import AgentStateStore


def test_extract_run_roundtrip(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "feedback.db")
    state.record_extract_run("notes/a.md", note_mtime=1234.5, summary_hash="deadbeef")
    row = state.extract_run_for("notes/a.md")
    assert row is not None
    assert row["note_path"] == "notes/a.md"
    assert row["note_mtime_seen"] == 1234.5
    assert row["summary_hash"] == "deadbeef"


def test_extract_run_upsert_overwrites(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "feedback.db")
    state.record_extract_run("a.md", note_mtime=1.0, summary_hash="aaa")
    state.record_extract_run("a.md", note_mtime=2.0, summary_hash="bbb")
    row = state.extract_run_for("a.md")
    assert row["summary_hash"] == "bbb"
    assert row["note_mtime_seen"] == 2.0


def test_link_and_reflect_runs(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "feedback.db")
    state.record_link_run("x.md", note_mtime=100.0, applied=3, suggested=7)
    state.record_reflect_run("personality", source_notes_hash="cafe", source_chat_count=12)
    assert state.link_run_for("x.md")["applied_count"] == 3
    assert state.link_run_for("x.md")["suggested_count"] == 7
    assert state.reflect_run_for("personality")["source_chat_count"] == 12
    assert state.reflect_run_for("nonexistent") is None


def test_coexists_with_feedback_db(tmp_path: Path) -> None:
    """Schema additivity: dropping AgentStateStore on a fresh feedback.db shouldn't error,
    and the existing FeedbackStore tables stay intact."""
    from datetime import UTC, datetime

    from my_daemon.models import FeedbackEvent
    from my_daemon.stores import FeedbackStore

    db = tmp_path / "feedback.db"
    fb = FeedbackStore(db_path=db)
    fb.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="hello",
            retrieval_summary={"k": "v"},
            answer="hi",
            latency_ms=42,
        )
    )
    # Now overlay agent state — must not collide.
    AgentStateStore(db).record_extract_run("a.md", note_mtime=1.0, summary_hash="x")
    assert len(fb.recent()) == 1
