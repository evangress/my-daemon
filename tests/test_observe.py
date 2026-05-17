# SPDX-License-Identifier: Apache-2.0
"""M4: observer pipeline — snapshot → analyze → letter → index → decay."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frontmatter
import pytest

from my_daemon.config import (
    AgentConfig,
    ConsolidationConfig,
    FeedbackConfig,
    GraphConfig,
    LLMConfig,
    Settings,
    SnapshotConfig,
)
from my_daemon.llm import LLMClient
from my_daemon.models import FeedbackEvent
from my_daemon.pipeline import agent_observe
from my_daemon.pipeline.agent_observe import run_observe
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores import AgentStateStore, FeedbackStore, GraphStore
from my_daemon.vault import VaultReader
from my_daemon.vault.chunker import chunk_note

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _tmp_vault(tmp_path: Path, vault_root: Path) -> Path:
    """Copy the read-only fixture vault into tmp_path so observe can write into Agent/."""
    dest = tmp_path / "vault"
    shutil.copytree(vault_root, dest)
    (dest / "Agent").mkdir(exist_ok=True)
    return dest


def _build_settings(
    tmp_path: Path,
    vault_root: Path,
    *,
    observer_enabled: bool = True,
) -> Settings:
    settings = Settings()
    settings.graph = GraphConfig(
        path=tmp_path / "graph.gpickle",
        manifest_path=tmp_path / "manifest.json",
    )
    settings.feedback = FeedbackConfig(db_path=tmp_path / "feedback.db")
    settings.snapshot = SnapshotConfig(dir=tmp_path / "snapshots", retention_days=7)
    settings.consolidation = ConsolidationConfig(out_dir=tmp_path / "consolidation")
    settings.vault.path = vault_root
    settings.agent = AgentConfig(
        enabled=True,
        observer_enabled=observer_enabled,
        observer_lookback_days=7,
        observer_max_communities=4,
        observer_prior_letters=4,
        observer_index_window=4,
        observer_model=None,  # → batch_model → llm.model
        folder_name="Agent",
    )
    settings.llm = LLMConfig(
        model="claude-sonnet-4-6",
        batch_model="claude-haiku-4-5",
        temperature=None,
    )
    return settings


def _populate_live_state(settings: Settings, vault_root: Path) -> None:
    """Seed the live graph + feedback DB with one candidate_selected event."""
    graph = GraphStore(path=settings.graph.path)
    for note in VaultReader(vault_root).read_all():
        chunks = chunk_note(note)
        graph.add_note(note, chunk_ids=[c.id for c in chunks])
    # Reinforce one edge and back-date its stamp so decay has work to do.
    apply_selection(
        graph,
        seed_note_path="Designing AI Memory.md",
        selected_note_path="Pullman Daemons.md",
    )
    old_iso = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    edge = graph.graph["note::Designing AI Memory.md"]["note::Pullman Daemons.md"]
    for d in edge.values():
        d["last_reinforced_at"] = old_iso
    graph.save()

    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    eid = feedback.log(
        FeedbackEvent(
            timestamp=datetime.now(UTC) - timedelta(hours=1),
            query="what is the daemon metaphor",
            retrieval_summary={
                "ranked": [
                    {
                        "chunk_id": "stub",
                        "note_path": "Pullman Daemons.md",
                        "seed_note_path": "Designing AI Memory.md",
                    }
                ]
            },
            answer="A companion.",
            latency_ms=12,
        )
    )
    feedback.attach_signal(
        eid, "candidate_selected",
        selected_rank=1, selected_chunk_id="stub", selected_note_path="Pullman Daemons.md",
    )


def _build_stub_llm(settings: Settings) -> LLMClient:
    """LLMClient that never reaches Anthropic — observer_letter is monkey-patched."""
    return LLMClient(settings.llm, api_key="not-used")


@pytest.fixture
def stub_letter(monkeypatch: pytest.MonkeyPatch):
    """Replace the observer LLM call with a deterministic stub.

    Records the kwargs it was called with so tests can assert the right
    inputs reached the prompt without invoking the network.
    """
    captured: dict = {}

    def _fake_observer_letter(client, **kwargs):
        captured.update(kwargs)
        return (
            "# What I noticed this week\n\n"
            f"Your snapshot `{kwargs['snapshot_id']}` shows a small but real "
            "philosophy cluster forming around the daemon metaphor.\n\n"
            "_Written from snapshot " + kwargs["snapshot_id"] + " by the test stub._"
        )

    monkeypatch.setattr(agent_observe, "observer_letter", _fake_observer_letter)
    return captured


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_observe_writes_letter_and_index(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    llm = _build_stub_llm(settings)

    stats = run_observe(
        settings, state, feedback, graph, llm,
        snapshot_id=None, dry_run=False, include_qdrant=False,
    )

    assert stats.letter_path is not None and stats.letter_path.is_file()
    assert stats.letter_path.name.startswith("observer-")
    assert stats.run_id is not None
    assert stats.communities_seen >= 0
    assert stats.events_replayed == 1

    # Rolling index lives at <vault>/Agent/observer.md and points at the new letter.
    index_path = vault / "Agent" / "observer.md"
    assert index_path.is_file()
    index_text = index_path.read_text(encoding="utf-8")
    assert stats.letter_path.name in index_text

    # The letter file carries frontmatter provenance.
    post = frontmatter.loads(stats.letter_path.read_text(encoding="utf-8"))
    assert post.metadata.get("daemon") == "observer"
    assert post.metadata.get("snapshot_id") == stats.snapshot_id


def test_observe_dry_run_writes_nothing(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    llm = _build_stub_llm(settings)

    # Snapshot the live graph bytes before, so we can assert it didn't change.
    pre = settings.graph.path.read_bytes()

    stats = run_observe(
        settings, state, feedback, graph, llm,
        snapshot_id=None, dry_run=True, include_qdrant=False,
    )

    assert stats.letter_path is None
    assert stats.edges_decayed == 0
    assert any("dry-run" in n for n in stats.notes)
    # Live graph untouched.
    assert settings.graph.path.read_bytes() == pre
    # Nothing under Agent/.
    assert not (vault / "Agent" / "observer.md").exists()
    today = datetime.now(UTC).date().isoformat()
    assert not (vault / "Agent" / f"observer-{today}.md").exists()
    # Run still recorded (so the user can see we attempted).
    runs = state.recent_observer_runs(limit=5)
    assert runs and runs[0]["dry_run"] == 1
    assert runs[0]["snapshot_id"] == stats.snapshot_id


def test_observe_decays_live_graph(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    pre_edge = next(iter(graph.graph["note::Designing AI Memory.md"][
        "note::Pullman Daemons.md"].values()))
    pre_weight = pre_edge["weight"]
    assert pre_weight > 1.0  # populated_live_state reinforced it

    llm = _build_stub_llm(settings)
    stats = run_observe(
        settings, state, feedback, graph, llm,
        dry_run=False, include_qdrant=False,
    )

    assert stats.edges_decayed >= 1
    # Reload from disk to confirm decay actually persisted.
    reloaded = GraphStore(path=settings.graph.path)
    reloaded.load()
    post_edge = next(iter(reloaded.graph["note::Designing AI Memory.md"][
        "note::Pullman Daemons.md"].values()))
    assert post_edge["weight"] < pre_weight


def test_observe_reuses_existing_snapshot(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    from my_daemon.stores import create_snapshot

    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    bundle = create_snapshot(settings, include_qdrant=False)

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    llm = _build_stub_llm(settings)

    stats = run_observe(
        settings, state, feedback, graph, llm,
        snapshot_id=bundle.id, dry_run=False, include_qdrant=False,
    )

    assert stats.snapshot_id == bundle.id
    # The settings.snapshot.dir should contain exactly one bundle (no new one created).
    assert len(list(settings.snapshot.dir.iterdir())) == 1


def test_observe_missing_snapshot_raises(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    llm = _build_stub_llm(settings)

    with pytest.raises(FileNotFoundError):
        run_observe(
            settings, state, feedback, graph, llm,
            snapshot_id="2099-01-01T00-00-00Z", include_qdrant=False,
        )


def test_observe_feeds_prior_letters_to_prompt(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    """A pre-existing observer-<date>.md should reach the LLM as a prior letter."""
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    # Plant a prior letter from a few days ago.
    prior_date = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
    prior_path = vault / "Agent" / f"observer-{prior_date}.md"
    prior_path.write_text(
        "---\ndaemon: observer\n---\n\nLast week you were thinking about Austin.\n",
        encoding="utf-8",
    )

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    llm = _build_stub_llm(settings)

    run_observe(settings, state, feedback, graph, llm, dry_run=False, include_qdrant=False)

    priors = stub_letter["prior_letters"]
    assert any("Austin" in body for body in priors)


def test_observe_same_day_rerun_snapshots_prior_letter(
    tmp_path: Path, vault_root: Path, stub_letter: dict
) -> None:
    """Re-running on the same day overwrites today's letter but backs the prior up."""
    vault = _tmp_vault(tmp_path, vault_root)
    settings = _build_settings(tmp_path, vault)
    _populate_live_state(settings, vault)

    state = AgentStateStore(db_path=settings.feedback.db_path)
    feedback = FeedbackStore(db_path=settings.feedback.db_path)
    graph = GraphStore(path=settings.graph.path)
    graph.load()
    llm = _build_stub_llm(settings)

    first = run_observe(settings, state, feedback, graph, llm, dry_run=False, include_qdrant=False)
    second = run_observe(settings, state, feedback, graph, llm, dry_run=False, include_qdrant=False)

    assert first.letter_path == second.letter_path  # same date → same filename
    # A backup of the prior body should now live under Agent/backups/.
    backups = list((vault / "Agent" / "backups").rglob("observer-*.md.*"))
    assert backups, "expected a snapshot of the previous letter under Agent/backups/"
