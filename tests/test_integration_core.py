# SPDX-License-Identifier: Apache-2.0
"""Core service layer (PLAN-HERMES H1/H2): recall shape, capture+recall, dreams,
endorse, neighbors, write-back gating — all without Qdrant or Anthropic."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_daemon.config import FeedbackConfig, GraphConfig, HermesConfig, Settings
from my_daemon.integration.core import DaemonCore
from my_daemon.models import (
    Chunk,
    FeedbackEvent,
    RetrievalResult,
    RetrievedChunk,
)
from my_daemon.pipeline.query import QueryResponse
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores import FeedbackStore, GraphStore
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note
from my_daemon.vault.identity import derive_path_uuid as _u

# ---------------------------------------------------------------------------
# fakes — stand in for the embedder + Qdrant so tests stay offline
# ---------------------------------------------------------------------------


class FakeEmbedder:
    dimension = 3

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]

    def encode_one(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0]


class FakeVectorStore:
    def __init__(self) -> None:
        self.upserted: list[str] = []
        self.deleted: list[str] = []

    def ensure_collection(self) -> None:
        pass

    def upsert(self, chunks, vectors, sparse_vectors=None) -> None:
        self.upserted.extend(c.note_path for c in chunks)

    def delete_by_note(self, note_path: str) -> None:
        self.deleted.append(note_path)

    def count(self) -> int:
        return len(set(self.upserted))


class FakeLLM:
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tmp_vault(tmp_path: Path, vault_root: Path) -> Path:
    dest = tmp_path / "vault"
    shutil.copytree(vault_root, dest)
    (dest / "Agent").mkdir(exist_ok=True)
    return dest


def _settings(tmp_path: Path, vault: Path, *, write_back: bool = False) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.graph = GraphConfig(path=tmp_path / "graph.gpickle", manifest_path=tmp_path / "manifest.json")
    s.feedback = FeedbackConfig(db_path=tmp_path / "feedback.db")
    s.hermes = HermesConfig(
        provider_enabled=True,
        allow_write_back=write_back,
        capture_requires_confirmation=True,
        recall_top_k=8,
    )
    return s


def _seeded_graph(settings: Settings, vault: Path) -> GraphStore:
    graph = GraphStore(path=settings.graph.path)
    for note in VaultReader(vault).read_all():
        chunks = chunk_note(note)
        graph.add_note(note, chunk_ids=[c.id for c in chunks])
    graph.save()
    return graph


def _core(settings: Settings, vault: Path) -> DaemonCore:
    graph = _seeded_graph(settings, vault)
    return DaemonCore(
        settings,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        graph_store=graph,
        feedback_store=FeedbackStore(db_path=settings.feedback.db_path),
        llm_client=FakeLLM(),
    )


def _fake_retrieval(*, two: bool = False) -> RetrievalResult:
    rc1 = RetrievedChunk(
        chunk=Chunk(
            id="c1",
            note_path="Pullman Daemons.md",
            heading_path=["Daemons"],
            text="A daemon is an external manifestation of a person's soul.",
            chunk_index=0,
        ),
        vector_score=0.9,
        graph_distance=0,
        combined_score=0.9,
    )
    ranked = [rc1]
    if two:
        rc2 = RetrievedChunk(
            chunk=Chunk(
                id="c2",
                note_path="Socratic Daemon.md",
                heading_path=["Socrates"],
                text="Socrates spoke of an inner daimonion, a warning voice.",
                chunk_index=0,
            ),
            vector_score=0.7,
            graph_distance=0,
            combined_score=0.7,
        )
        ranked.append(rc2)
    return RetrievalResult(query="what is a daemon", seeds=ranked, expanded=[], ranked=ranked)


# ---------------------------------------------------------------------------
# recall / recall_block (engine.ask monkeypatched — no Qdrant)
# ---------------------------------------------------------------------------


def test_recall_shape_and_feedback_id(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault)
    core = _core(settings, vault)

    # Log a real feedback event so feedback_event_id is genuine.
    eid = core.feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="what is a daemon",
            retrieval_summary={},
            answer="",
            latency_ms=5,
        )
    )
    core.engine.ask = lambda q, synthesize=False: QueryResponse(  # type: ignore[method-assign]
        answer="", retrieval=_fake_retrieval(), feedback_event_id=eid, latency_ms=5,
    )

    data = core.recall("what is a daemon")
    assert data["feedback_event_id"] == eid
    assert data["candidates"], "expected at least one candidate"
    top = data["candidates"][0]
    assert {"rank", "chunk_id", "note_path", "heading_path", "score", "preview", "seed_note_path"} <= top.keys()
    assert top["rank"] == 1
    assert top["note_path"] == "Pullman Daemons.md"


def test_recall_block_is_cited_and_budget_capped(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault)
    core = _core(settings, vault)
    core.engine.ask = lambda q, synthesize=False: QueryResponse(  # type: ignore[method-assign]
        answer="", retrieval=_fake_retrieval(two=True), feedback_event_id=1, latency_ms=5,
    )

    block = core.recall_block("what is a daemon", budget_chars=4000)
    assert "Pullman Daemons.md" in block.text  # cited by note path
    assert "Socratic Daemon.md" in block.text
    assert block.feedback_event_id == 1
    assert len(block.candidates) == 2

    # A tiny budget keeps the first candidate but drops the second.
    tiny = core.recall_block("what is a daemon", budget_chars=80)
    assert "Pullman Daemons.md" in tiny.text
    assert "Socratic Daemon.md" not in tiny.text


# ---------------------------------------------------------------------------
# remember (write-back) — capture is provenance-stamped and recallable
# ---------------------------------------------------------------------------


def test_remember_writes_provenance_capture_and_ingests(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault, write_back=True)
    core = _core(settings, vault)

    result = core.remember(
        "Evan prefers long-term, human-meaningful design over quick wins.",
        title="Evan design value",
        source="hermes",
        confirmed=False,
        session_id="sess-1",
    )
    assert result["ok"] is True
    assert result["status"] == "unconfirmed"  # capture_requires_confirmation=True, not confirmed
    path = Path(result["path"])
    assert path.is_file()
    assert path.is_relative_to(vault / "Conversations" / "hermes")

    import frontmatter

    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    assert post["source"] == "hermes"
    assert post["status"] == "unconfirmed"
    assert post["session_id"] == "sess-1"

    # The note was incrementally ingested: vector upsert + graph node + manifest.
    assert any("Conversations/hermes" in p for p in core.vector_store.upserted)
    assert core.graph.chunk_ids_for(_u(result["rel_path"]))


def test_remember_noop_when_write_back_disabled(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault, write_back=False)
    core = _core(settings, vault)

    result = core.remember("anything", title="x")
    assert result["ok"] is False
    assert "write-back disabled" in result["reason"]
    assert not (vault / "Conversations").exists()


def test_remember_confirmed_flag_sets_status(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault, write_back=True)
    core = _core(settings, vault)
    result = core.remember("A confirmed fact.", confirmed=True)
    assert result["status"] == "confirmed"


def test_remember_refuses_path_escape(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault, write_back=True)
    settings.hermes.capture_folder = "../../etc"
    core = _core(settings, vault)
    with pytest.raises(ValueError, match="escapes vault"):
        core.remember("nope")


# ---------------------------------------------------------------------------
# endorse (soft reinforcement reuses daemon-select internals)
# ---------------------------------------------------------------------------


def test_endorse_reinforces_seed_to_note_path(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault, write_back=True)
    core = _core(settings, vault)

    eid = core.feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC),
            query="daemon metaphor",
            retrieval_summary={
                "ranked": [
                    {
                        "chunk_id": "stub",
                        "note_path": "Pullman Daemons.md",
                        "note_uuid": _u("Pullman Daemons.md"),
                        "seed_note_path": "Designing AI Memory.md",
                        "seed_note_uuid": _u("Designing AI Memory.md"),
                    }
                ]
            },
            answer="",
            latency_ms=3,
        )
    )
    result = core.endorse(eid, 1)
    assert result["ok"] is True
    assert result["edges_reinforced"] >= 1
    assert result["total_delta"] > 0

    # The feedback row now carries the selection signal.
    event = core.feedback.get(eid)
    assert event is not None and event.signal == "candidate_selected"
    assert event.selected_note_uuid == _u("Pullman Daemons.md")
    assert event.selected_note_path == "Pullman Daemons.md"  # display breadcrumb


def test_endorse_noop_when_write_back_disabled(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault, write_back=False)
    core = _core(settings, vault)
    assert core.endorse(1, 1)["ok"] is False


# ---------------------------------------------------------------------------
# dreams / neighbors / status
# ---------------------------------------------------------------------------


def _write_letter(vault: Path, day: str, body: str) -> None:
    (vault / "Agent").mkdir(exist_ok=True)
    (vault / "Agent" / f"observer-{day}.md").write_text(
        f"---\ndaemon: observer\n---\n\n{body}\n", encoding="utf-8"
    )


def test_dreams_return_newest_first(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault)
    core = _core(settings, vault)

    assert core.latest_dream() is None  # degrades gracefully with no letters

    _write_letter(vault, "2026-06-01", "First reflection.")
    _write_letter(vault, "2026-06-04", "Most recent reflection.")

    latest = core.latest_dream()
    assert latest is not None and latest["date"] == "2026-06-04"
    assert "Most recent" in latest["body"]
    assert [d["date"] for d in core.dreams(limit=5)] == ["2026-06-04", "2026-06-01"]
    assert core.read_dream("2026-06-01")["body"].startswith("First")  # type: ignore[index]
    assert core.read_dream("2099-01-01") is None


def test_neighbors_and_status(tmp_path: Path, vault_root: Path) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _settings(tmp_path, vault)
    core = _core(settings, vault)

    # Reinforce one edge so a neighbor is reachable + the graph has warmth.
    apply_selection(
        core.graph,
        seed_note_uuid=_u("Designing AI Memory.md"),
        selected_note_uuid=_u("Pullman Daemons.md"),
    )
    n = core.neighbors("Designing AI Memory.md", depth=2)
    assert n["note_path"] == "Designing AI Memory.md"
    # Asserting the shape only is what let this silently return {} for a whole
    # release after the identity cutover. Assert real values.
    assert n["ok"] is True
    returned = {nb["note_path"] for nb in n["neighbors"]}
    assert "Pullman Daemons.md" in returned
    assert all(p.endswith(".md") for p in returned), "paths, not uuids"


def test_neighbors_of_an_unknown_path_says_so(tmp_path: Path, vault_root: Path) -> None:
    """An empty list is indistinguishable from a bug. Be explicit."""
    settings = _settings(tmp_path, vault_root)
    core = _core(settings, _tmp_vault(tmp_path, vault_root))

    n = core.neighbors("No Such Note.md")

    assert n["ok"] is False
    assert n["neighbors"] == []

    status = core.status()
    assert status["notes"] >= 1
    assert status["write_back"] is False
    assert status["vector_chunks"] == 0  # FakeVectorStore counts upserts; none yet
