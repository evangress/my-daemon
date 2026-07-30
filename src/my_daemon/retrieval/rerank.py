# SPDX-License-Identifier: Apache-2.0
"""Cross-encoder reranking: score (query, chunk) jointly rather than by cosine.

A bi-encoder embeds query and document independently, so relevance is whatever
survives compression into a single vector. A cross-encoder reads both together
and scores the pair, which is materially better at "is this passage actually
about this question" and materially slower — hence its use as a *reranker* over
a small candidate pool rather than as a retriever.

This unit scores and nothing else. It does not sort, does not know about config,
and does not know how its ordering will be used. That is what lets the
orchestrator tests run against a stub with no model download.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from my_daemon.models import Chunk, RetrievedChunk


def build_pair_text(chunk: Chunk) -> str:
    """The document side of the pair: context first, then the text.

    Heading path and date are included because they are real relevance signal a
    bare body omits — "March › 2nd" and a date help the model judge a temporal
    question that the prose alone leaves ambiguous.
    """

    heading = " › ".join(chunk.heading_path) if chunk.heading_path else ""
    stamp = f"  [{chunk.occurred_at.date().isoformat()}]" if chunk.occurred_at else ""
    header = f"{chunk.note_path}{' › ' + heading if heading else ''}{stamp}"
    return f"{header}\n{chunk.text.strip()}"


def _to_unit(score: float) -> float:
    return 1.0 / (1.0 + math.exp(-score))


class CrossEncoderReranker:
    def __init__(
        self,
        model_name: str,
        cache_folder: Path | None = None,
        *,
        _model: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_folder = cache_folder
        self._loaded = _model

    def _model_(self) -> Any:
        if self._loaded is None:
            from sentence_transformers import CrossEncoder

            kwargs: dict[str, Any] = {}
            if self.cache_folder is not None:
                kwargs["cache_folder"] = str(self.cache_folder)
            self._loaded = CrossEncoder(self.model_name, **kwargs)
        return self._loaded

    def rank(self, query: str, candidates: Sequence[RetrievedChunk]) -> list[float]:
        """One relevance score per candidate, in the order given.

        Scores already inside [0, 1] are returned **unchanged**. Some rerankers
        emit calibrated probabilities and some emit raw logits; sigmoiding
        unconditionally would squash a calibrated 0.007 to ~0.5 and throw away
        the model's own confidence.
        """

        if not candidates:
            return []
        pairs = [(query, build_pair_text(rc.chunk)) for rc in candidates]
        raw = [float(s) for s in self._model_().predict(pairs)]
        if all(0.0 <= s <= 1.0 for s in raw):
            return raw
        return [_to_unit(s) for s in raw]

    def download(self) -> Path:
        """Force the weights into the cache so later runs stay offline."""
        self._model_()
        return self.cache_folder or Path.home() / ".cache" / "huggingface"
