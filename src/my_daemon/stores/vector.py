# SPDX-License-Identifier: Apache-2.0
"""Qdrant client wrapper for chunk vectors, hybrid (dense+sparse) search, and payload filters."""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from my_daemon.models import Chunk

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

    from my_daemon.config import QdrantConfig


_CHUNK_TEXT_PREVIEW_LIMIT = 4000  # full text stays in the graph store; payload is for display

# Named vector slots inside the chunks collection. Both live on the same point.
DENSE_NAME = "dense"
SPARSE_NAME = "sparse"

# The literal qdrant-client accepts for a non-persistent embedded store.
MEMORY_LOCATION = ":memory:"


class LocalStoreLockedError(RuntimeError):
    """Embedded Qdrant is single-process and the storage folder is already held.

    Raised instead of qdrant-client's bare ``RuntimeError`` so the operator gets
    the *cause* and the two ways out (stop the other process, or move to a
    server) rather than a traceback out of a vendored lock helper.
    """


def _stable_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"my-daemon/chunk/{chunk_id}"))


class VectorStore:
    """Qdrant-backed chunk store, in either server or embedded mode.

    Pass ``url`` for a running Qdrant server, or ``path`` to run the engine
    in-process against a local folder (embedded mode — no Docker). Exactly one
    of the two, and the choice is made here because ``_client_`` is the single
    place a client is ever constructed.
    """

    def __init__(
        self,
        url: str | None,
        collection: str,
        dim: int,
        hybrid: bool = True,
        path: str | Path | None = None,
    ) -> None:
        if (url is None) == (path is None):
            raise ValueError(
                "VectorStore needs exactly one of url= (server mode) or "
                "path= (embedded mode); "
                f"got url={url!r}, path={path!r}"
            )
        self.url = url
        self.path = path
        self.collection = collection
        self.dim = dim
        self.hybrid = hybrid
        self._client: QdrantClient | None = None

    @classmethod
    def from_config(cls, config: QdrantConfig, dim: int, hybrid: bool = True) -> VectorStore:
        """Build from the ``vector_store.qdrant`` config block.

        The mode branch lives in exactly one place — here — so no call site has
        to know that embedded and server modes exist.
        """
        return cls(
            url=None if config.is_embedded else config.url,
            collection=config.collection,
            dim=dim,
            hybrid=hybrid,
            path=config.path,
        )

    @property
    def is_embedded(self) -> bool:
        return self.path is not None

    @property
    def location(self) -> str:
        """Human-readable target, for error messages and status output."""
        return str(self.path) if self.path is not None else str(self.url)

    def _client_(self) -> QdrantClient:
        if self._client is None:
            from qdrant_client import QdrantClient

            if self.path is not None:
                self._client = self._open_local(QdrantClient)
            else:
                # check_compatibility=False: the docker-compose pins qdrant server 1.11 but
                # the pip client floats forward; the version-mismatch warning is noisy and
                # the operations we use (query_points/upsert/delete) work across both.
                self._client = QdrantClient(url=self.url, check_compatibility=False)
        return self._client

    def _open_local(self, client_cls: type[QdrantClient]) -> QdrantClient:
        """Open the in-process engine, translating the lock failure.

        Embedded Qdrant takes an exclusive flock on the storage folder. Two
        daemons (say, the CLI and the GUI) pointed at the same ``path`` is a
        configuration mistake, not a crash — say so.
        """
        location = str(self.path)
        if location != MEMORY_LOCATION:
            Path(location).mkdir(parents=True, exist_ok=True)
        try:
            return client_cls(path=location)
        except RuntimeError as exc:
            # qdrant-client signals this with a plain RuntimeError and no
            # dedicated type; match on either half of its wording, and re-raise
            # untouched if it turns out to be some other RuntimeError.
            text = str(exc)
            if "already accessed" not in text and "another instance" not in text:
                raise
            raise LocalStoreLockedError(
                f"Embedded Qdrant storage at {location} is already open in another "
                "process (embedded mode allows exactly one). Stop the other "
                "my-daemon process, or switch vector_store.qdrant to a server "
                "'url' (docker-compose up -d qdrant) if you need concurrent access."
            ) from exc

    def close(self) -> None:
        """Release the client — and, in embedded mode, the storage lock.

        Idempotent, and safe to call on a store that was never used.
        """
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

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
        if not isinstance(vectors, dict):
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

        self._ensure_payload_indexes()

    def _ensure_payload_indexes(self) -> None:
        """Index the fields we filter on.

        ``note_uuid`` carries graph expansion's scroll, which runs for *every
        seed of every query* — it was unindexed before the cutover and that was
        pure latency. ``note_path`` is indexed too so display-side lookups
        (and the rename path) stay cheap.

        Embedded mode has no payload indexes at all (it filters in Python and
        warns if you ask), so we skip rather than emit a UserWarning on every
        ``ensure_collection`` for something that is a no-op there.
        """

        if self.is_embedded:
            return

        from qdrant_client.http.models import PayloadSchemaType

        client = self._client_()
        for field in ("note_uuid", "note_path"):
            with contextlib.suppress(Exception):  # already indexed → fine
                client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
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
        # Non-None exactly when we are in hybrid mode, which is what the loop
        # below branches on.
        sparse: list[tuple[list[int], list[float]]] | None = None
        if self.hybrid:
            if sparse_vectors is None:
                raise ValueError("hybrid VectorStore requires sparse_vectors on upsert")
            sparse = sparse_vectors

        points = []
        for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            payload = {
                "chunk_id": chunk.id,
                "note_uuid": chunk.note_uuid,
                "note_path": chunk.note_path,
                "heading_path": chunk.heading_path,
                "chunk_index": chunk.chunk_index,
                "tags": chunk.tags,
                "wikilinks": chunk.wikilinks,
                "text": chunk.text[:_CHUNK_TEXT_PREVIEW_LIMIT],
            }
            point_vector: dict | list[float]
            if sparse is not None:
                idx, val = sparse[i]
                point_vector = {
                    DENSE_NAME: vec,
                    SPARSE_NAME: SparseVector(indices=idx, values=val),
                }
            else:
                point_vector = vec
            points.append(
                PointStruct(id=_stable_point_id(chunk.id), vector=point_vector, payload=payload)
            )
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

    def set_note_path(self, note_uuid: str, rel_path: str) -> None:
        """Update the display path on every chunk of a note, without re-embedding.

        A rename changes nothing semantic, and chunk ids derive from the uuid,
        so the points themselves are already correct — only this one payload
        field is stale.
        """

        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        self._client_().set_payload(
            collection_name=self.collection,
            payload={"note_path": rel_path},
            points=Filter(
                must=[FieldCondition(key="note_uuid", match=MatchValue(value=note_uuid))]
            ),
            wait=True,
        )

    def delete_by_note_uuid(self, note_uuid: str) -> None:
        from qdrant_client.http.models import FieldCondition, Filter, FilterSelector, MatchValue

        self._client_().delete(
            collection_name=self.collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="note_uuid", match=MatchValue(value=note_uuid))]
                )
            ),
            wait=True,
        )

    def count(self) -> int:
        result = self._client_().count(collection_name=self.collection, exact=True)
        return int(result.count)
