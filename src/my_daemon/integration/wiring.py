# SPDX-License-Identifier: Apache-2.0
"""One construction path for the daemon's stores.

The CLI, the GUI, :class:`QueryEngine` and ``build_core`` each used to build the
same six collaborators independently, and they drifted the moment the
orchestrator gained ``listeners=``: the GUI kept its own hand-rolled synthesis
call and so never learned about fingerprint recall at all.

Everything that needs stores goes through here now.
"""

from __future__ import annotations

from dataclasses import dataclass

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.llm import LLMClient
from my_daemon.pipeline.activation import ActivationRecorder
from my_daemon.retrieval import RetrievalOrchestrator
from my_daemon.stores import FeedbackStore, GraphStore, NoteRegistry, VectorStore
from my_daemon.stores.activations import ActivationLedger


@dataclass
class Stores:
    settings: Settings
    embedder: Embedder
    sparse_embedder: SparseEmbedder | None
    vector_store: VectorStore
    graph_store: GraphStore
    feedback_store: FeedbackStore
    registry: NoteRegistry
    ledger: ActivationLedger
    llm: LLMClient


def build_stores(
    settings: Settings,
    *,
    load_graph: bool = True,
    embedder: Embedder | None = None,
) -> Stores:
    """Construct every store from settings.

    The embedder and Qdrant client are touched lazily on first use, so this
    stays cheap enough to call from a provider's ``initialize`` — but never
    from anything required to stay network-free. ``embedder`` is injectable so
    tests can skip a model download.
    """

    embedder = embedder or Embedder(
        settings.embeddings.model,
        batch_size=settings.embeddings.batch_size,
        device=settings.embeddings.device,
        cache_folder=settings.embeddings.cache_folder,
    )
    sparse_embedder = (
        SparseEmbedder(
            model_name=settings.embeddings.sparse_model,
            cache_folder=settings.embeddings.cache_folder,
        )
        if settings.embeddings.hybrid
        else None
    )
    vector_store = VectorStore.from_config(
        settings.vector_store.qdrant,
        dim=embedder.dimension,
        hybrid=settings.embeddings.hybrid,
    )
    graph_store = GraphStore(path=settings.graph.path)
    if load_graph:
        graph_store.load()

    # Feedback, registry and ledger deliberately share one file — one state
    # database to back up or wipe.
    db_path = settings.feedback.db_path
    return Stores(
        settings=settings,
        embedder=embedder,
        sparse_embedder=sparse_embedder,
        vector_store=vector_store,
        graph_store=graph_store,
        feedback_store=FeedbackStore(db_path=db_path),
        registry=NoteRegistry(db_path=db_path),
        ledger=ActivationLedger(db_path=db_path),
        llm=LLMClient(settings.llm, api_key=settings.anthropic_api_key),
    )


def build_orchestrator(
    stores: Stores,
    *,
    record_activations: bool = True,
) -> RetrievalOrchestrator:
    """The orchestrator, wired to record unless a caller opts out.

    ``record_activations=False`` exists for debug probes like ``daemon search``,
    which are not questions the user asked and must not pollute the fingerprint
    space.
    """

    listeners = [ActivationRecorder(stores.ledger)] if record_activations else []
    return RetrievalOrchestrator(
        stores.settings,
        stores.embedder,
        stores.vector_store,
        stores.graph_store,
        sparse_embedder=stores.sparse_embedder,
        listeners=listeners,
    )
