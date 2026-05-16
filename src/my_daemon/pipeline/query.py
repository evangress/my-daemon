"""Query pipeline: retrieve, synthesize, log."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder
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


class QueryEngine:
    def __init__(
        self,
        settings: Settings,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        feedback_store: FeedbackStore,
        llm_client: LLMClient,
    ) -> None:
        self.s = settings
        self.orchestrator = RetrievalOrchestrator(settings, embedder, vector_store, graph_store)
        self.feedback = feedback_store
        self.llm = llm_client

    def ask(self, query: str, synthesize: bool = True) -> QueryResponse:
        t0 = time.perf_counter()
        result = self.orchestrator.retrieve(query)
        answer = self.llm.synthesize(query, result.ranked) if synthesize else ""
        latency_ms = int((time.perf_counter() - t0) * 1000)

        summary = {
            "ranked": [
                {
                    "chunk_id": rc.chunk.id,
                    "note_path": rc.chunk.note_path,
                    "heading_path": rc.chunk.heading_path,
                    "score": rc.combined_score,
                    "vector_score": rc.vector_score,
                    "graph_distance": rc.graph_distance,
                }
                for rc in result.ranked
            ],
            "seed_count": len(result.seeds),
            "expanded_count": len(result.expanded),
        }

        event_id = self.feedback.log(
            FeedbackEvent(
                timestamp=datetime.now(UTC),
                query=query,
                retrieval_summary=summary,
                answer=answer,
                latency_ms=latency_ms,
            )
        )
        return QueryResponse(answer=answer, retrieval=result, feedback_event_id=event_id, latency_ms=latency_ms)
