# SPDX-License-Identifier: Apache-2.0
"""Query pipeline: retrieve, synthesize, log."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.llm import LLMClient
from my_daemon.models import FeedbackEvent, RetrievalResult
from my_daemon.retrieval import RetrievalOrchestrator
from my_daemon.stores import FeedbackStore, GraphStore, VectorStore


@dataclass
class QueryResponse:
    answer: str
    retrieval: RetrievalResult
    feedback_event_id: int
    latency_ms: int


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
        # graph_distance==0 means the chunk *is* the seed; otherwise it was
        # pulled in via expansion and the seed lookup gives us the start node.
        if rc.graph_distance == 0:
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
    ) -> None:
        self.s = settings
        self.orchestrator = RetrievalOrchestrator(
            settings, embedder, vector_store, graph_store, sparse_embedder=sparse_embedder,
        )
        self.feedback = feedback_store
        self.llm = llm_client

    def ask(self, query: str, synthesize: bool = True) -> QueryResponse:
        t0 = time.perf_counter()
        result = self.orchestrator.retrieve(query)
        answer = self.llm.synthesize(query, result.ranked) if synthesize else ""
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
        return QueryResponse(answer=answer, retrieval=result, feedback_event_id=event_id, latency_ms=latency_ms)
