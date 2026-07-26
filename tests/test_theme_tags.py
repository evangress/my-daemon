# SPDX-License-Identifier: Apache-2.0
"""Theme tags: proposed, decided by the user, then written through the safe path."""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.config import Settings
from my_daemon.models import NoteRecord
from my_daemon.pipeline.theme_tags import (
    THEME_TAG_PREFIX,
    apply_decision,
    propose_theme_tags,
)
from my_daemon.stores.registry import NoteRegistry
from my_daemon.stores.themes import ThemeStore

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("---\ntags: [memory]\n---\n\n# A\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(vault: Path, tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.feedback.db_path = tmp_path / "state.db"
    return s


@pytest.fixture
def registry(settings: Settings) -> NoteRegistry:
    reg = NoteRegistry(db_path=settings.feedback.db_path)
    reg.upsert(NoteRecord(uuid=A, rel_path="A.md", title="A"))
    reg.upsert(NoteRecord(uuid=B, rel_path="B.md", title="B"))
    return reg


@pytest.fixture
def themes(settings: Settings) -> ThemeStore:
    return ThemeStore(db_path=settings.feedback.db_path)


def _theme(themes: ThemeStore, *, status="accepted") -> int:
    theme_id = themes.create(
        slug="why-projects-stall", label="Why projects stall",
        centroid={A: 0.9, B: 0.4}, snapshot_id="s1",
    )
    themes.set_notes(theme_id, {A: 0.9, B: 0.4})
    themes.set_status(theme_id, status)
    return theme_id


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------


def test_an_accepted_theme_proposes_tags_for_its_notes(themes, registry):
    theme_id = _theme(themes)

    propose_theme_tags(themes)

    pending = themes.pending_proposals()
    assert {p.note_uuid for p in pending} == {A, B}
    assert all(p.theme_id == theme_id for p in pending)


def test_a_proposed_theme_proposes_nothing_until_accepted(themes, registry):
    _theme(themes, status="proposed")

    propose_theme_tags(themes)

    assert themes.pending_proposals() == []


def test_tags_carry_the_theme_prefix(themes, registry):
    _theme(themes)

    propose_theme_tags(themes)

    assert all(p.tag.startswith(THEME_TAG_PREFIX) for p in themes.pending_proposals())


def test_proposing_twice_does_not_duplicate(themes, registry):
    _theme(themes)

    propose_theme_tags(themes)
    propose_theme_tags(themes)

    assert len(themes.pending_proposals()) == 2


def test_a_rejected_tag_is_never_re_proposed(themes, registry):
    """Rejections are remembered — the daemon does not nag."""
    _theme(themes)
    propose_theme_tags(themes)
    for proposal in themes.pending_proposals():
        themes.decide(proposal.id, "rejected")

    propose_theme_tags(themes)

    assert themes.pending_proposals() == []


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_accepting_writes_the_tag_into_frontmatter(themes, registry, settings, vault):
    _theme(themes)
    propose_theme_tags(themes)
    proposal = next(p for p in themes.pending_proposals() if p.note_uuid == A)

    apply_decision(settings, themes, registry, proposal.id, "accepted", grace_minutes=0)

    assert "theme/why-projects-stall" in (vault / "A.md").read_text()


def test_accepting_preserves_the_existing_tag_style(themes, registry, settings, vault):
    _theme(themes)
    propose_theme_tags(themes)
    proposal = next(p for p in themes.pending_proposals() if p.note_uuid == A)

    apply_decision(settings, themes, registry, proposal.id, "accepted", grace_minutes=0)

    assert "tags: [memory, theme/why-projects-stall]" in (vault / "A.md").read_text()


def test_rejecting_writes_nothing(themes, registry, settings, vault):
    _theme(themes)
    propose_theme_tags(themes)
    proposal = next(p for p in themes.pending_proposals() if p.note_uuid == A)
    before = (vault / "A.md").read_bytes()

    apply_decision(settings, themes, registry, proposal.id, "rejected", grace_minutes=0)

    assert (vault / "A.md").read_bytes() == before
    assert themes.pending_proposals() == [
        p for p in themes.pending_proposals() if p.note_uuid == B
    ]


def test_a_decided_proposal_leaves_the_pending_queue(themes, registry, settings):
    _theme(themes)
    propose_theme_tags(themes)
    proposal = themes.pending_proposals()[0]

    apply_decision(settings, themes, registry, proposal.id, "accepted", grace_minutes=0)

    assert proposal.id not in {p.id for p in themes.pending_proposals()}


def test_a_note_the_daemon_may_not_write_records_why(themes, registry, settings, vault):
    (vault / "A.md").write_text(
        "---\ndaemon: ignore\n---\n\n# A\n", encoding="utf-8"
    )
    _theme(themes)
    propose_theme_tags(themes)
    proposal = next(p for p in themes.pending_proposals() if p.note_uuid == A)

    result = apply_decision(
        settings, themes, registry, proposal.id, "accepted", grace_minutes=0
    )

    assert result.changed is False
    assert "ignore" in result.reason


# ---------------------------------------------------------------------------
# Breaking the self-reinforcement loop
# ---------------------------------------------------------------------------


def test_theme_tag_edges_are_excluded_from_clustering():
    """Otherwise: theme tags -> tag edges -> different expansion -> different
    fingerprints -> new themes. The system converges on its own reflection."""
    from my_daemon.analysis.themes import is_self_referential_tag

    assert is_self_referential_tag("theme/why-projects-stall") is True
    assert is_self_referential_tag("philosophy") is False


# ---------------------------------------------------------------------------
# The review CLI
# ---------------------------------------------------------------------------


def test_accepting_a_theme_queues_its_tag_proposals(
    themes, registry, settings, monkeypatch
):
    from typer.testing import CliRunner

    from my_daemon.cli import app

    theme_id = _theme(themes, status="proposed")
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    result = CliRunner().invoke(app, ["themes", "accept", str(theme_id)])

    assert result.exit_code == 0, result.output
    assert len(themes.pending_proposals()) == 2


def test_accepting_a_theme_locks_its_label(themes, registry, settings, monkeypatch):
    from typer.testing import CliRunner

    from my_daemon.cli import app

    theme_id = _theme(themes, status="proposed")
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    CliRunner().invoke(app, ["themes", "accept", str(theme_id), "--label", "Mine"])
    themes.set_label(theme_id, label="the observer's new idea", summary="")

    assert themes.get(theme_id).label == "Mine"


def test_review_accept_all_writes_every_pending_tag(
    themes, registry, settings, vault, monkeypatch
):
    from typer.testing import CliRunner

    from my_daemon.cli import app

    _theme(themes)
    propose_theme_tags(themes)
    settings.agent.write_grace_minutes = 0
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    result = CliRunner().invoke(app, ["themes", "review", "--accept-all"])

    assert result.exit_code == 0, result.output
    assert "theme/why-projects-stall" in (vault / "A.md").read_text()
    assert themes.pending_proposals() == []


def test_review_reject_all_writes_nothing(
    themes, registry, settings, vault, monkeypatch
):
    from typer.testing import CliRunner

    from my_daemon.cli import app

    _theme(themes)
    propose_theme_tags(themes)
    before = (vault / "A.md").read_bytes()
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)

    CliRunner().invoke(app, ["themes", "review", "--reject-all"])

    assert (vault / "A.md").read_bytes() == before
    assert themes.pending_proposals() == []
