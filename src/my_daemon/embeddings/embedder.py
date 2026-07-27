# SPDX-License-Identifier: Apache-2.0
"""Thin sentence-transformers wrapper with lazy model loading and batched encoding.

The embedder pins the model to a project-local ``cache_folder`` and prefers
offline loads: it tries ``local_files_only=True`` first so the HF Hub is not
contacted on every query. If the cache is empty, it falls back to a single
download, and subsequent loads stay offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class Embedder:
    def __init__(
        self,
        model_name: str,
        batch_size: int = 32,
        device: str = "auto",
        cache_folder: Path | str | None = None,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self.device = device
        self.cache_folder = Path(cache_folder).expanduser() if cache_folder else None
        self._model: SentenceTransformer | None = None
        self._dim: int | None = None

    def _dimension_of(self, model: SentenceTransformer) -> int:
        """The model's output width, or a plain error saying we cannot tell.

        ``get_embedding_dimension()`` returns ``None`` for a model whose head
        does not declare one. That number goes straight into the Qdrant
        collection schema, so guessing is not an option — and ``int(None)``
        would surface as a bare ``TypeError`` from an unrelated-looking line.
        """

        dim = model.get_embedding_dimension()
        if dim is None:
            raise RuntimeError(
                f"embedding model {self.model_name!r} does not report an output "
                "dimension; pick a sentence-transformers model that does"
            )
        return int(dim)

    def _resolve_device(self) -> str | None:
        if self.device == "auto":
            return None
        return self.device

    def _load(self, *, local_files_only: bool) -> SentenceTransformer:
        from sentence_transformers import SentenceTransformer

        kwargs: dict = {"device": self._resolve_device(), "local_files_only": local_files_only}
        if self.cache_folder is not None:
            self.cache_folder.mkdir(parents=True, exist_ok=True)
            kwargs["cache_folder"] = str(self.cache_folder)
        return SentenceTransformer(self.model_name, **kwargs)

    def _ensure_loaded(self) -> SentenceTransformer:
        if self._model is None:
            try:
                self._model = self._load(local_files_only=True)
            except Exception:
                # Cache miss (or corrupted local copy) — pull once, then stay offline next time.
                self._model = self._load(local_files_only=False)
            self._dim = self._dimension_of(self._model)
        return self._model

    def download(self) -> Path:
        """Force a download of the model into ``cache_folder`` and return the folder.

        Safe to call when already cached — sentence-transformers will no-op.
        """
        self._model = self._load(local_files_only=False)
        self._dim = self._dimension_of(self._model)
        return (
            self.cache_folder
            if self.cache_folder is not None
            else Path.home() / ".cache" / "huggingface"
        )

    @property
    def dimension(self) -> int:
        self._ensure_loaded()
        assert self._dim is not None
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_loaded()
        vectors = model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [v.tolist() for v in vectors]

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]
