# SPDX-License-Identifier: Apache-2.0
"""The post-retrieval seam.

``RetrievalOrchestrator.retrieve()`` is the only point every surface passes
through — CLI, GUI, ``QueryEngine``, ``DaemonCore``, and all five Hermes hooks.
That makes it the right place to observe retrieval, and the wrong place to *do*
persistence: handing it a database would turn it into a god object and break
every test that constructs it.

So it emits a trace to registered listeners instead. The orchestrator's total
new knowledge is one Protocol; it never imports sqlite, the ledger, or the
registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from my_daemon.models import RetrievalResult
from my_daemon.stores.activations import Activation


@dataclass
class RetrievalTrace:
    query: str
    surface: str
    started_at: datetime
    latency_ms: int
    result: RetrievalResult
    activations: list[Activation] = field(default_factory=list)
    session_id: str | None = None


@runtime_checkable
class RetrievalListener(Protocol):
    def on_retrieval(self, trace: RetrievalTrace) -> str | None:
        """Observe a completed retrieval. Returns a query_uid, or None.

        **Must not raise.** The orchestrator guards the call anyway — a ledger
        failure degrades the daemon to exactly its previous behaviour rather
        than breaking the user's query — but a listener that throws is a bug.
        """
        ...


def activations_from(result: RetrievalResult) -> list[Activation]:
    """Derive the activation rows from a finished retrieval.

    Everything needed is already on the result: seeds carry their vector score,
    expanded chunks carry graph distance and the seed they came from, and the
    ranked order supplies position. No extra I/O.
    """

    seed_uuids = {s.chunk.id: s.chunk.note_uuid for s in result.seeds}
    seen: set[tuple[str, str]] = set()
    out: list[Activation] = []

    for rank, rc in enumerate(result.ranked, start=1):
        note_uuid = rc.chunk.note_uuid
        if not note_uuid:
            continue
        source = "vector_seed" if rc.graph_distance in (0, None) else "graph_expansion"
        key = (note_uuid, source)
        if key in seen:
            continue  # keep the best-ranked appearance of each (note, source)
        seen.add(key)
        out.append(
            Activation(
                note_uuid=note_uuid,
                source=source,  # type: ignore[arg-type]
                rank=rank,
                raw_score=rc.vector_score if rc.vector_score is not None else rc.combined_score,
                chunk_id=rc.chunk.id,
                graph_distance=rc.graph_distance,
                seed_note_uuid=seed_uuids.get(rc.seed_chunk_id or rc.chunk.id),
            )
        )
    return out
