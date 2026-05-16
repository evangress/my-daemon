"""Daily extractor: read recently-touched notes, append an `## Agent Notes` section.

Selection: notes that have changed since their last extract run AND are at least
``agent.extract_min_word_count`` words long. The writer's 30-minute grace period
keeps us off notes the user just saved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from my_daemon.config import Settings
from my_daemon.llm import LLMClient
from my_daemon.llm.agents import extract_note_observations, render_agent_notes_body
from my_daemon.models import Note
from my_daemon.stores import AgentStateStore
from my_daemon.vault import VaultReader
from my_daemon.vault.writer import WriteResult, write_agent_section

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ExtractStats:
    notes_scanned: int = 0
    notes_eligible: int = 0
    notes_processed: int = 0
    notes_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _payload_hash(summary: str, key_points: list[str], themes: list[str]) -> str:
    """Stable identity for the *content* of an extract — used to skip no-op re-runs."""
    blob = summary + "\n" + "\n".join(key_points) + "\n" + ",".join(themes)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _candidate_notes(
    settings: Settings,
    state: AgentStateStore,
    all_: bool,
    only_note: str | None,
) -> Iterable[Note]:
    """Yield notes that should be considered for extraction this run."""
    reader = VaultReader(settings.vault.path, exclude_dirs=settings.vault.exclude_dirs)
    for note in reader.read_all():
        if only_note and note.relative_path != only_note:
            continue
        if note.word_count < settings.agent.extract_min_word_count:
            continue
        if not all_:
            prior = state.extract_run_for(note.relative_path)
            if prior is not None:
                prior_mtime = prior.get("note_mtime_seen") or 0.0
                if note.mtime.timestamp() <= prior_mtime:
                    continue
        yield note


def run_extract(
    settings: Settings,
    state: AgentStateStore,
    llm: LLMClient,
    *,
    all_: bool = False,
    only_note: str | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> ExtractStats:
    stats = ExtractStats()
    vault_root = settings.vault.path

    notes = list(_candidate_notes(settings, state, all_, only_note))
    stats.notes_scanned = len(notes)
    stats.notes_eligible = len(notes)

    model = llm.config.batch_model or llm.config.model

    for i, note in enumerate(notes):
        if progress:
            progress(i, len(notes), note.relative_path)
        if dry_run:
            stats.notes_skipped += 1
            continue
        try:
            payload = extract_note_observations(llm, note, model=model)
        except Exception as exc:
            stats.errors.append(f"{note.relative_path}: extract failed: {exc!r}")
            continue

        body = render_agent_notes_body(payload, model_used=model)
        try:
            result: WriteResult = write_agent_section(
                Path(note.path),
                body,
                vault_root=vault_root,
                agent_folder=settings.agent.folder_name,
                grace_minutes=settings.agent.write_grace_minutes,
            )
        except ValueError as exc:
            # e.g. two ## Agent Notes headings detected — refuse, don't mangle.
            stats.errors.append(f"{note.relative_path}: {exc}")
            continue

        if not result.changed:
            stats.notes_skipped += 1
            continue
        state.record_extract_run(
            note.relative_path,
            note_mtime=note.mtime.timestamp(),
            summary_hash=_payload_hash(payload.summary, payload.key_points, payload.themes),
        )
        stats.notes_processed += 1

    return stats
