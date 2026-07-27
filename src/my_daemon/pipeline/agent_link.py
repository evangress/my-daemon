# SPDX-License-Identifier: Apache-2.0
"""Auto-linker / tagger.

For each note, find candidate wikilinks and tags drawn from the rest of the
vault. The auto-apply gate is strict (cosine >= agent.link_apply_cosine AND the
target's title appears verbatim in the source body). Anything between the
suggest and apply thresholds lands in ``Agent/link-suggestions-YYYY-MM-DD.md``
for the user to skim.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder
from my_daemon.llm import LLMClient
from my_daemon.llm.agents import LinkSuggestion, propose_links
from my_daemon.models import THEME_TAG_PREFIX, Note, is_daemon_authored_tag
from my_daemon.stores import AgentStateStore, GraphStore, VectorStore
from my_daemon.vault import VaultReader
from my_daemon.vault.identity import effective_uuid
from my_daemon.vault.writer import add_tags, insert_wikilinks, write_atomic

ProgressFn = Callable[[int, int, str], None]


@dataclass
class LinkStats:
    notes_scanned: int = 0
    auto_applied_links: int = 0
    auto_applied_tags: int = 0
    suggestions_written: int = 0
    errors: list[str] = field(default_factory=list)
    suggestions_file: Path | None = None


def _note_query_vector(embedder: Embedder, note: Note) -> list[float]:
    # Use a leading body slice; sentence-transformers truncates anyway, but
    # keeping this explicit makes the cost predictable.
    return embedder.encode_one(note.body[:6000])


def _title_appears_in_body(title: str, body: str) -> bool:
    """Case-insensitive word-boundary check for the target's title in the source body."""
    pattern = re.compile(rf"(?<!\w){re.escape(title)}(?!\w)", re.IGNORECASE)
    return bool(pattern.search(body))


def _gather_link_candidates(
    note: Note,
    embedder: Embedder,
    vector_store: VectorStore,
    path_to_note: dict[str, Note],
    suggest_cosine: float,
    apply_cosine: float,
    apply_requires_substring: bool,
    max_per_note: int,
) -> tuple[list[tuple[Note, float]], list[tuple[Note, float]]]:
    """Return (auto_apply_candidates, suggest_only_candidates), each ordered best-first."""
    vec = _note_query_vector(embedder, note)
    # Over-fetch chunks so we can dedupe to top notes.
    raw = vector_store.search(vec, top_k=max(40, max_per_note * 6))

    # Take the best chunk hit per note_path (deduplicate).
    best_by_note: dict[str, float] = {}
    for h in raw:
        path = h.get("note_path")
        if not path or path == note.relative_path:
            continue
        if path not in path_to_note:
            continue  # may be stale (deleted note) — skip
        score = float(h.get("score", 0.0))
        if score > best_by_note.get(path, -1.0):
            best_by_note[path] = score

    ranked = sorted(best_by_note.items(), key=lambda kv: kv[1], reverse=True)

    auto: list[tuple[Note, float]] = []
    suggest: list[tuple[Note, float]] = []
    for path, score in ranked:
        if score < suggest_cosine:
            break
        cand = path_to_note[path]
        is_apply = score >= apply_cosine
        if (
            is_apply
            and apply_requires_substring
            and not _title_appears_in_body(cand.title, note.body)
        ):
            is_apply = False
        if is_apply and len(auto) < max_per_note:
            auto.append((cand, score))
        else:
            suggest.append((cand, score))
    return auto, suggest


def _gather_tag_candidates(
    note: Note,
    graph_store: GraphStore,
    uuid_to_note: dict[str, Note],
    min_neighbor_count: int,
    depth: int = 2,
) -> list[str]:
    """Tags carried by >= N graph-nearby notes but missing from the source.

    Daemon-authored tags are never propagated. A theme tag reaching a note must
    be a decision the user made in ``daemon themes review``, not a side effect
    of enough neighbours happening to carry it — otherwise the daemon spreads
    its own conclusion into notes nobody accepted it for.
    """

    neighbors = graph_store.neighbors_within(
        effective_uuid(note),
        depth=depth,
        exclude_tag_prefixes=(THEME_TAG_PREFIX,),
    )
    if not neighbors:
        return []
    have = set(note.tags)
    counts: dict[str, int] = {}
    for neighbor_uuid in neighbors:
        nb_note = uuid_to_note.get(neighbor_uuid)
        if nb_note is None:
            continue
        for t in nb_note.tags:
            if t in have or is_daemon_authored_tag(t):
                continue
            counts[t] = counts.get(t, 0) + 1
    return sorted([t for t, c in counts.items() if c >= min_neighbor_count])


