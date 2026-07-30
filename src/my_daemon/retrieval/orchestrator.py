# SPDX-License-Identifier: Apache-2.0
"""Compose seed + expand → dedupe → rank → trim to token budget."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.models import THEME_TAG_PREFIX, DateRange, RetrievalResult, RetrievedChunk
from my_daemon.retrieval.expand import expand_from_seeds
from my_daemon.retrieval.interleave import EXPANSION_TEAM, SEED_TEAM, multileave
from my_daemon.retrieval.seed import seed_search
from my_daemon.retrieval.trace import RetrievalListener, RetrievalTrace, activations_from
from my_daemon.stores import GraphStore, VectorStore

log = logging.getLogger(__name__)


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
        listeners: Sequence[RetrievalListener] = (),
        rng: random.Random | None = None,
    ) -> None:
        self.s = settings
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.sparse_embedder = sparse_embedder
        # Defaults to empty, so every existing construction site and test keeps
        # working untouched.
        self.listeners = list(listeners)
        # The interleaving coin. Injectable so a test can assert on draft order
        # rather than on a distribution; unseeded in production, because a
        # *predictable* toss would reintroduce exactly the position bias the
        # draft exists to cancel.
        self.rng = rng or random.Random()

    def retrieve(
        self,
        query: str,
        *,
        surface: str = "unknown",
        session_id: str | None = None,
        date_range: DateRange | None = None,
    ) -> RetrievalResult:
        started_at = datetime.now(UTC)
        t0 = time.perf_counter()
        seeds = seed_search(
            query,
            self.embedder,
            self.vector_store,
            top_k=self.s.retrieval.seed_top_k,
            sparse_embedder=self.sparse_embedder,
            date_range=date_range,
        )
        expanded = expand_from_seeds(
            seeds,
            self.graph_store,
            self.vector_store,
            depth=self.s.graph.expansion_depth,
            decay=self.s.graph.distance_decay,
            exclude_tag_prefixes=(THEME_TAG_PREFIX,),
            date_range=date_range,
        )

        ranked = self._pool(seeds, expanded)

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

        range_active = date_range is not None and date_range.is_active
        result = RetrievalResult(
            query=query,
            seeds=seeds,
            expanded=expanded,
            ranked=kept,
            date_range=date_range,
            # Only when a filter is active: an unfiltered query must pay
            # nothing for it.
            undated_excluded=(self.vector_store.count_undated() if range_active else 0),
        )
        self._notify(
            result,
            surface=surface,
            session_id=session_id,
            started_at=started_at,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        return result

    def _pool(
        self, seeds: list[RetrievedChunk], expanded: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """Order the merged candidates — by team draft, or by raw score.

        Both branches dedupe by chunk id first, keeping the better-scoring
        appearance, so the two orderings are over the same set.
        """

        best: dict[str, RetrievedChunk] = {}
        for rc in [*seeds, *expanded]:
            existing = best.get(rc.chunk.id)
            if existing is None or rc.combined_score > existing.combined_score:
                best[rc.chunk.id] = rc

        by_score = sorted(best.values(), key=lambda r: r.combined_score, reverse=True)
        if not self.s.retrieval.interleave:
            return by_score

        seed_ids = {s.chunk.id for s in seeds}
        return multileave(
            {
                SEED_TEAM: [rc for rc in by_score if rc.chunk.id in seed_ids],
                EXPANSION_TEAM: [rc for rc in by_score if rc.chunk.id not in seed_ids],
            },
            rng=self.rng,
        )

    def _notify(
        self,
        result: RetrievalResult,
        *,
        surface: str,
        session_id: str | None,
        started_at: datetime,
        latency_ms: int,
    ) -> None:
        if not self.listeners:
            return
        trace = RetrievalTrace(
            query=result.query,
            surface=surface,
            started_at=started_at,
            latency_ms=latency_ms,
            result=result,
            activations=activations_from(result),
            session_id=session_id,
        )
        for listener in self.listeners:
            try:
                query_uid = listener.on_retrieval(trace)
            except Exception:  # noqa: BLE001 — never break a query over bookkeeping
                log.warning("retrieval listener failed", exc_info=True)
                continue
            if query_uid and result.query_uid is None:
                result.query_uid = query_uid
