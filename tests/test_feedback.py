from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from my_daemon.models import FeedbackEvent
from my_daemon.stores import FeedbackStore


def test_feedback_log_and_attach_signal(tmp_path: Path):
    store = FeedbackStore(db_path=tmp_path / "feedback.db")
    event = FeedbackEvent(
        timestamp=datetime.now(UTC),
        query="what is a daemon",
        retrieval_summary={"ranked": []},
        answer="A companion.",
        latency_ms=42,
    )
    event_id = store.log(event)
    assert event_id > 0

    store.attach_signal(event_id, "explicit_up")
    rows = store.recent()
    assert rows[0]["signal"] == "explicit_up"
    assert rows[0]["query"] == "what is a daemon"
