"""fastembed-based sparse encoder (BM42 by default) for hybrid retrieval.

Same offline-first cache pattern as ``Embedder``: try ``local_files_only``
first, fall back to a one-time download. Returns ``(indices, values)`` pairs
ready to wrap in Qdrant's ``SparseVector``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastembed import SparseTextEmbedding


class SparseEmbedder:
    def __init__(
        self,
        model_name: str = "Qdrant/bm42-all-minilm-l6-v2-attentions",
        cache_folder: Path | str | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_folder = Path(cache_folder).expanduser() if cache_folder else None
        self._model: SparseTextEmbedding | None = None

    def _load(self, *, local_files_only: bool) -> SparseTextEmbedding:
        from fastembed import SparseTextEmbedding

        kwargs: dict = {"model_name": self.model_name, "lazy_load": False}
        if self.cache_folder is not None:
            self.cache_folder.mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = str(self.cache_folder)
        # fastembed honors HF_HUB_OFFLINE for "already cached" loads; toggle it
        # for the duration of construction so a cache miss doesn't silently
        # attempt an unwanted download.
        import os

        prev = os.environ.get("HF_HUB_OFFLINE")
        if local_files_only:
            os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            return SparseTextEmbedding(**kwargs)
        finally:
            if prev is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev

    def _ensure_loaded(self) -> SparseTextEmbedding:
        if self._model is None:
            try:
                self._model = self._load(local_files_only=True)
            except Exception:
                self._model = self._load(local_files_only=False)
        return self._model

    def encode(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        if not texts:
            return []
        model = self._ensure_loaded()
        out: list[tuple[list[int], list[float]]] = []
        for emb in model.embed(texts):
            out.append((emb.indices.tolist(), emb.values.tolist()))
        return out

    def encode_one(self, text: str) -> tuple[list[int], list[float]]:
        return self.encode([text])[0]

    def download(self) -> Path:
        """Force-download the sparse model into ``cache_folder``."""
        self._model = self._load(local_files_only=False)
        return self.cache_folder if self.cache_folder is not None else Path.home() / ".cache" / "fastembed"