def _format_suggestions_block(
    source: Note,
    suggestions: list[tuple[Note, float]],
    llm_notes: list[LinkSuggestion] | None,
) -> str:
    lines: list[str] = [f"### {source.title}", f"_path: `{source.relative_path}`_", ""]
    by_target = {s.target.lower(): s for s in (llm_notes or [])}
    for cand, score in suggestions:
        reason = ""
        ls = by_target.get(cand.title.lower())
        if ls is not None:
            reason = f" — _{ls.reason}_ (confidence {ls.confidence:.2f})"
        lines.append(f"- `[[{cand.title}]]` (cosine {score:.2f}){reason}")
    lines.append("")
    return "\n".join(lines)


def run_link(
    settings: Settings,
    state: AgentStateStore,
    embedder: Embedder,
    vector_store: VectorStore,
    graph_store: GraphStore,
    llm: LLMClient | None = None,
    *,
    only_note: str | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> LinkStats:
    """Run the linker across the vault (or a single note)."""
    stats = LinkStats()
    vault_root = settings.vault.path
    agent_folder = settings.agent.folder_name

    reader = VaultReader(vault_root, exclude_dirs=settings.vault.exclude_dirs)
    all_notes = list(reader.read_all())
    path_to_note: dict[str, Note] = {n.relative_path: n for n in all_notes}
    # The graph is keyed by identity, so tag gathering needs a uuid-keyed map.
    uuid_to_note: dict[str, Note] = {effective_uuid(n): n for n in all_notes}

    candidates = [path_to_note[only_note]] if only_note and only_note in path_to_note else all_notes
    stats.notes_scanned = len(candidates)

    suggestion_blocks: list[str] = []

    for i, note in enumerate(candidates):
        if progress:
            progress(i, len(candidates), note.relative_path)
        try:
            auto, suggest = _gather_link_candidates(
                note,
                embedder,
                vector_store,
                path_to_note,
                suggest_cosine=settings.agent.link_suggest_cosine,
                apply_cosine=settings.agent.link_apply_cosine,
                apply_requires_substring=settings.agent.link_apply_requires_title_substring,
                max_per_note=settings.agent.link_max_per_note,
            )
            tag_adds = _gather_tag_candidates(
                note,
                graph_store,
                uuid_to_note,
                min_neighbor_count=settings.agent.tag_apply_min_neighbor_count,
            )

            llm_opinions: list[LinkSuggestion] | None = None
            if suggest and llm is not None:
                cand_pairs = [
                    (c, c.body[:200]) for c, _ in suggest[: settings.agent.link_max_per_note * 2]
                ]
                try:
                    llm_opinions = propose_links(llm, note, cand_pairs)
                except Exception as exc:
                    stats.errors.append(f"{note.relative_path}: propose_links failed: {exc!r}")
                    llm_opinions = None

            if not dry_run and auto:
                links = [(cand.title, cand.title) for cand, _ in auto]
                result = insert_wikilinks(
                    Path(note.path),
                    links,
                    vault_root=vault_root,
                    agent_folder=agent_folder,
                    grace_minutes=settings.agent.write_grace_minutes,
                )
                if result.changed:
                    stats.auto_applied_links += len(auto)

            if not dry_run and tag_adds:
                tag_result = add_tags(
                    Path(note.path),
                    tag_adds,
                    vault_root=vault_root,
                    agent_folder=agent_folder,
                    grace_minutes=settings.agent.write_grace_minutes,
                )
                if tag_result.changed:
                    stats.auto_applied_tags += len(tag_adds)

            if suggest:
                suggestion_blocks.append(_format_suggestions_block(note, suggest, llm_opinions))

            state.record_link_run(
                note.relative_path,
                note_mtime=note.mtime.timestamp(),
                applied=len(auto) if not dry_run else 0,
                suggested=len(suggest),
            )
        except Exception as exc:
            stats.errors.append(f"{note.relative_path}: {exc!r}")

    if suggestion_blocks and not dry_run:
        out_path = vault_root / agent_folder / f"link-suggestions-{date.today().isoformat()}.md"
        header = (
            f"# Link suggestions — {date.today().isoformat()}\n\n"
            "_Generated by your daemon. Each block is one source note with candidate wikilinks "
            "that fell below the auto-apply threshold. Edit your notes directly to accept any "
            "of these; the daemon won't apply them automatically._\n\n"
        )
        write_atomic(out_path, header + "\n".join(suggestion_blocks))
        stats.suggestions_file = out_path
        stats.suggestions_written = len(suggestion_blocks)

    return stats
