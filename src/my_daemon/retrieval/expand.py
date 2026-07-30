# SPDX-License-Identifier: Apache-2.0
"""Graph expansion: starting from seed chunks, walk wikilink/tag edges to neighbors."""

from __future__ import annotations

from my_daemon.models import DateRange, RetrievedChunk
from my_daemon.stores import GraphStore, VectorStore
from my_daemon.stores.vector import chunk_from_payload, date_conditions


def expand_from_seeds(
    seeds: list[RetrievedChunk],
    graph_store: GraphStore,
    vector_store: VectorStore,
    depth: int = 2,
    decay: float = 0.5,
    exclude_tag_prefixes: tuple[str, ...] = (),
    *,
    date_range: DateRange | None = None,
) -> list[RetrievedChunk]:
    """For each seed, BFS the graph and pull chunks belonging to neighbor notes.

    Score for an expanded chunk is the seed's vector score decayed by graph distance.
    The same chunk may be reachable from multiple seeds; we keep the best score.

    ``date_range``, when active, is folded into the same ``scroll_filter`` that
    already restricts each scroll to one neighbor note — this is the other of
    the two retrieval arms that must honor the filter (see ``seed_search``);
    filtering only seeds would let an out-of-range note back in through the
    graph.
    """

    from qdrant_client.http.models import FieldCondition, Filter, MatchValue

    out: dict[str, RetrievedChunk] = {}
    seen_seed_uuids: set[str] = {s.chunk.note_uuid for s in seeds}

    client = vector_store._client_()

    # Computed once: empty when no range is active, matching the no-filter
    # path in seed_search / VectorStore.search.
    extra_conditions = date_conditions(date_range)

    for seed in seeds:
        # weighted=True: Dijkstra over 1/weight so feedback-reinforced edges
        # produce a *smaller* distance and ride in with a higher decayed score.
        # Hop budget still enforced inside the call.
        neighbors = graph_store.neighbors_within(
            seed.chunk.note_uuid,
            depth=depth,
            weighted=True,
            exclude_tag_prefixes=exclude_tag_prefixes,
        )
        for neighbor_uuid, distance in neighbors.items():
            if neighbor_uuid in seen_seed_uuids:
                # already represented by the seed itself
                continue
            # Pull chunks of this neighbor note from Qdrant via payload filter.
            scroll_hits, _ = client.scroll(
                collection_name=vector_store.collection,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="note_uuid", match=MatchValue(value=neighbor_uuid)),
                        *extra_conditions,
                    ]
                ),
                with_payload=True,
                limit=64,
            )
            seed_score = seed.vector_score or 0.0
            for point in scroll_hits:
                p = point.payload or {}
                chunk = chunk_from_payload(p)
                score = seed_score * (decay**distance)
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
