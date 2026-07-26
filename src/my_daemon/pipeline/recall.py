# SPDX-License-Identifier: Apache-2.0
"""Fingerprint recall — "you've been here before".

The point of the whole activation ledger. A question worded nothing like an
earlier one, but which lights up the same notes, is the same underlying
concern — and cosine over activation fingerprints finds that where embedding
the query text would not.

What comes back is deliberately rendered in *paths*, not uuids: it goes into an
LLM prompt and onto the user's screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from my_daemon.config import Settings
from my_daemon.stores.activations import INTENTIONAL_SURFACES, ActivationLedger
from my_daemon.stores.registry import NoteRegistry


@dataclass
class RecalledMemory:
    query_id: int
    text: str
    ts: datetime
    score: float
    shared_notes: list[str] = field(default_factory=list)


def recall_related(
    ledger: ActivationLedger,
    registry: NoteRegistry,
    *,
    query_id: int,
    settings: Settings,
    max_shared: int = 4,
) -> list[RecalledMemory]:
    """Past queries whose activation pattern resembles this one's."""

    memory = settings.memory
    if not memory.recall_enabled:
        return []

    probe = ledger.fingerprint(query_id)
    if not probe:
        return []

    since = (
        datetime.now(UTC) - timedelta(days=memory.lookback_days) if memory.lookback_days else None
    )
    hits = ledger.similar(
        probe,
        top_k=memory.top_k,
        since=since,
        min_score=memory.min_score,
        max_df_ratio=memory.max_df_ratio,
        exclude_query_id=query_id,
        # Ambient prefetches aren't questions the user asked, so they are not
        # things to remind the user they once asked.
        surfaces=INTENTIONAL_SURFACES,
    )
    if not hits:
        return []

    # Resolve identities to paths in one round trip. `paths_for` deliberately
    # includes soft-deleted rows — the ledger outlives the note, and a memory
    # of a note you since deleted should still be readable.
    wanted = {u for h in hits for u in h.shared_notes[:max_shared]}
    paths = registry.paths_for(wanted)

    return [
        RecalledMemory(
            query_id=h.query_id,
            text=h.text,
            ts=h.ts,
            score=h.score,
            shared_notes=[paths.get(u, u) for u in h.shared_notes[:max_shared]],
        )
        for h in hits
    ]
