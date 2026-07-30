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
from my_daemon.retrieval.interleave import EXPANSION_TEAM, RERANK_TEAM, SEED_TEAM, multileave
from my_daemon.retrieval.rerank import CrossEncoderReranker
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
        reranker: CrossEncoderReranker | None = None,
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
        # Defaults to None so every existing construction site and test keeps
        # working untouched (§IV.18). Scores only — it does not know about
        # config, which is why `retrieval.rerank` is checked separately below
        # rather than folded into "is a reranker present".
        self.reranker = reranker
        # Set per-query in `_pool`; `None` means no rerank ran this query.
        self._rerank_ms: int | None = None

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
        # Reset per query: a prior query's rerank must never leak into this
        # one's result, e.g. when the feature is toggled off mid-process.
        self._rerank_ms = None
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

        ranked = self._pool(query, seeds, expanded)

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
            rerank_ms=self._rerank_ms,
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
        self, query: str, seeds: list[RetrievedChunk], expanded: list[RetrievedChunk]
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
        rankings: dict[str, list[RetrievedChunk]] = {
            SEED_TEAM: [rc for rc in by_score if rc.chunk.id in seed_ids],
            EXPANSION_TEAM: [rc for rc in by_score if rc.chunk.id not in seed_ids],
        }

        if self.reranker is not None and self.s.retrieval.rerank:
            capped = by_score[: self.s.retrieval.rerank_max_candidates]
            t0 = time.perf_counter()
            scores = self.reranker.rank(query, capped)
            # Reported separately so the cost stays visible instead of being
            # folded into a total nobody can attribute.
            self._rerank_ms = int((time.perf_counter() - t0) * 1000)
            # Sorted into a new list; `combined_score` is deliberately
            # untouched — it is persisted in the retrieval summary and feeds
            # the activation ledger's rank-based strength, so overwriting it
            # would silently change what a fingerprint means.
            rankings[RERANK_TEAM] = [
                rc for _, rc in sorted(zip(scores, capped, strict=True), key=lambda p: -p[0])
            ]

        return multileave(rankings, rng=self.rng)

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
