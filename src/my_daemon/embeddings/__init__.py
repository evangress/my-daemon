# SPDX-License-Identifier: Apache-2.0
"""Embedding model wrappers — dense (sentence-transformers) and sparse (fastembed)."""

from my_daemon.embeddings.embedder import Embedder
from my_daemon.embeddings.sparse import SparseEmbedder

__all__ = ["Embedder", "SparseEmbedder"]
