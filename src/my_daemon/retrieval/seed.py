# SPDX-License-Identifier: Apache-2.0
"""Vector top-K seed retrieval (dense, or dense+sparse RRF when hybrid is on)."""

from __future__ import annotations

from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.models import RetrievedChunk
from my_daemon.stores import VectorStore
from my_daemon.stores.vector import chunk_from_payload


def seed_search(
    query: str,
    embedder: Embedder,
    vector_store: VectorStore,
    top_k: int = 8,
    sparse_embedder: SparseEmbedder | None = None,
) -> list[RetrievedChunk]:
    """Embed the query and return the top-K hits as RetrievedChunks.

    When ``sparse_embedder`` is provided, runs a Qdrant server-side RRF fusion
    over the dense and sparse rankings. Otherwise falls back to dense-only.
    """

    vec = embedder.encode_one(query)
    if sparse_embedder is not None:
        sparse_vec = sparse_embedder.encode_one(query)
        hits = vector_store.hybrid_search(vec, sparse_vec, top_k=top_k)
    else:
        hits = vector_store.search(vec, top_k=top_k)
    seeds: list[RetrievedChunk] = []
    for h in hits:
        chunk = chunk_from_payload(h)
        seeds.append(
            RetrievedChunk(
                chunk=chunk,
                vector_score=float(h["score"]),
                graph_distance=0,
                seed_chunk_id=chunk.id,
                combined_score=float(h["score"]),
            )
        )
    return seeds
