# SPDX-License-Identifier: Apache-2.0
"""Turn accepted themes into tags on the notes that define them.

Proposed, never auto-applied. The daemon's judgement about what you have been
circling is worth surfacing; writing it into your own prose is your call.
"""

from __future__ import annotations

from my_daemon.config import Settings
from my_daemon.models import THEME_TAG_PREFIX
from my_daemon.stores.registry import NoteRegistry
from my_daemon.stores.themes import ThemeStore
from my_daemon.vault.writer import WriteResult, add_frontmatter_list_values_textual

# Below this centroid weight a note is on the fringe of the theme, not part of
# what defines it.
_MIN_NOTE_WEIGHT = 0.15


def theme_tag(slug: str) -> str:
    return f"{THEME_TAG_PREFIX}{slug}"


def propose_theme_tags(themes: ThemeStore, *, min_weight: float = _MIN_NOTE_WEIGHT) -> int:
    """Queue tag proposals for every accepted theme. Returns how many were new.

    Only *accepted* themes propose. A theme the user has not yet blessed has no
    business suggesting edits to their notes.
    """

    proposed = 0
    for theme in themes.all():
        if theme.status != "accepted":
            continue
        tag = theme_tag(theme.slug)
        for note_uuid, weight in themes.notes_for(theme.id).items():
            if weight < min_weight:
                continue
            themes.propose_tag(theme.id, note_uuid, tag)
            proposed += 1
    return proposed


def apply_decision(
    settings: Settings,
    themes: ThemeStore,
    registry: NoteRegistry,
    proposal_id: int,
    decision: str,
    *,
    grace_minutes: int = 2,
) -> WriteResult:
    """Record a decision, and on acceptance write the tag through the safe path."""

    proposal = next(
        (p for p in themes.pending_proposals(limit=10_000) if p.id == proposal_id), None
    )
    if proposal is None:
        return WriteResult(path=settings.vault.path, changed=False, reason="unknown proposal")

    if decision != "accepted":
        themes.decide(proposal_id, decision)
        return WriteResult(path=settings.vault.path, changed=False, reason=decision)

    record = registry.get(proposal.note_uuid)
    if record is None:
        themes.decide(proposal_id, "accepted", "note not in registry")
        return WriteResult(
            path=settings.vault.path, changed=False, reason="note not in registry"
        )

    vault_root = settings.vault.path.expanduser().resolve()
    result = add_frontmatter_list_values_textual(
        vault_root / record.rel_path,
        "tags",
        [proposal.tag],
        vault_root=vault_root,
        agent_folder=settings.agent.folder_name,
        grace_minutes=grace_minutes,
    )
    # Decided either way: a note we could not write is a fact to remember, not
    # a proposal to keep re-offering.
    themes.decide(proposal_id, "accepted", result.reason)
    return result
