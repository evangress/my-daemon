# SPDX-License-Identifier: Apache-2.0
"""Argument plumbing for the Typer app.

`cli.py` is the widest untested surface in the project, and both of the
commands that close the feedback loop were dead there: `select` passed a kwarg
`apply_selection` lost in the path→UUID cutover, and `ask` forwarded Typer's
`OptionInfo` defaults as if they were values, so synthesis silently never ran.

Everything here stays offline: Qdrant and the embedder are hand-written fakes
(house style — no MagicMock), while the graph, the feedback DB and the ledger
are the real thing on tmp paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.config import Settings
from my_daemon.models import FeedbackEvent
from my_daemon.stores import FeedbackStore, GraphStore
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note
from my_daemon.vault.identity import derive_path_uuid as _u

runner = CliRunner()

DESIGNING = _u("Designing AI Memory.md")
PULLMAN = _u("Pullman Daemons.md")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeEmbedder:
    dimension = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    def encode_one(self, text: str) -> list[float]:
        return [0.0] * 4


class FakeQdrantClient:
    def scroll(self, **kwargs):  # noqa: ANN003
        return [], None


class FakeVectorStore:
    collection = "chunks"

    def __init__(self, hits: list[dict] | None = None) -> None:
        self.hits = hits or []
        self.searches: list[int] = []

    def search(self, vector, top_k=8):  # noqa: ANN001
        self.searches.append(top_k)
        return self.hits

    def hybrid_search(self, vector, sparse_vector, top_k=8):  # noqa: ANN001
        self.searches.append(top_k)
        return self.hits

    def count(self) -> int:
        return len(self.hits)

    def _client_(self) -> FakeQdrantClient:
        return FakeQdrantClient()


class FakeLLM:
    """Records every synthesis so a test can prove the LLM path was taken."""

    def __init__(self, answer: str = "the daemon speaks") -> None:
        self.answer = answer
        self.queries: list[str] = []

    def synthesize(self, query: str, chunks, memories=None) -> str:  # noqa: ANN001
        self.queries.append(query)
        return self.answer


class FakeStores:
    """Stand-in for `integration.wiring.Stores` — same attribute names."""

    def __init__(self, settings: Settings, graph_store: GraphStore) -> None:
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.sparse_embedder = None
        self.vector_store = FakeVectorStore()
        self.graph_store = graph_store
        self.feedback_store = FeedbackStore(db_path=settings.feedback.db_path)
        self.llm = FakeLLM()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _build_vault_graph(vault_root: Path, path: Path) -> GraphStore:
    store = GraphStore(path=path)
    for note in VaultReader(vault_root).read_all():
        store.add_note(note, chunk_ids=[c.id for c in chunk_note(note)])
    store.save()
    return store


@pytest.fixture
def settings(tmp_path: Path, vault_root: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A Settings whose every path is disposable, wired into `_load()`."""
    s = Settings()
    s.vault.path = vault_root
    s.graph.path = tmp_path / "graph.gpickle"
    s.graph.manifest_path = tmp_path / "manifest.json"
    s.feedback.db_path = tmp_path / "state.db"
    monkeypatch.setattr("my_daemon.cli._load", lambda: s)
    return s


@pytest.fixture
def vault_graph(settings: Settings, vault_root: Path) -> GraphStore:
    return _build_vault_graph(vault_root, settings.graph.path)


