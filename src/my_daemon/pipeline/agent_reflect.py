# SPDX-License-Identifier: Apache-2.0
"""Reflection job: maintain themed memory files in `<vault>/Agent/memory-*.md`.

For each configured theme, gather recent notes + recent chats, ask the LLM to
update the file (preserving user edits as ground truth), and write atomically
with a snapshot beforehand. Also append a one-block-per-run rolling journal.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import frontmatter

from my_daemon.config import Settings
from my_daemon.llm import LLMClient
from my_daemon.llm.agents import update_memory
from my_daemon.models import FeedbackEvent, Note
from my_daemon.stores import AgentStateStore, FeedbackStore
from my_daemon.vault import VaultReader
from my_daemon.vault.writer import snapshot, write_atomic

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ReflectStats:
    themes_processed: int = 0
    themes_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    rolling_journal_path: Path | None = None


def _recent_notes(settings: Settings, days: int) -> list[Note]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    reader = VaultReader(settings.vault.path, exclude_dirs=settings.vault.exclude_dirs)
    return [n for n in reader.read_all() if n.mtime >= cutoff]


def _recent_chats(feedback: FeedbackStore, limit: int = 20) -> list[FeedbackEvent]:
    rows = feedback.recent(limit=limit)
    out: list[FeedbackEvent] = []
    for r in rows:
        out.append(
            FeedbackEvent(
                id=r.get("id"),
                timestamp=datetime.fromisoformat(r["timestamp"]),
                query=r.get("query", ""),
                retrieval_summary={},  # not needed by the prompt
                answer=r.get("answer", ""),
                latency_ms=int(r.get("latency_ms", 0)),
            )
        )
    return out


def _source_hash(notes: list[Note], chats: list[FeedbackEvent]) -> str:
    blob_lines: list[str] = [f"{n.relative_path}:{n.mtime.isoformat()}" for n in notes]
    blob_lines += [f"chat:{ev.id}:{ev.timestamp.isoformat()}" for ev in chats]
    return hashlib.sha256("\n".join(blob_lines).encode("utf-8")).hexdigest()[:16]


def _memory_path(vault_root: Path, agent_folder: str, theme: str) -> Path:
    return vault_root / agent_folder / f"memory-{theme}.md"


def _read_prior(memory_path: Path) -> tuple[str, dict]:
    """Return (prior_body_only_no_frontmatter, prior_metadata)."""
    if not memory_path.is_file():
        return "", {}
    post = frontmatter.loads(memory_path.read_text(encoding="utf-8"))
    return post.content, dict(post.metadata)


def _compose_file(theme: str, new_body: str, *, model_used: str, n_notes: int, n_chats: int) -> str:
    post = frontmatter.Post(
        new_body.strip() + "\n",
        daemon="memory",
        theme=theme,
        updated=datetime.now(UTC).isoformat(),
        model=model_used,
        source_notes=n_notes,
        source_chats=n_chats,
    )
    preamble = (
        "_This file is maintained by your daemon. Edit freely — the next run will "
        "incorporate your edits as ground truth. Delete it to start a theme from scratch._\n\n"
    )
    return frontmatter.dumps(post).replace(new_body, preamble + new_body) + "\n"


def _append_rolling(
    vault_root: Path, agent_folder: str, line: str
) -> Path:
    path = vault_root / agent_folder / "memory-rolling.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    block = f"\n### {today}\n{line.strip()}\n"
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = (
            "# Rolling memory journal\n\n"
            "_Dated entries appended by `daemon reflect`. Newest at the bottom._\n"
        )
    write_atomic(path, existing.rstrip() + "\n" + block)
    return path


def run_reflect(
    settings: Settings,
    state: AgentStateStore,
    feedback: FeedbackStore,
    llm: LLMClient,
    *,
    only_theme: str | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> ReflectStats:
    stats = ReflectStats()
    vault_root = settings.vault.path
    agent_folder = settings.agent.folder_name

    themes = (
        [only_theme] if only_theme else list(settings.agent.reflect_themes)
    )
    notes = _recent_notes(settings, settings.agent.reflect_lookback_days)
    chats = _recent_chats(feedback, limit=20)
    sig = _source_hash(notes, chats)
    model = llm.config.batch_model or llm.config.model

    summary_lines: list[str] = []

    for i, theme in enumerate(themes):
        if progress:
            progress(i, len(themes), theme)

        prior_row = state.reflect_run_for(theme)
        if prior_row and prior_row.get("source_notes_hash") == sig and not dry_run:
            stats.themes_skipped += 1
            continue

        memory_path = _memory_path(vault_root, agent_folder, theme)
        prior_body, _prior_meta = _read_prior(memory_path)

        try:
            new_body = update_memory(llm, theme, prior_body, notes, chats, model=model)
        except Exception as exc:
            stats.errors.append(f"{theme}: update_memory failed: {exc!r}")
            continue

        if dry_run:
            stats.themes_processed += 1
            continue

        if memory_path.is_file():
            snapshot(memory_path, vault_root=vault_root, agent_folder=agent_folder)
        file_text = _compose_file(theme, new_body, model_used=model, n_notes=len(notes), n_chats=len(chats))
        write_atomic(memory_path, file_text)

        state.record_reflect_run(theme, source_notes_hash=sig, source_chat_count=len(chats))
        stats.themes_processed += 1
        summary_lines.append(f"- **{theme}**: refreshed against {len(notes)} notes, {len(chats)} chats.")

    if summary_lines and not dry_run:
        stats.rolling_journal_path = _append_rolling(
            vault_root, agent_folder, "\n".join(summary_lines)
        )

    return stats
