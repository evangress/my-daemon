# SPDX-License-Identifier: Apache-2.0
"""Dev convenience: dump graph stats and vector counts."""

from __future__ import annotations

from my_daemon.config import load_settings
from my_daemon.embeddings import Embedder
from my_daemon.stores import GraphStore, VectorStore


def main() -> None:
    s = load_settings()
    graph = GraphStore(path=s.graph.path)
    graph.load()
    stats = graph.stats()
    print(f"notes={stats.note_count} tags={stats.tag_count} edges={stats.edge_count}")

    try:
        embedder = Embedder(s.embeddings.model)
        vec = VectorStore(s.vector_store.qdrant.url, s.vector_store.qdrant.collection, dim=embedder.dimension)
        print(f"vector_chunks={vec.count()}")
    except Exception as exc:
        print(f"vector store unavailable: {exc}")


if __name__ == "__main__":
    main()