@pytest.fixture
def stores(settings: Settings, vault_graph: GraphStore, monkeypatch: pytest.MonkeyPatch):
    """Replace the one construction path with offline fakes."""
    fake = FakeStores(settings, vault_graph)
    monkeypatch.setattr("my_daemon.cli.build_stores", lambda s, **kw: fake)
    return fake


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_prints_the_installed_version():
    from my_daemon import __version__

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert __version__ in result.output


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_writes_a_config_with_the_given_vault_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(
        "vault:\n  path: ~/Documents/Obsidian/MyVault\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["init", "--vault", "/srv/notes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == (
        "vault:\n  path: /srv/notes\n"
    )


def test_init_refuses_to_clobber_an_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text("vault:\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "mine\n"


def test_init_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(
        "vault:\n  path: ~/Documents/Obsidian/MyVault\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text("mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--vault", "/srv/notes", "--force"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == (
        "vault:\n  path: /srv/notes\n"
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_graph_size_and_vault_path(
    settings: Settings, vault_graph: GraphStore, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("my_daemon.cli._build_embedder", lambda s: FakeEmbedder())
    monkeypatch.setattr(
        "my_daemon.cli._build_vector_store", lambda s, dim: FakeVectorStore([{"a": 1}])
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    stats = vault_graph.stats()
    assert stats.note_count == 6
    assert "graph notes 6" in _squash(result.output)
    assert "vector chunks 1" in _squash(result.output)


def test_status_survives_an_unreachable_qdrant(
    settings: Settings, vault_graph: GraphStore, monkeypatch: pytest.MonkeyPatch
):
    def _explode(s, dim):  # noqa: ANN001
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr("my_daemon.cli._build_embedder", lambda s: FakeEmbedder())
    monkeypatch.setattr("my_daemon.cli._build_vector_store", _explode)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "unavailable" in result.output
    assert "qdrant is down" in _squash(result.output)


_BOX_CHARS = "┏┓┗┛━─│┃┡┩┠┨╇┼├┤┬┴╭╮╰╯╺╸"


def _squash(text: str) -> str:
    """Rich pads with runs of spaces and draws boxes; normalise for matching."""
    stripped = text.translate({ord(c): " " for c in _BOX_CHARS})
    return " ".join(stripped.split())


# ---------------------------------------------------------------------------
# graph stats
# ---------------------------------------------------------------------------


def test_graph_stats_ranks_notes_and_tags(settings: Settings, vault_graph: GraphStore):
    result = runner.invoke(app, ["graph", "stats"])

    assert result.exit_code == 0, result.output
    assert "Top notes by PageRank" in _squash(result.output)
    assert "Top tags by degree" in _squash(result.output)


# ---------------------------------------------------------------------------
# query / ask
# ---------------------------------------------------------------------------


def test_query_synthesizes_by_default(stores: FakeStores):
    result = runner.invoke(app, ["query", "what is a daemon"])

    assert result.exit_code == 0, result.output
    assert stores.llm.queries == ["what is a daemon"]
    assert "the daemon speaks" in _squash(result.output)


def test_query_no_llm_skips_synthesis(stores: FakeStores):
    result = runner.invoke(app, ["query", "what is a daemon", "--no-llm"])

    assert result.exit_code == 0, result.output
    assert stores.llm.queries == []
    assert "the daemon speaks" not in result.output


def test_query_is_quiet_without_verbose(stores: FakeStores):
    result = runner.invoke(app, ["query", "what is a daemon"])

    assert "seeds=" not in result.output


def test_query_verbose_reports_the_retrieval_shape(stores: FakeStores):
    result = runner.invoke(app, ["query", "what is a daemon", "-v"])

    assert result.exit_code == 0, result.output
    assert "seeds=0 expanded=0" in _squash(result.output)


def test_ask_synthesizes_like_query(stores: FakeStores):
    """The bug: `ask` forwarded Typer's OptionInfo defaults, which are truthy,
    so `synthesize` resolved False and the LLM was never called."""
    result = runner.invoke(app, ["ask", "what is a daemon"])

    assert result.exit_code == 0, result.output
    assert stores.llm.queries == ["what is a daemon"]
    assert "the daemon speaks" in _squash(result.output)


def test_ask_is_quiet_by_default(stores: FakeStores):
    """The same bug made `verbose` resolve True on every `ask`."""
    result = runner.invoke(app, ["ask", "what is a daemon"])

    assert "seeds=" not in result.output


def test_ask_accepts_the_same_flags_as_query(stores: FakeStores):
    result = runner.invoke(app, ["ask", "what is a daemon", "--no-llm", "-v"])

    assert result.exit_code == 0, result.output
    assert stores.llm.queries == []
    assert "seeds=0 expanded=0" in _squash(result.output)


# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------


def _log_two_candidate_query(settings: Settings) -> int:
    """A feedback row shaped exactly like `build_retrieval_summary` writes it."""
    store = FeedbackStore(db_path=settings.feedback.db_path)
    return store.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="what is a daemon",
            retrieval_summary={
                "ranked": [
                    {
                        "chunk_id": "designing-0",
                        "note_uuid": DESIGNING,
                        "note_path": "Designing AI Memory.md",
                        "score": 0.9,
                        "graph_distance": 0,
                        "seed_chunk_id": "designing-0",
                        "seed_note_uuid": DESIGNING,
                        "seed_note_path": "Designing AI Memory.md",
                    },
                    {
                        "chunk_id": "pullman-0",
                        "note_uuid": PULLMAN,
                        "note_path": "Pullman Daemons.md",
                        "score": 0.4,
                        "graph_distance": 1.0,
                        "seed_chunk_id": "designing-0",
                        "seed_note_uuid": DESIGNING,
                        "seed_note_path": "Designing AI Memory.md",
                    },
                ],
                "seed_count": 1,
                "expanded_count": 1,
            },
            answer="",
            latency_ms=12,
        )
    )


def _wikilink_weights(path: Path) -> list[float]:
    reloaded = GraphStore(path=path)
    reloaded.load()
    edge = reloaded.graph[f"note::{DESIGNING}"][f"note::{PULLMAN}"]
    return [d["weight"] for d in edge.values()]


def test_select_reinforces_the_path_to_the_picked_note(settings: Settings, vault_graph: GraphStore):
    """The bug: `select` passed a `selected_note_path=` kwarg `apply_selection`
    no longer has, so every invocation died with a TypeError."""
    feedback_id = _log_two_candidate_query(settings)
    assert _wikilink_weights(settings.graph.path) == [1.0]

    result = runner.invoke(app, ["select", str(feedback_id), "2"])

    assert result.exit_code == 0, result.output
    # alpha=0.5 on a virgin edge, persisted back to disk.
    assert _wikilink_weights(settings.graph.path) == [1.5]


def test_select_attaches_the_signal_to_the_feedback_row(
    settings: Settings, vault_graph: GraphStore
):
    feedback_id = _log_two_candidate_query(settings)

    runner.invoke(app, ["select", str(feedback_id), "2"])

    event = FeedbackStore(db_path=settings.feedback.db_path).get(feedback_id)
    assert event.signal == "candidate_selected"
    assert event.selected_rank == 2
    assert event.selected_chunk_id == "pullman-0"
    assert event.selected_note_uuid == PULLMAN
    assert event.selected_note_path == "Pullman Daemons.md"


def test_select_reports_the_reinforced_path(settings: Settings, vault_graph: GraphStore):
    feedback_id = _log_two_candidate_query(settings)

    result = runner.invoke(app, ["select", str(feedback_id), "2"])

    assert result.exit_code == 0, result.output
    # Both directions of the wikilink pair between the two notes.
    assert "Edges reinforced: 2" in _squash(result.output)
    assert f"{DESIGNING} → {PULLMAN}" in _squash(result.output)


def test_selecting_the_seed_itself_reinforces_nothing(settings: Settings, vault_graph: GraphStore):
    feedback_id = _log_two_candidate_query(settings)

    result = runner.invoke(app, ["select", str(feedback_id), "1"])

    assert result.exit_code == 0, result.output
    assert "Edges reinforced: 0" in _squash(result.output)
    assert _wikilink_weights(settings.graph.path) == [1.0]


def test_select_rejects_an_unknown_feedback_id(settings: Settings, vault_graph: GraphStore):
    result = runner.invoke(app, ["select", "999", "1"])

    assert result.exit_code == 1
    assert "No feedback event with id 999" in _squash(result.output)


def test_select_rejects_an_out_of_range_rank(settings: Settings, vault_graph: GraphStore):
    feedback_id = _log_two_candidate_query(settings)

    result = runner.invoke(app, ["select", str(feedback_id), "7"])

    assert result.exit_code == 1
    assert "out of range" in _squash(result.output)
    assert _wikilink_weights(settings.graph.path) == [1.0]


def test_invalid_api_key_becomes_one_actionable_line(stores: FakeStores):
    """A 401 from Anthropic must not arrive as a raw traceback.

    Found live 2026-07-27: a placeholder key in .env passed doctor's
    presence check, then `daemon ask` dumped an AuthenticationError
    traceback from inside the SDK. Same contract as Qdrant-down: one
    actionable line, exit 1.
    """
    import httpx
    from anthropic import AuthenticationError

    def raise_401(query, chunks, memories=None):  # noqa: ANN001
        raise AuthenticationError(
            message="invalid x-api-key",
            response=httpx.Response(
                401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            body={"error": {"type": "authentication_error"}},
        )

    stores.llm.synthesize = raise_401
    result = runner.invoke(app, ["ask", "what do I believe?"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "rejected" in result.output
