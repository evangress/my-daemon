"""Vector top-K seed retrieval."""

from __future__ import annotations

from my_daemon.embeddings import Embedder
from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.stores import VectorStore


def seed_search(
    query: str,
    embedder: Embedder,
    vector_store: VectorStore,
    top_k: int = 8,
) -> list[RetrievedChunk]:
    """Embed the query and return the top-K Qdrant hits as RetrievedChunks."""

    vec = embedder.encode_one(query)
    hits = vector_store.search(vec, top_k=top_k)
    seeds: list[RetrievedChunk] = []
    for h in hits:
        chunk = Chunk(
            id=h["chunk_id"],
            note_path=h["note_path"],
            heading_path=list(h.get("heading_path") or []),
            text=h.get("text", ""),
            chunk_index=int(h.get("chunk_index", 0)),
            tags=list(h.get("tags") or []),
            wikilinks=list(h.get("wikilinks") or []),
        )
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
