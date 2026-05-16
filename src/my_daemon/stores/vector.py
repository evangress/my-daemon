"""Qdrant client wrapper for chunk vectors, hybrid (dense+sparse) search, and payload filters."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from my_daemon.models import Chunk

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


_CHUNK_TEXT_PREVIEW_LIMIT = 4000  # full text stays in the graph store; payload is for display

# Named vector slots inside the chunks collection. Both live on the same point.
DENSE_NAME = "dense"
SPARSE_NAME = "sparse"


def _stable_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"my-daemon/chunk/{chunk_id}"))


class VectorStore:
    def __init__(self, url: str, collection: str, dim: int, hybrid: bool = True) -> None:
        self.url = url
        self.collection = collection
        self.dim = dim
        self.hybrid = hybrid
        self._client: QdrantClient | None = None

    def _client_(self) -> QdrantClient:
        if self._client is None:
            from qdrant_client import QdrantClient
            # check_compatibility=False: the docker-compose pins qdrant server 1.11 but
            # the pip client floats forward; the version-mismatch warning is noisy and
            # the operations we use (query_points/upsert/delete) work across both.
            self._client = QdrantClient(url=self.url, check_compatibility=False)
        return self._client

    def _collection_is_legacy(self) -> bool:
        """A pre-hybrid collection was created with a single unnamed vector slot.

        The hybrid schema uses *named* dense+sparse slots, so the two are
        incompatible — we drop and recreate (user must re-ingest) rather than
        silently mis-store new points.
        """
        client = self._client_()
        info = client.get_collection(collection_name=self.collection)
        vectors = info.config.params.vectors
        # Named-vector configs are dicts keyed by name. The old single-slot
        # config is a bare VectorParams.
        is_named = isinstance(vectors, dict)
        if not is_named:
            return True
        return DENSE_NAME not in vectors

    def ensure_collection(self) -> None:
        from qdrant_client.http.models import (
            Distance,
            SparseVectorParams,
            VectorParams,
        )

        client = self._client_()
        existing = {c.name for c in client.get_collections().collections}

        if self.collection in existing:
            if self.hybrid and self._collection_is_legacy():
                # Heads-up to the operator: hybrid needs a different schema.
                # Dropping is honest; pretending to coexist would lose writes.
                print(
                    f"[my-daemon] Collection '{self.collection}' uses the old single-vector "
                    f"schema; dropping it for the hybrid upgrade. Run `daemon ingest --full` to repopulate."
                )
                client.delete_collection(collection_name=self.collection)
            else:
                return

        if self.hybrid:
            client.create_collection(
                collection_name=self.collection,
                vectors_config={DENSE_NAME: VectorParams(size=self.dim, distance=Distance.COSINE)},
                sparse_vectors_config={SPARSE_NAME: SparseVectorParams()},
            )
        else:
            client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def upsert(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        sparse_vectors: list[tuple[list[int], list[float]]] | None = None,
    ) -> None:
        from qdrant_client.http.models import PointStruct, SparseVector

        if not chunks:
            return
        if self.hybrid and sparse_vectors is None:
            raise ValueError("hybrid VectorStore requires sparse_vectors on upsert")

        points = []
        for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            payload = {
                "chunk_id": chunk.id,
                "note_path": chunk.note_path,
                "heading_path": chunk.heading_path,
                "chunk_index": chunk.chunk_index,
                "tags": chunk.tags,
                "wikilinks": chunk.wikilinks,
                "text": chunk.text[:_CHUNK_TEXT_PREVIEW_LIMIT],
            }
            if self.hybrid:
                idx, val = sparse_vectors[i]
                point_vector: dict = {
                    DENSE_NAME: vec,
                    SPARSE_NAME: SparseVector(indices=idx, values=val),
                }
            else:
                point_vector = vec
            points.append(PointStruct(id=_stable_point_id(chunk.id), vector=point_vector, payload=payload))
        self._client_().upsert(collection_name=self.collection, points=points)

    def search(self, vector: list[float], top_k: int = 8) -> list[dict]:
        """Dense-only search (kept for the `hybrid=false` fallback path)."""
        client = self._client_()
        # In hybrid mode the dense vector is named; in legacy mode it's anonymous.
        query_kwargs: dict = {
            "collection_name": self.collection,
            "query": vector,
            "limit": top_k,
            "with_payload": True,
        }
        if self.hybrid:
            query_kwargs["using"] = DENSE_NAME
        response = client.query_points(**query_kwargs)
        return [{"score": p.score, **(p.payload or {})} for p in response.points]

    def hybrid_search(
        self,
        dense_vector: list[float],
        sparse_vector: tuple[list[int], list[float]],
        top_k: int = 8,
        prefetch_limit: int = 32,
    ) -> list[dict]:
        """Server-side dense+sparse RRF via Qdrant's prefetch + FusionQuery."""
        from qdrant_client.http.models import (
            Fusion,
            FusionQuery,
            Prefetch,
            SparseVector,
        )

        idx, val = sparse_vector
        sparse_q = SparseVector(indices=idx, values=val)

        response = self._client_().query_points(
            collection_name=self.collection,
            prefetch=[
                Prefetch(query=dense_vector, using=DENSE_NAME, limit=prefetch_limit),
                Prefetch(query=sparse_q, using=SPARSE_NAME, limit=prefetch_limit),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        return [{"score": p.score, **(p.payload or {})} for p in response.points]

    def delete_by_note(self, note_path: str) -> None:
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        client = self._client_()
        client.delete(
            collection_name=self.collection,
            points_selector=Filter(
                must=[FieldCondition(key="note_path", match=MatchValue(value=note_path))]
            ),
        )

    def count(self) -> int:
        result = self._client_().count(collection_name=self.collection, exact=True)
        return int(result.count)
