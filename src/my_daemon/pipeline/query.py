# SPDX-License-Identifier: Apache-2.0
"""Query pipeline: retrieve, synthesize, log."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.llm import LLMClient
from my_daemon.models import DateRange, FeedbackEvent, RetrievalResult, is_seed_distance
from my_daemon.pipeline.activation import ActivationRecorder
from my_daemon.pipeline.recall import RecalledMemory, recall_related
from my_daemon.retrieval import RetrievalOrchestrator
from my_daemon.retrieval.rerank import CrossEncoderReranker
from my_daemon.stores import FeedbackStore, GraphStore, VectorStore
from my_daemon.stores.activations import ActivationLedger
from my_daemon.stores.registry import NoteRegistry

log = logging.getLogger(__name__)


@dataclass
class QueryResponse:
    answer: str
    retrieval: RetrievalResult
    feedback_event_id: int
    latency_ms: int
    # Past questions that lit up the same notes. Populated whenever recall is
    # enabled, regardless of whether they were injected into the prompt —
    # `memory.show_to_user` decides whether a surface renders them.
    memories: list[RecalledMemory] = field(default_factory=list)


def build_retrieval_summary(result: RetrievalResult) -> dict:
    """Persisted JSON shape for a query's ranked candidates.

    Stored in ``feedback.retrieval_summary`` so the later ``daemon select``
    command can map a candidate rank back to the chunk and note that produced
    it — plus the seed it expanded from, which is the start point for the
    graph-path reinforcement in ``retrieval.weights.apply_selection``.
    """

    seed_note_by_chunk: dict[str, str] = {s.chunk.id: s.chunk.note_path for s in result.seeds}
    seed_uuid_by_chunk: dict[str, str] = {s.chunk.id: s.chunk.note_uuid for s in result.seeds}

    ranked: list[dict] = []
    for rc in result.ranked:
        seed_chunk_id = rc.seed_chunk_id or rc.chunk.id
        # A seed distance means the chunk *is* the seed; otherwise it was
        # pulled in via expansion (at a possibly fractional distance) and the
        # seed lookup gives us the start node.
        if is_seed_distance(rc.graph_distance):
            seed_note = rc.chunk.note_path
            seed_uuid = rc.chunk.note_uuid
        else:
            seed_note = seed_note_by_chunk.get(seed_chunk_id, rc.chunk.note_path)
            seed_uuid = seed_uuid_by_chunk.get(seed_chunk_id, rc.chunk.note_uuid)
        ranked.append(
            {
                "chunk_id": rc.chunk.id,
                "note_uuid": rc.chunk.note_uuid,
                "note_path": rc.chunk.note_path,
                "heading_path": rc.chunk.heading_path,
                "score": rc.combined_score,
                "vector_score": rc.vector_score,
                "graph_distance": rc.graph_distance,
                "seed_chunk_id": seed_chunk_id,
                "seed_note_uuid": seed_uuid,
                "seed_note_path": seed_note,
                # Which ranking drafted this candidate. `daemon select` runs
                # long after the retrieval, from this row alone, so the
                # attribution has to be persisted here or it is lost.
                "team": rc.team,
            }
        )

    return {
        "ranked": ranked,
        "seed_count": len(result.seeds),
        "expanded_count": len(result.expanded),
    }


class QueryEngine:
    def __init__(
        self,
        settings: Settings,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        feedback_store: FeedbackStore,
        llm_client: LLMClient,
        sparse_embedder: SparseEmbedder | None = None,
        surface: str = "cli",
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self.s = settings
        self.surface = surface
        self.ledger = ActivationLedger(db_path=settings.feedback.db_path)
        self.registry = NoteRegistry(db_path=settings.feedback.db_path)
        self.orchestrator = RetrievalOrchestrator(
            settings,
            embedder,
            vector_store,
            graph_store,
            sparse_embedder=sparse_embedder,
            listeners=[ActivationRecorder(self.ledger)],
            reranker=reranker,
        )
        self.feedback = feedback_store
        self.llm = llm_client

    def ask(
        self,
        query: str,
        synthesize: bool = True,
        *,
        surface: str | None = None,
        date_range: DateRange | None = None,
    ) -> QueryResponse:
        """``surface`` overrides the engine's default for this one call.

        One engine serves both of Hermes's paths — the deliberate
        ``mydaemon_recall`` tool and the per-turn ambient prefetch — and they
        must not be recorded as the same kind of event.

        ``date_range`` defaults to ``None`` — no filter — so every existing
        caller is untouched; §IV.10 surfaces (the CLI's ``--since``/``--until``,
        ``DaemonCore.recall``) pass one through to ``retrieve()``.
        """

        t0 = time.perf_counter()
        result = self.orchestrator.retrieve(
            query, surface=surface or self.surface, date_range=date_range
        )
        memories = self.recall_for(result)
        answer = (
            self.llm.synthesize(
                query,
                result.ranked,
                memories if self.s.memory.inject_into_context else None,
            )
            if synthesize
            else ""
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        event_id = self.feedback.log(
            FeedbackEvent(
                timestamp=datetime.now(UTC),
                query=query,
                retrieval_summary=build_retrieval_summary(result),
                answer=answer,
                latency_ms=latency_ms,
            )
        )
        if result.query_uid:
            # One direction only: `queries` is the retrieval record, `feedback`
            # is the answer + signal record. They are not the same event — a
            # Hermes prefetch produces no answer at all.
            self.ledger.link_feedback(result.query_uid, event_id)
        return QueryResponse(
            answer=answer,
            retrieval=result,
            feedback_event_id=event_id,
            latency_ms=latency_ms,
            memories=memories,
        )

    def recall_for(self, result: RetrievalResult) -> list[RecalledMemory]:
        """Never let a recall failure break the query it was enriching."""
        if not result.query_uid or not self.s.memory.recall_enabled:
            return []
        try:
            row = self.ledger.get(result.query_uid)
            if row is None:
                return []
            return recall_related(
                self.ledger, self.registry, query_id=int(row["id"]), settings=self.s
            )
        except Exception:  # noqa: BLE001
            log.warning("recall failed", exc_info=True)
            return []
