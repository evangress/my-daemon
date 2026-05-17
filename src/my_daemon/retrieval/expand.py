# SPDX-License-Identifier: Apache-2.0
"""Graph expansion: starting from seed chunks, walk wikilink/tag edges to neighbors."""

from __future__ import annotations

from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.stores import GraphStore, VectorStore


def expand_from_seeds(
    seeds: list[RetrievedChunk],
    graph_store: GraphStore,
    vector_store: VectorStore,
    depth: int = 2,
    decay: float = 0.5,
) -> list[RetrievedChunk]:
    """For each seed, BFS the graph and pull chunks belonging to neighbor notes.

    Score for an expanded chunk is the seed's vector score decayed by graph distance.
    The same chunk may be reachable from multiple seeds; we keep the best score.
    """

    from qdrant_client.http.models import FieldCondition, Filter, MatchValue

    out: dict[str, RetrievedChunk] = {}
    seen_seed_paths: set[str] = {s.chunk.note_path for s in seeds}

    client = vector_store._client_()

    for seed in seeds:
        # weighted=True: Dijkstra over 1/weight so feedback-reinforced edges
        # produce a *smaller* distance and ride in with a higher decayed score.
        # Hop budget still enforced inside the call.
        neighbors = graph_store.neighbors_within(seed.chunk.note_path, depth=depth, weighted=True)
        for rel_path, distance in neighbors.items():
            if rel_path in seen_seed_paths:
                # already represented by the seed itself
                continue
            # Pull chunks of this neighbor note from Qdrant via payload filter.
            scroll_hits, _ = client.scroll(
                collection_name=vector_store.collection,
                scroll_filter=Filter(
                    must=[FieldCondition(key="note_path", match=MatchValue(value=rel_path))]
                ),
                with_payload=True,
                limit=64,
            )
            seed_score = seed.vector_score or 0.0
            for point in scroll_hits:
                p = point.payload or {}
                chunk = Chunk(
                    id=p["chunk_id"],
                    note_path=p["note_path"],
                    heading_path=list(p.get("heading_path") or []),
                    text=p.get("text", ""),
                    chunk_index=int(p.get("chunk_index", 0)),
                    tags=list(p.get("tags") or []),
                    wikilinks=list(p.get("wikilinks") or []),
                )
                score = seed_score * (decay ** distance)
                existing = out.get(chunk.id)
                if existing is None or score > existing.combined_score:
                    out[chunk.id] = RetrievedChunk(
                        chunk=chunk,
                        vector_score=None,
                        graph_distance=distance,
                        seed_chunk_id=seed.chunk.id,
                        combined_score=score,
                    )
    return list(out.values())
