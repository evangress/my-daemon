"""Qdrant client wrapper for chunk vectors and payload-filtered search."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from my_daemon.models import Chunk

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


_CHUNK_TEXT_PREVIEW_LIMIT = 4000  # full text stays in the graph store; payload is for display


def _stable_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"my-daemon/chunk/{chunk_id}"))


class VectorStore:
    def __init__(self, url: str, collection: str, dim: int) -> None:
        self.url = url
        self.collection = collection
        self.dim = dim
        self._client: QdrantClient | None = None

    def _client_(self) -> QdrantClient:
        if self._client is None:
            from qdrant_client import QdrantClient
            self._client = QdrantClient(url=self.url)
        return self._client

    def ensure_collection(self) -> None:
        from qdrant_client.http.models import Distance, VectorParams

        client = self._client_()
        existing = {c.name for c in client.get_collections().collections}
        if self.collection in existing:
            return
        client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        from qdrant_client.http.models import PointStruct

        if not chunks:
            return
        points = []
        for chunk, vec in zip(chunks, vectors, strict=True):
            payload = {
                "chunk_id": chunk.id,
                "note_path": chunk.note_path,
                "heading_path": chunk.heading_path,
                "chunk_index": chunk.chunk_index,
                "tags": chunk.tags,
                "wikilinks": chunk.wikilinks,
                "text": chunk.text[:_CHUNK_TEXT_PREVIEW_LIMIT],
            }
            points.append(PointStruct(id=_stable_point_id(chunk.id), vector=vec, payload=payload))
        self._client_().upsert(collection_name=self.collection, points=points)

    def search(self, vector: list[float], top_k: int = 8) -> list[dict]:
        client = self._client_()
        response = client.query_points(
            collection_name=self.collection,
            query=vector,
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
