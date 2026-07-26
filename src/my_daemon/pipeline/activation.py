# SPDX-License-Identifier: Apache-2.0
"""The listener that turns a retrieval into ledger rows."""

from __future__ import annotations

from my_daemon.retrieval.trace import RetrievalTrace
from my_daemon.stores.activations import ActivationLedger


class ActivationRecorder:
    """Persists every retrieval as a query + its activations.

    Deliberately the *only* thing between the orchestrator and the ledger, so
    the orchestrator stays ignorant of persistence.
    """

    def __init__(self, ledger: ActivationLedger) -> None:
        self.ledger = ledger

    def on_retrieval(self, trace: RetrievalTrace) -> str | None:
        if not trace.activations:
            return None
        import uuid as uuidlib

        query_uid = str(uuidlib.uuid4())
        self.ledger.record(
            query_uid=query_uid,
            text=trace.query,
            surface=trace.surface,
            ts=trace.started_at,
            activations=trace.activations,
            session_id=trace.session_id,
            seed_count=len(trace.result.seeds),
            expanded_count=len(trace.result.expanded),
            latency_ms=trace.latency_ms,
        )
        return query_uid
