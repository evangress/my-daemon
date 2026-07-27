# SPDX-License-Identifier: Apache-2.0
"""One construction path for the stores.

The CLI, the GUI, `QueryEngine` and `build_core` each built the same six
objects independently. They drifted the moment `listeners=` existed — the GUI
went months without recall injection because its own hand-rolled synthesis call
never learned about it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.integration.wiring import build_orchestrator, build_stores


@pytest.fixture
def settings(tmp_path: Path, vault_root: Path) -> Settings:
    s = Settings()
    s.vault.path = vault_root
    s.embeddings.hybrid = False  # skip the sparse model download in tests
    s.graph.path = tmp_path / "data" / "graph.gpickle"
    s.graph.manifest_path = tmp_path / "data" / "manifest.json"
    s.feedback.db_path = tmp_path / "data" / "state.db"
    return s


def test_build_stores_returns_every_collaborator(settings: Settings):
    stores = build_stores(settings, load_graph=False, embedder=_FakeEmbedder())

    for name in (
        "settings",
        "embedder",
        "vector_store",
        "graph_store",
        "feedback_store",
        "registry",
        "ledger",
        "llm",
    ):
        assert getattr(stores, name) is not None, name


def test_the_sparse_embedder_follows_the_hybrid_flag(settings: Settings):
    settings.embeddings.hybrid = False

    assert (
        build_stores(settings, load_graph=False, embedder=_FakeEmbedder()).sparse_embedder is None
    )


def test_stores_share_one_state_database(settings: Settings):
    """Feedback, registry and ledger must not fan out into separate files."""
    stores = build_stores(settings, load_graph=False, embedder=_FakeEmbedder())

    assert stores.feedback_store.db_path == stores.registry.db_path == stores.ledger.db_path


def test_the_orchestrator_records_activations_by_default(settings: Settings):
    stores = build_stores(settings, load_graph=False, embedder=_FakeEmbedder())

    orchestrator = build_orchestrator(stores)

    assert orchestrator.listeners, "every surface must record, or the ledger lies"


def test_recording_can_be_switched_off_for_debug_paths(settings: Settings):
    """`daemon search` is a probe, not a question — it must not pollute."""
    stores = build_stores(settings, load_graph=False, embedder=_FakeEmbedder())

    assert build_orchestrator(stores, record_activations=False).listeners == []


class _FakeEmbedder:
    dimension = 4

    def encode(self, texts):  # noqa: ANN001
        return [[0.0] * 4 for _ in texts]

    def encode_one(self, text):  # noqa: ANN001
        return [0.0] * 4
