# SPDX-License-Identifier: Apache-2.0
"""Observer pipeline (M4): snapshot → analyze → letter → index → decay.

The closing loop of the adaptive memory cycle. The observer LLM reads the
structural + weight-evolution reports a snapshot produces and writes a single
markdown letter into ``<vault>/Agent/observer-<YYYY-MM-DD>.md``. The same
writeback safety discipline as `daemon reflect` applies: atomic write,
backup snapshot of any prior letter, a rolling index file for navigation,
and a record in `agent_observer_runs`.

Decay runs at the very end. It's the *only* point where ``daemon consolidate``
mutates the live graph, and only in the gentle direction of pulling
reinforced edges back toward baseline. The thinking is that we just took a
snapshot for analysis, so this is the safe moment to apply the entropy.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter

from my_daemon.analysis import compute_report, persist_reports, simulate_evolution
from my_daemon.analysis.structural import report_dir_for
from my_daemon.config import Settings
from my_daemon.llm import LLMClient
from my_daemon.llm.agents import _model_for, observer_letter
from my_daemon.models import FeedbackEvent
from my_daemon.retrieval.weights import decay_unused_edges
from my_daemon.stores import (
    AgentStateStore,
    FeedbackStore,
    GraphStore,
    SnapshotBundle,
    create_snapshot,
    get_snapshot,
    open_readonly,
)
from my_daemon.vault.writer import snapshot as vault_snapshot
from my_daemon.vault.writer import write_atomic

ProgressFn = Callable[[str], None]

_LETTER_FILE_RE = re.compile(r"^observer-(\d{4}-\d{2}-\d{2})\.md$")
_INDEX_FILENAME = "observer.md"


@dataclass
class ObserveStats:
    """Summary of one consolidate run, returned to the CLI."""

    snapshot_id: str
    letter_path: Path | None = None
    model_used: str = ""
    communities_seen: int = 0
    themes_seen: int = 0
    themes_new: int = 0
    theme_churn: float = 0.0
    events_replayed: int = 0
    edges_decayed: int = 0
    dry_run: bool = False
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_bundle(
    settings: Settings,
    snapshot_id: str | None,
    *,
    include_qdrant: bool,
    progress: ProgressFn | None,
) -> SnapshotBundle:
    if snapshot_id is not None:
        bundle = get_snapshot(settings, snapshot_id)
        if bundle is None:
            raise FileNotFoundError(
                f"no snapshot with id {snapshot_id} in {settings.snapshot.dir}"
            )
        if progress:
            progress(f"using existing snapshot {bundle.id}")
        return bundle
    if progress:
        progress("creating snapshot…")
    warnings: list[str] = []
    bundle = create_snapshot(
        settings, include_qdrant=include_qdrant, on_warning=warnings.append,
    )
    if progress:
        for w in warnings:
            progress(f"snapshot warning: {w}")
        progress(f"snapshot created: {bundle.id}")
    return bundle


def _letter_path_for_today(vault_root: Path, agent_folder: str) -> Path:
    today = date.today().isoformat()
    return vault_root / agent_folder / f"observer-{today}.md"


def _load_prior_letters(
    vault_root: Path, agent_folder: str, *, limit: int, exclude: Path | None = None
) -> list[str]:
    """Return body text of the most-recent observer letters, newest first.

    Skips ``exclude`` (the letter we're about to write) so this run's draft
    doesn't get fed back into itself when a same-day re-run loads priors.
    """

    folder = vault_root / agent_folder
    if not folder.is_dir():
        return []
    matches: list[tuple[str, Path]] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        m = _LETTER_FILE_RE.match(path.name)
        if not m:
            continue
        if exclude is not None and path.resolve() == exclude.resolve():
            continue
        matches.append((m.group(1), path))
    matches.sort(key=lambda x: x[0], reverse=True)

    bodies: list[str] = []
    for _, path in matches[:limit]:
        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a malformed prior shouldn't break this run
            continue
        bodies.append(post.content.strip())
    return bodies


def _recent_feedback(feedback: FeedbackStore, *, limit: int = 20) -> list[FeedbackEvent]:
    """Recent chats, hydrated into FeedbackEvent for the observer prompt."""
    out: list[FeedbackEvent] = []
    for row in feedback.recent(limit=limit):
        try:
            out.append(
                FeedbackEvent(
                    id=row.get("id"),
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    query=row.get("query") or "",
                    retrieval_summary={},  # not needed by the prompt
                    answer=row.get("answer") or "",
                    latency_ms=int(row.get("latency_ms") or 0),
                )
            )
        except Exception:  # noqa: BLE001 — skip a malformed row, don't break the run
            continue
    return out


def _compose_letter_file(body: str, *, snapshot_id: str, model_used: str) -> str:
    """Wrap the LLM's markdown body with provenance frontmatter + a header note."""
    post = frontmatter.Post(
        body.strip() + "\n",
        daemon="observer",
        snapshot_id=snapshot_id,
        written_at=datetime.now(UTC).isoformat(),
        model=model_used,
    )
    preamble = (
        "_Letter from your daemon. Auto-written from snapshot "
        f"`{snapshot_id}` (model: `{model_used}`). Edit freely; the next "
        "run will read your edits via the prior-letters continuity loop._\n\n"
    )
    return frontmatter.dumps(post).replace(body, preamble + body) + "\n"


def _first_paragraph(body: str, *, max_chars: int = 280) -> str:
    """Excerpt used in the rolling index — first non-empty paragraph, trimmed."""
    for chunk in body.split("\n\n"):
        stripped = chunk.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip the trailing italic provenance line if it's the only content.
        if stripped.startswith("_") and stripped.endswith("_"):
            continue
        if len(stripped) > max_chars:
            return stripped[: max_chars - 1].rstrip() + "…"
        return stripped
    return "(empty letter)"


def _write_rolling_index(
    vault_root: Path, agent_folder: str, *, window: int
) -> Path:
    """(Re)render ``observer.md`` to point at the most-recent ``window`` letters."""

    folder = vault_root / agent_folder
    folder.mkdir(parents=True, exist_ok=True)
    matches: list[tuple[str, Path]] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        m = _LETTER_FILE_RE.match(path.name)
        if not m:
            continue
        matches.append((m.group(1), path))
    matches.sort(key=lambda x: x[0], reverse=True)

    lines = [
        "# Observer letters",
        "",
        "_Most-recent letters from your daemon. Open one to read the full thing._",
        "",
    ]
    for date_str, path in matches[:window]:
        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8"))
            excerpt = _first_paragraph(post.content)
        except Exception:  # noqa: BLE001 — degraded entry is still better than crashing
            excerpt = "(could not read letter)"
        lines.append(f"## [{date_str}]({path.name})")
        lines.append("")
        lines.append(f"> {excerpt}")
        lines.append("")

    index_path = folder / _INDEX_FILENAME
    write_atomic(index_path, "\n".join(lines).rstrip() + "\n")
    return index_path


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_observe(
    settings: Settings,
    state: AgentStateStore,
    feedback: FeedbackStore,
    graph_store: GraphStore,
    llm: LLMClient,
    *,
    snapshot_id: str | None = None,
    dry_run: bool = False,
    include_qdrant: bool = True,
    now: datetime | None = None,
    progress: ProgressFn | None = None,
) -> ObserveStats:
    """Run the consolidation loop: snapshot → analyze → letter → index → decay.

    ``snapshot_id`` lets the caller reuse an existing bundle (idempotent
    re-runs against the same point in time). ``dry_run`` lets the LLM run
    but skips every write to the vault and every mutation of the live graph.
    """

    moment = now or datetime.now(UTC)
    bundle = _resolve_bundle(
        settings, snapshot_id, include_qdrant=include_qdrant, progress=progress,
    )

    cfg = settings.agent
    cons = settings.consolidation

    # --- analyze ----------------------------------------------------------
    handle = open_readonly(bundle)
    try:
        structural = compute_report(
            handle.graph,
            snapshot_id=bundle.id,
            max_communities=cfg.observer_max_communities,
            max_bridging_notes=cons.max_bridging_notes,
            max_bridge_edges=cons.max_bridge_edges,
            max_orphans=cons.max_orphans,
            max_dangling=cons.max_dangling,
            max_warm_edges=cons.max_warm_edges,
            betweenness_sample_k=cons.betweenness_sample_k,
        )
    finally:
        handle.close()

    if progress:
        progress(
            f"structural: {structural.community_count} communities, "
            f"{len(structural.warm_edges)} warm edges, "
            f"{len(structural.bridging_notes)} bridging notes"
        )

    evolution = simulate_evolution(
        bundle, lookback_days=cfg.observer_lookback_days, now=moment,
    )
    if progress:
        progress(
            f"evolution: {evolution.events_replayed} events replayed "
            f"({evolution.events_skipped} skipped)"
        )

    out_dir = report_dir_for(bundle.id, root=cons.out_dir)
    persist_reports(out_dir, structural=structural, evolution=evolution)

    # --- letter -----------------------------------------------------------
    vault_root = settings.vault.path
    agent_folder = cfg.folder_name
    today_letter_path = _letter_path_for_today(vault_root, agent_folder)
    prior_letters = _load_prior_letters(
        vault_root, agent_folder,
        limit=cfg.observer_prior_letters,
        exclude=today_letter_path,
    )
    recent_feedback = _recent_feedback(feedback, limit=20)
    model_used = _model_for(llm, cfg.observer_model)

    if progress:
        progress(f"calling observer LLM ({model_used})…")
    letter_body = observer_letter(
        llm,
        structural=structural,
        evolution=evolution,
        recent_feedback=recent_feedback,
        prior_letters=prior_letters,
        snapshot_id=bundle.id,
        max_communities=cfg.observer_max_communities,
        model=cfg.observer_model,
    )

    stats = ObserveStats(
        snapshot_id=bundle.id,
        model_used=model_used,
        communities_seen=structural.community_count,
        events_replayed=evolution.events_replayed,
        dry_run=dry_run,
    )

    theme_result = _cluster_themes(
        settings, llm, bundle.id, dry_run=dry_run, progress=progress, stats=stats
    )
    if theme_result is not None:
        stats.themes_seen = len(theme_result.matched) + len(theme_result.created)
        stats.themes_new = len(theme_result.created)
        stats.theme_churn = theme_result.churn

    # --- write (skipped on dry-run) ---------------------------------------
    letter_written_path: Path | None = None
    if not dry_run:
        if today_letter_path.is_file():
            # Same-day re-run: snapshot the prior body so the user can recover it.
            vault_snapshot(
                today_letter_path,
                vault_root=vault_root,
                agent_folder=agent_folder,
            )
        write_atomic(
            today_letter_path,
            _compose_letter_file(letter_body, snapshot_id=bundle.id, model_used=model_used),
        )
        letter_written_path = today_letter_path
        _write_rolling_index(
            vault_root, agent_folder, window=cfg.observer_index_window,
        )
        if progress:
            progress(f"wrote {today_letter_path}")
    else:
        stats.notes.append(f"dry-run: would write {today_letter_path}")
        if progress:
            progress(f"dry-run: skipped write to {today_letter_path}")

    stats.letter_path = letter_written_path

    # --- decay (skipped on dry-run) ---------------------------------------
    if not dry_run:
        decay_report = decay_unused_edges(graph_store, now=moment)
        graph_store.save()
        stats.edges_decayed = decay_report.edges_decayed
        if progress:
            progress(
                f"decayed {decay_report.edges_decayed} edge(s) "
                f"of {decay_report.edges_visited} reinforced"
            )

    # --- record -----------------------------------------------------------
    stats.run_id = state.record_observer_run(
        snapshot_id=bundle.id,
        letter_path=str(letter_written_path) if letter_written_path else None,
        model=model_used,
        events_replayed=stats.events_replayed,
        communities_seen=stats.communities_seen,
        edges_decayed=stats.edges_decayed,
        dry_run=dry_run,
    )
    return stats


def _cluster_themes(
    settings: Settings,
    llm,  # noqa: ANN001
    snapshot_id: str,
    *,
    dry_run: bool,
    progress,  # noqa: ANN001
    stats: ObserveStats,
):
    """Cluster query fingerprints into named themes, inside the dream phase.

    Deliberately non-fatal: a consolidation run that cannot cluster should
    still write its letter.
    """

    cons = settings.consolidation
    if not getattr(cons, "cluster_themes", True):
        return None

    from my_daemon.analysis.themes import cluster_fingerprints, reconcile_themes
    from my_daemon.llm.agents import name_theme
    from my_daemon.stores.activations import ActivationLedger
    from my_daemon.stores.registry import NoteRegistry
    from my_daemon.stores.themes import ThemeStore

    try:
        ledger = ActivationLedger(db_path=settings.feedback.db_path)
        clusters = cluster_fingerprints(
            ledger, min_cluster_size=cons.min_cluster_size, limit=cons.theme_query_limit
        )
        if progress:
            progress(f"themes: {len(clusters)} cluster(s) found")
        if dry_run:
            return None

        store = ThemeStore(db_path=settings.feedback.db_path)
        result = reconcile_themes(
            store, clusters, snapshot_id=snapshot_id,
            match_threshold=cons.theme_match_threshold,
        )

        registry = NoteRegistry(db_path=settings.feedback.db_path)
        # Only *new* clusters are named. A returning one keeps its id, label and
        # summary — no LLM call, and no churn in what the user sees.
        for theme_id, cluster in result.needs_naming[: cons.max_new_themes_per_run]:
            records = [registry.get(u) for u in cluster.centroid]
            titles = [r.title or r.rel_path for r in records if r]
            label, summary = name_theme(
                llm,
                note_titles=titles,
                representative_queries=cluster.representative_queries,
                model=settings.agent.observer_model,
            )
            store.set_label(theme_id, label=label, summary=summary)

        if len(result.needs_naming) > cons.max_new_themes_per_run:
            stats.notes.append(
                f"{len(result.needs_naming) - cons.max_new_themes_per_run} new theme(s) "
                "left unnamed this run (max_new_themes_per_run)"
            )
        if result.churn > 0.5:
            stats.notes.append(
                f"theme churn {result.churn:.2f} — themes are not settled yet"
            )
        return result
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"theme clustering failed: {exc!r}")
        return None
