"""Compose seed + expand → dedupe → rank → trim to token budget."""

from __future__ import annotations

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.models import RetrievalResult, RetrievedChunk
from my_daemon.retrieval.expand import expand_from_seeds
from my_daemon.retrieval.seed import seed_search
from my_daemon.stores import GraphStore, VectorStore


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class RetrievalOrchestrator:
    def __init__(
        self,
        settings: Settings,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        sparse_embedder: SparseEmbedder | None = None,
    ) -> None:
        self.s = settings
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.sparse_embedder = sparse_embedder

    def retrieve(self, query: str) -> RetrievalResult:
        seeds = seed_search(
            query,
            self.embedder,
            self.vector_store,
            top_k=self.s.retrieval.seed_top_k,
            sparse_embedder=self.sparse_embedder,
        )
        expanded = expand_from_seeds(
            seeds,
            self.graph_store,
            self.vector_store,
            depth=self.s.graph.expansion_depth,
            decay=self.s.graph.distance_decay,
        )

        combined: dict[str, RetrievedChunk] = {}
        for rc in [*seeds, *expanded]:
            existing = combined.get(rc.chunk.id)
            if existing is None or rc.combined_score > existing.combined_score:
                combined[rc.chunk.id] = rc

        ranked = sorted(combined.values(), key=lambda r: r.combined_score, reverse=True)

        budget = self.s.retrieval.context_token_budget
        min_candidates = self.s.retrieval.candidate_pool

        used = 0
        kept: list[RetrievedChunk] = []
        for rc in ranked:
            cost = _approx_tokens(rc.chunk.text)
            if kept and used + cost > budget and len(kept) >= min_candidates:
                break
            kept.append(rc)
            used += cost

        return RetrievalResult(query=query, seeds=seeds, expanded=expanded, ranked=kept)
