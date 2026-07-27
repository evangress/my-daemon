# SPDX-License-Identifier: Apache-2.0
"""The one boundary in ``Embedder`` where a missing value is not recoverable."""

from __future__ import annotations

import pytest

from my_daemon.embeddings.embedder import Embedder


class _ModelWithDimension:
    def get_embedding_dimension(self) -> int:
        return 384


class _ModelWithoutDimension:
    def get_embedding_dimension(self) -> None:
        return None


def test_dimension_of_reads_the_models_width() -> None:
    assert Embedder("some-model")._dimension_of(_ModelWithDimension()) == 384


def test_dimension_of_names_the_model_that_reports_nothing() -> None:
    """This number becomes the Qdrant collection schema, so it is never guessed."""
    with pytest.raises(RuntimeError, match="some-model"):
        Embedder("some-model")._dimension_of(_ModelWithoutDimension())
