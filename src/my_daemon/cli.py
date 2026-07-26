# SPDX-License-Identifier: Apache-2.0
"""Typer-based CLI: daemon init / ingest / query / status / search / graph / reset."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from my_daemon import __version__
from my_daemon.analysis import compute_report, persist_reports, simulate_evolution
from my_daemon.analysis.structural import report_dir_for
from my_daemon.config import Settings, load_settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.llm import LLMClient
from my_daemon.pipeline import QueryEngine, ingest_vault
from my_daemon.pipeline.agent_extract import run_extract
from my_daemon.pipeline.agent_link import run_link
from my_daemon.pipeline.agent_observe import run_observe
from my_daemon.pipeline.agent_reflect import run_reflect
from my_daemon.pipeline.migrate_uuids import (
    assign_uuids,
    list_migration_runs,
    rollback_uuids,
)
from my_daemon.retrieval.weights import apply_selection
from my_daemon.stores import (
    AgentStateStore,
    FeedbackStore,
    GraphStore,
    NoteRegistry,
    VectorStore,
    create_snapshot,
    delete_snapshot,
    get_snapshot,
    list_snapshots,
    open_readonly,
    prune_snapshots,
)
from my_daemon.stores.activations import ActivationLedger
from my_daemon.stores.db import MIGRATIONS as DB_MIGRATIONS
from my_daemon.stores.db import SCHEMA_VERSION as DB_SCHEMA_VERSION
from my_daemon.stores.db import migrate as db_migrate
from my_daemon.stores.db import schema_version as db_schema_version

app = typer.Typer(
    name="daemon",
    help="My Daemon — a personal memory companion over your Obsidian vault.",
    no_args_is_help=True,
    add_completion=False,
)
graph_app = typer.Typer(name="graph", help="Graph inspection commands.")
app.add_typer(graph_app)
models_app = typer.Typer(name="models", help="Embedding model management.")
app.add_typer(models_app)
snapshot_app = typer.Typer(name="snapshot", help="Freeze + manage read-only state bundles.")
app.add_typer(snapshot_app)
hermes_app = typer.Typer(name="hermes", help="Hermes memory-provider integration.")
app.add_typer(hermes_app)
migrate_app = typer.Typer(name="migrate", help="Schema and identity migrations.")
app.add_typer(migrate_app)

console = Console()


def _load() -> Settings:
    try:
        return load_settings()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _build_embedder(s: Settings) -> Embedder:
    return Embedder(
        s.embeddings.model,
        batch_size=s.embeddings.batch_size,
        device=s.embeddings.device,
        cache_folder=s.embeddings.cache_folder,
    )


def _build_sparse_embedder(s: Settings) -> SparseEmbedder | None:
    if not s.embeddings.hybrid:
        return None
    return SparseEmbedder(
        model_name=s.embeddings.sparse_model,
        cache_folder=s.embeddings.cache_folder,
    )


def _build_vector_store(s: Settings, dim: int) -> VectorStore:
    return VectorStore(
        url=s.vector_store.qdrant.url,
        collection=s.vector_store.qdrant.collection,
        dim=dim,
        hybrid=s.embeddings.hybrid,
    )


def _build_graph_store(s: Settings) -> GraphStore:
    return GraphStore(path=s.graph.path)


def _build_feedback_store(s: Settings) -> FeedbackStore:
    return FeedbackStore(db_path=s.feedback.db_path)


def _build_agent_state(s: Settings) -> AgentStateStore:
    # Same SQLite file as feedback — one state file to back up / wipe.
    return AgentStateStore(db_path=s.feedback.db_path)


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"my-daemon {__version__}")


@app.command()
def init(
    vault: str | None = typer.Option(None, help="Path to your Obsidian vault."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config.yaml."),
) -> None:
    """Generate config.yaml + .env from examples, prompting for the vault path."""
    cwd = Path.cwd()
    config_path = cwd / "config.yaml"
    env_path = cwd / ".env"
    config_example = cwd / "config.example.yaml"
    env_example = cwd / ".env.example"

    if config_path.exists() and not force:
        console.print(f"[yellow]{config_path} already exists. Use --force to overwrite.[/yellow]")
        raise typer.Exit(code=1)
    if not config_example.is_file():
        console.print(f"[red]Missing {config_example}. Run from the project root.[/red]")
        raise typer.Exit(code=1)

    vault_path = vault or Prompt.ask(
        "Vault path",
        default="~/Documents/Obsidian/MyVault",
    )
    text = config_example.read_text(encoding="utf-8")
    text = text.replace("~/Documents/Obsidian/MyVault", vault_path)
    config_path.write_text(text, encoding="utf-8")
    console.print(f"[green]Wrote {config_path}[/green]")

    if not env_path.exists() and env_example.is_file():
        shutil.copy(env_example, env_path)
        console.print(f"[green]Wrote {env_path} — add your ANTHROPIC_API_KEY there.[/green]")


@app.command()
def ingest(
    full: bool = typer.Option(False, "--full", help="Force a full rebuild instead of incremental."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Ingest the vault into the vector store and the graph."""
    s = _load()
    embedder = _build_embedder(s)
    sparse_embedder = _build_sparse_embedder(s)
    vector_store = _build_vector_store(s, dim=embedder.dimension)
    graph_store = _build_graph_store(s)

    def _progress(i: int, total: int, rel_path: str) -> None:
        if verbose:
            console.log(f"[{i + 1}/{total}] {rel_path}")

    stats = ingest_vault(
        s, embedder, vector_store, graph_store,
        sparse_embedder=sparse_embedder, full_rebuild=full, progress=_progress,
    )

    table = Table(title="Ingest summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Notes scanned", str(stats.notes_scanned))
    table.add_row("Notes new/updated", str(stats.notes_new_or_updated))
    table.add_row("Notes skipped (unchanged)", str(stats.skipped_unchanged))
    table.add_row("Notes deleted", str(stats.notes_deleted))
    table.add_row("Chunks upserted", str(stats.chunks_upserted))
    console.print(table)
    if stats.errors:
        console.print(Panel("\n".join(stats.errors), title="Errors", border_style="red"))


@app.command()
def query(
    text: str = typer.Argument(..., help="The question to ask your daemon."),
    no_synthesize: bool = typer.Option(False, "--no-llm", help="Skip LLM synthesis; show ranked context only."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Ask the daemon a question."""
    s = _load()
    embedder = _build_embedder(s)
    sparse_embedder = _build_sparse_embedder(s)
    vector_store = _build_vector_store(s, dim=embedder.dimension)
    graph_store = _build_graph_store(s)
    graph_store.load()
    feedback_store = _build_feedback_store(s)
    llm = LLMClient(s.llm, api_key=s.anthropic_api_key)

    engine = QueryEngine(
        s, embedder, vector_store, graph_store, feedback_store, llm,
        sparse_embedder=sparse_embedder,
    )
    response = engine.ask(text, synthesize=not no_synthesize)

    if response.answer:
        console.print(Panel(response.answer, title="Daemon", border_style="cyan"))

    table = Table(title=f"Candidates (≥{s.retrieval.candidate_pool})")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("source")
    table.add_column("preview")
    for i, rc in enumerate(response.retrieval.ranked, start=1):
        heading = " › ".join(rc.chunk.heading_path) if rc.chunk.heading_path else "—"
        preview = rc.chunk.text.strip().replace("\n", " ")[:80]
        table.add_row(str(i), f"{rc.combined_score:.3f}", f"{rc.chunk.note_path}\n› {heading}", preview)
    console.print(table)

    if response.memories and s.memory.show_to_user:
        lines = [
            f"[dim]{m.ts.date().isoformat()}[/dim]  {m.text}\n"
            f"    [dim]same notes: {', '.join(m.shared_notes) or '—'}[/dim]"
            for m in response.memories
        ]
        console.print(
            Panel(
                "\n".join(lines),
                title="You've been here before",
                border_style="magenta",
            )
        )

    if verbose:
        console.log(
            f"seeds={len(response.retrieval.seeds)} expanded={len(response.retrieval.expanded)} "
            f"latency_ms={response.latency_ms} feedback_id={response.feedback_event_id} "
            f"memories={len(response.memories)}"
        )


@app.command()
def ask(text: str = typer.Argument(...)) -> None:
    """Alias for query."""
    query(text=text)


@app.command()
def select(
    feedback_id: int = typer.Argument(..., help="The feedback event id returned by `daemon query -v`."),
    rank: int = typer.Argument(..., help="Which candidate to select (1-based, matching the table)."),
) -> None:
    """Record that the user picked candidate #rank for a past query.

    Attaches a ``candidate_selected`` signal to the feedback row and reinforces
    every edge on the shortest graph path from the seed note that produced
    that candidate to the candidate's note. Reinforced edges feel "shorter"
    to future graph expansions.
    """
    s = _load()
    feedback_store = _build_feedback_store(s)
    event = feedback_store.get(feedback_id)
    if event is None:
        console.print(f"[red]No feedback event with id {feedback_id}.[/red]")
        raise typer.Exit(code=1)

    ranked = event.retrieval_summary.get("ranked") or []
    if not ranked:
        console.print(
            f"[red]Feedback #{feedback_id} has no ranked candidates "
            "(was logged before the schema upgrade?).[/red]"
        )
        raise typer.Exit(code=1)
    if rank < 1 or rank > len(ranked):
        console.print(
            f"[red]Rank {rank} out of range; this event has {len(ranked)} candidates.[/red]"
        )
        raise typer.Exit(code=1)

    picked = ranked[rank - 1]
    seed_note = picked.get("seed_note_uuid") or picked.get("note_uuid")
    selected_note = picked.get("note_uuid")

    graph_store = _build_graph_store(s)
    graph_store.load()
    result = apply_selection(
        graph_store,
        seed_note_uuid=seed_note,
        selected_note_uuid=selected_note,
        selected_note_path=picked.get("note_path"),
    )
    graph_store.save()

    feedback_store.attach_signal(
        feedback_id,
        "candidate_selected",
        selected_rank=rank,
        selected_chunk_id=picked.get("chunk_id"),
        selected_note_uuid=selected_note,
        selected_note_path=picked.get("note_path"),
    )

    console.print(
        f"[green]Recorded selection #{rank} → {selected_note}.[/green]\n"
        f"Path: {' → '.join(n.removeprefix('note::').removeprefix('tag::') for n in result.path) or '(self)'}\n"
        f"Edges reinforced: {result.edges_reinforced}, total Δweight: {result.total_delta:.3f}"
    )


@app.command()
def search(
    text: str = typer.Argument(..., help="Vector-only search; no LLM synthesis."),
    top_k: int = typer.Option(8, "--top-k", "-k"),
) -> None:
    """Debug: vector search without graph expansion or LLM."""
    s = _load()
    embedder = _build_embedder(s)
    sparse_embedder = _build_sparse_embedder(s)
    vector_store = _build_vector_store(s, dim=embedder.dimension)

    vec = embedder.encode_one(text)
    if sparse_embedder is not None:
        sparse_vec = sparse_embedder.encode_one(text)
        hits = vector_store.hybrid_search(vec, sparse_vec, top_k=top_k)
    else:
        hits = vector_store.search(vec, top_k=top_k)

    table = Table(title="Vector hits")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("note")
    table.add_column("preview")
    for i, h in enumerate(hits, start=1):
        preview = (h.get("text") or "").strip().replace("\n", " ")[:80]
        table.add_row(str(i), f"{h['score']:.3f}", h["note_path"], preview)
    console.print(table)


@app.command()
def status() -> None:
    """Show ingest stats and store sizes."""
    s = _load()
    embedder = _build_embedder(s)
    graph_store = _build_graph_store(s)
    graph_store.load()
    try:
        vector_store = _build_vector_store(s, dim=embedder.dimension)
        v_count: int | str = vector_store.count()
    except Exception as exc:
        v_count = f"unavailable ({exc})"

    g_stats = graph_store.stats()

    table = Table(title="My Daemon status")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("vault path", str(s.vault.path))
    table.add_row("vector chunks", str(v_count))
    table.add_row("graph notes", str(g_stats.note_count))
    table.add_row("graph tags", str(g_stats.tag_count))
    table.add_row("graph edges", str(g_stats.edge_count))
    table.add_row("manifest", str(s.graph.manifest_path))
    console.print(table)


@graph_app.command("stats")
def graph_stats() -> None:
    """PageRank top notes and degree-based top tags."""
    s = _load()
    graph_store = _build_graph_store(s)
    graph_store.load()
    stats = graph_store.stats()

    pr_table = Table(title="Top notes by PageRank")
    pr_table.add_column("note")
    pr_table.add_column("score", justify="right")
    for note, score in stats.top_pagerank:
        pr_table.add_row(note, f"{score:.4f}")
    console.print(pr_table)

    tag_table = Table(title="Top tags by degree")
    tag_table.add_column("tag")
    tag_table.add_column("degree", justify="right")
    for tag, degree in stats.top_tags:
        tag_table.add_row(tag, str(degree))
    console.print(tag_table)


@app.command()
def chat(
    host: str = typer.Option("127.0.0.1", help="Host interface to bind."),
    port: int = typer.Option(8765, help="Port for the local UI."),
    native: bool = typer.Option(
        True,
        "--native/--no-native",
        help="Open as a desktop window via pywebview (default). Use --no-native for the browser tab.",
    ),
) -> None:
    """Launch the warm-themed chat window for the daemon."""
    # Cheap, fast preflight when running native: NiceGUI swallows a missing
    # pywebview import in unhelpful ways. Catching it here means the silent
    # .vbs launcher writes a clear cause to the log instead of dying mute.
    if native:
        try:
            import webview  # noqa: F401  (pywebview installs as `webview`)
        except ImportError as exc:
            console.print(
                "[red]Native mode requires pywebview, which is not installed in this venv.[/red]\n"
                "Fix with one of:\n"
                "  [bold]uv sync[/bold]                    (preferred — picks up the pinned version)\n"
                "  [bold].venv\\Scripts\\pip install pywebview[/bold]  (Windows)\n"
                "  [bold].venv/bin/pip install pywebview[/bold]      (Linux/macOS)\n"
                "Or pass [bold]--no-native[/bold] to use the browser tab instead."
            )
            raise typer.Exit(code=2) from exc

    from my_daemon.gui import launch_chat

    launch_chat(host=host, port=port, native=native)


@app.command()
def setup() -> None:
    """Open the Tkinter setup window: pick a vault folder, set the API key."""
    from my_daemon.gui import launch_setup

    launch_setup()


@app.command()
def extract(
    all_: bool = typer.Option(False, "--all", help="Process every eligible note, not just changed ones."),
    note: str | None = typer.Option(None, "--note", help="Vault-relative path; restrict to one note."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be processed; touch nothing."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Write an `## Agent Notes` section into recently-changed notes (the daily extractor)."""
    s = _load()
    if not s.agent.enabled and not dry_run:
        console.print(
            "[yellow]agent.enabled is false in config.yaml — refusing to write. "
            "Pass --dry-run to preview, or flip the flag once you're ready.[/yellow]"
        )
        raise typer.Exit(code=1)

    state = _build_agent_state(s)
    llm = LLMClient(s.llm, api_key=s.anthropic_api_key)

    def _progress(i: int, total: int, rel_path: str) -> None:
        if verbose:
            console.log(f"[{i + 1}/{total}] {rel_path}")

    stats = run_extract(s, state, llm, all_=all_, only_note=note, dry_run=dry_run, progress=_progress)

    table = Table(title=f"daemon extract {'(dry-run)' if dry_run else ''}".strip())
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Notes scanned", str(stats.notes_scanned))
    table.add_row("Eligible", str(stats.notes_eligible))
    table.add_row("Processed", str(stats.notes_processed))
    table.add_row("Skipped", str(stats.notes_skipped))
    console.print(table)
    if stats.errors:
        console.print(Panel("\n".join(stats.errors), title="Errors", border_style="red"))


@app.command()
def link(
    note: str | None = typer.Option(None, "--note", help="Vault-relative path; restrict to one note."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; touch nothing."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM second-opinion on suggestions."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Auto-link / tag notes; write lower-confidence suggestions to Agent/link-suggestions-*.md."""
    s = _load()
    if not s.agent.enabled and not dry_run:
        console.print(
            "[yellow]agent.enabled is false in config.yaml — refusing to write. "
            "Pass --dry-run to preview, or flip the flag once you're ready.[/yellow]"
        )
        raise typer.Exit(code=1)

    embedder = _build_embedder(s)
    vector_store = _build_vector_store(s, dim=embedder.dimension)
    graph_store = _build_graph_store(s)
    graph_store.load()
    state = _build_agent_state(s)
    llm = None if no_llm else LLMClient(s.llm, api_key=s.anthropic_api_key)

    def _progress(i: int, total: int, rel_path: str) -> None:
        if verbose:
            console.log(f"[{i + 1}/{total}] {rel_path}")

    stats = run_link(
        s, state, embedder, vector_store, graph_store, llm,
        only_note=note, dry_run=dry_run, progress=_progress,
    )

    table = Table(title=f"daemon link {'(dry-run)' if dry_run else ''}".strip())
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Notes scanned", str(stats.notes_scanned))
    table.add_row("Auto-applied wikilinks", str(stats.auto_applied_links))
    table.add_row("Auto-applied tags", str(stats.auto_applied_tags))
    table.add_row("Suggestions written", str(stats.suggestions_written))
    console.print(table)
    if stats.suggestions_file:
        console.print(f"[green]Review: {stats.suggestions_file}[/green]")
    if stats.errors:
        console.print(Panel("\n".join(stats.errors), title="Errors", border_style="red"))


@app.command()
def reflect(
    theme: str | None = typer.Option(None, "--theme", help="Restrict to one theme; default is all configured."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run the LLM but don't write any files."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Update themed memory files in <vault>/Agent/ (the 'digital embodiment')."""
    s = _load()
    if not s.agent.enabled and not dry_run:
        console.print(
            "[yellow]agent.enabled is false in config.yaml — refusing to write. "
            "Pass --dry-run to preview, or flip the flag once you're ready.[/yellow]"
        )
        raise typer.Exit(code=1)

    state = _build_agent_state(s)
    feedback = _build_feedback_store(s)
    llm = LLMClient(s.llm, api_key=s.anthropic_api_key)

    def _progress(i: int, total: int, theme_name: str) -> None:
        if verbose:
            console.log(f"[{i + 1}/{total}] theme: {theme_name}")

    stats = run_reflect(s, state, feedback, llm, only_theme=theme, dry_run=dry_run, progress=_progress)

    table = Table(title=f"daemon reflect {'(dry-run)' if dry_run else ''}".strip())
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Themes processed", str(stats.themes_processed))
    table.add_row("Themes skipped (unchanged)", str(stats.themes_skipped))
    console.print(table)
    if stats.rolling_journal_path:
        console.print(f"[green]Rolling journal: {stats.rolling_journal_path}[/green]")
    if stats.errors:
        console.print(Panel("\n".join(stats.errors), title="Errors", border_style="red"))


@models_app.command("download")
def models_download() -> None:
    """Pre-download the embedding model(s) into the local cache folder.

    After this, queries and ingest run fully offline (no HF Hub calls).
    Pulls the dense model always; pulls the sparse model too when hybrid is on.
    """
    s = _load()
    embedder = _build_embedder(s)
    cache = embedder.download()
    console.print(f"[green]Dense model '{s.embeddings.model}' ready in {cache} (dim={embedder.dimension})[/green]")

    if s.embeddings.hybrid:
        sparse = _build_sparse_embedder(s)
        assert sparse is not None  # hybrid=True guarantees a builder result
        sparse_cache = sparse.download()
        console.print(f"[green]Sparse model '{s.embeddings.sparse_model}' ready in {sparse_cache}[/green]")


@snapshot_app.command("create")
def snapshot_create(
    no_qdrant: bool = typer.Option(
        False,
        "--no-qdrant",
        help="Skip the vector snapshot — useful when Qdrant is offline or for graph/feedback-only bundles.",
    ),
) -> None:
    """Freeze vector + graph + feedback state into a new bundle under ./data/snapshots."""
    s = _load()
    warnings: list[str] = []
    bundle = create_snapshot(
        s,
        include_qdrant=not no_qdrant,
        on_warning=warnings.append,
    )
    console.print(f"[green]Created snapshot {bundle.id}[/green] → {bundle.dir}")
    if bundle.qdrant_snapshot:
        console.print(f"  qdrant: {bundle.qdrant_snapshot.name}")
    elif no_qdrant:
        console.print("  qdrant: skipped (--no-qdrant)")
    for w in warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")


@snapshot_app.command("list")
def snapshot_list() -> None:
    """List bundles in oldest→newest order."""
    s = _load()
    bundles = list_snapshots(s)
    if not bundles:
        console.print(f"[yellow]No snapshots in {s.snapshot.dir}.[/yellow]")
        return
    table = Table(title="Snapshots")
    table.add_column("id")
    table.add_column("created")
    table.add_column("qdrant")
    table.add_column("dir")
    for b in bundles:
        qdrant_cell = b.qdrant_snapshot.name if b.qdrant_snapshot else "—"
        table.add_row(b.id, b.created_at.isoformat(timespec="seconds"), qdrant_cell, str(b.dir))
    console.print(table)


@snapshot_app.command("delete")
def snapshot_delete(
    snapshot_id: str = typer.Argument(..., help="The snapshot id (timestamp form, e.g. 2026-05-17T03-00-00Z)."),
) -> None:
    """Remove one bundle directory. The server-side Qdrant snapshot is left alone."""
    s = _load()
    bundle = get_snapshot(s, snapshot_id)
    if bundle is None:
        console.print(f"[red]No snapshot with id {snapshot_id} in {s.snapshot.dir}.[/red]")
        raise typer.Exit(code=1)
    delete_snapshot(bundle)
    console.print(f"[green]Deleted snapshot {snapshot_id}[/green]")


@app.command()
def analyze(
    snapshot_id: str = typer.Argument(..., help="The snapshot id to analyze (from `daemon snapshot list`)."),
    lookback_days: int | None = typer.Option(
        None,
        "--lookback-days",
        help="Override the configured lookback window for the weight-evolution replay.",
    ),
    no_simulate: bool = typer.Option(
        False,
        "--no-simulate",
        help="Skip the weight-evolution replay (structural report only).",
    ),
    no_write: bool = typer.Option(
        False,
        "--no-write",
        help="Print the rich-table summaries but don't persist any JSON.",
    ),
) -> None:
    """Run M3 structural analysis on a snapshot bundle.

    Produces ``structural.json`` and (unless ``--no-simulate``) ``weight_evolution.json``
    under ``consolidation.out_dir/<snapshot_id>/``. Pure-Python — no LLM.
    """

    s = _load()
    bundle = get_snapshot(s, snapshot_id)
    if bundle is None:
        console.print(f"[red]No snapshot with id {snapshot_id} in {s.snapshot.dir}.[/red]")
        raise typer.Exit(code=1)

    cfg = s.consolidation
    handle = open_readonly(bundle)
    try:
        structural = compute_report(
            handle.graph,
            snapshot_id=bundle.id,
            max_communities=cfg.max_communities,
            max_bridging_notes=cfg.max_bridging_notes,
            max_bridge_edges=cfg.max_bridge_edges,
            max_orphans=cfg.max_orphans,
            max_dangling=cfg.max_dangling,
            max_warm_edges=cfg.max_warm_edges,
            betweenness_sample_k=cfg.betweenness_sample_k,
        )
    finally:
        handle.close()

    evolution = None
    if not no_simulate:
        evolution = simulate_evolution(
            bundle,
            lookback_days=lookback_days if lookback_days is not None else cfg.simulate_lookback_days,
        )

    _print_structural(structural)
    if evolution is not None:
        _print_evolution(evolution)

    if not no_write:
        out_dir = report_dir_for(bundle.id, root=cfg.out_dir)
        written = persist_reports(out_dir, structural=structural, evolution=evolution)
        for name, path in written.items():
            console.print(f"[green]wrote[/green] {name}: {path}")


def _print_structural(report) -> None:  # noqa: ANN001 — local helper, pydantic model
    summary = Table(title=f"Structural report ({report.snapshot_id or 'live'})")
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("notes", str(report.note_count))
    summary.add_row("tags", str(report.tag_count))
    summary.add_row("edges", str(report.edge_count))
    summary.add_row("communities", str(report.community_count))
    summary.add_row("bridging notes", str(len(report.bridging_notes)))
    summary.add_row("bridge edges", str(len(report.bridge_edges)))
    summary.add_row("orphan notes", str(len(report.orphan_notes)))
    summary.add_row("dangling targets", str(len(report.dangling_targets)))
    summary.add_row("warm edges", str(len(report.warm_edges)))
    console.print(summary)

    if report.communities:
        comm_table = Table(title="Top communities")
        comm_table.add_column("id", justify="right")
        comm_table.add_column("size", justify="right")
        comm_table.add_column("members (first few)")
        comm_table.add_column("top tags")
        for c in report.communities:
            members = ", ".join(c.members[:5]) + ("…" if len(c.members) > 5 else "")
            tags = ", ".join(f"#{t} ({n})" for t, n in c.top_tags[:3])
            comm_table.add_row(str(c.community_id), str(c.size), members, tags)
        console.print(comm_table)

    if report.bridging_notes:
        bn_table = Table(title="Bridging notes (sampled betweenness)")
        bn_table.add_column("note")
        bn_table.add_column("betweenness", justify="right")
        for b in report.bridging_notes:
            bn_table.add_row(b.note_path, f"{b.betweenness:.4f}")
        console.print(bn_table)

    if report.warm_edges:
        we_table = Table(title="Warmest edges (weight > threshold)")
        we_table.add_column("src")
        we_table.add_column("→")
        we_table.add_column("dst")
        we_table.add_column("kind")
        we_table.add_column("weight", justify="right")
        for e in report.warm_edges:
            we_table.add_row(e.src, "→", e.dst, e.kind, f"{e.weight:.3f}")
        console.print(we_table)


def _print_evolution(report) -> None:  # noqa: ANN001 — local helper, pydantic model
    head = Table(title=f"Weight-evolution preview ({report.lookback_days}d lookback)")
    head.add_column("metric")
    head.add_column("value", justify="right")
    head.add_row("events replayed", str(report.events_replayed))
    head.add_row("events skipped", str(report.events_skipped))
    head.add_row("edges shifted", str(len(report.top_edges)))
    head.add_row("notes touched", str(len(report.top_notes)))
    console.print(head)

    if report.top_edges:
        et = Table(title="Top edge deltas")
        et.add_column("src")
        et.add_column("→")
        et.add_column("dst")
        et.add_column("kind")
        et.add_column("before", justify="right")
        et.add_column("after", justify="right")
        et.add_column("Δ", justify="right")
        for d in report.top_edges:
            et.add_row(
                d.src, "→", d.dst, d.kind,
                f"{d.before:.3f}", f"{d.after:.3f}", f"{d.delta:+.3f}",
            )
        console.print(et)


@snapshot_app.command("prune")
def snapshot_prune(
    retention_days: int | None = typer.Option(
        None,
        "--retention-days",
        help="Override the configured retention window for this run.",
    ),
) -> None:
    """Delete bundles older than the retention window (default from config)."""
    s = _load()
    deleted = prune_snapshots(s, retention_days=retention_days)
    if not deleted:
        console.print("[green]Nothing to prune.[/green]")
        return
    for b in deleted:
        console.print(f"[yellow]pruned[/yellow] {b.id}")
    console.print(f"[green]Pruned {len(deleted)} snapshot(s).[/green]")


@app.command()
def consolidate(
    snapshot_id: str | None = typer.Option(
        None,
        "--snapshot",
        help="Reuse an existing snapshot bundle. Default: create a fresh one.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run the LLM but skip every write to the vault and the live graph.",
    ),
    no_qdrant: bool = typer.Option(
        False,
        "--no-qdrant",
        help="When creating a fresh snapshot, skip the Qdrant payload.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run the M4 consolidation loop: snapshot → analyze → observer letter → decay.

    Writes ``<vault>/Agent/observer-<date>.md`` and refreshes the rolling
    ``observer.md`` index. Gated by both ``agent.enabled`` AND
    ``agent.observer_enabled`` — the LLM is the most expensive and most
    opinion-laden of the writeback jobs, so it earns its own switch.
    """
    s = _load()
    if not s.agent.enabled and not dry_run:
        console.print(
            "[yellow]agent.enabled is false in config.yaml — refusing to write. "
            "Pass --dry-run to preview, or flip the flag once you're ready.[/yellow]"
        )
        raise typer.Exit(code=1)
    if not s.agent.observer_enabled and not dry_run:
        console.print(
            "[yellow]agent.observer_enabled is false in config.yaml — refusing to write. "
            "Flip the observer-specific flag (separate from agent.enabled) when you "
            "want the daemon to start writing letters.[/yellow]"
        )
        raise typer.Exit(code=1)

    feedback_store = _build_feedback_store(s)
    state = _build_agent_state(s)
    graph_store = _build_graph_store(s)
    graph_store.load()
    llm = LLMClient(s.llm, api_key=s.anthropic_api_key)

    def _progress(line: str) -> None:
        if verbose:
            console.log(line)

    try:
        stats = run_observe(
            s, state, feedback_store, graph_store, llm,
            snapshot_id=snapshot_id,
            dry_run=dry_run,
            include_qdrant=not no_qdrant,
            progress=_progress,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"daemon consolidate {'(dry-run)' if dry_run else ''}".strip())
    table.add_column("metric")
    table.add_column("value")
    table.add_row("snapshot id", stats.snapshot_id)
    table.add_row("model", stats.model_used)
    table.add_row("communities", str(stats.communities_seen))
    table.add_row("events replayed", str(stats.events_replayed))
    table.add_row("edges decayed", str(stats.edges_decayed))
    table.add_row("letter", str(stats.letter_path) if stats.letter_path else "(dry-run)")
    table.add_row("run id", str(stats.run_id) if stats.run_id else "—")
    console.print(table)
    for note in stats.notes:
        console.print(f"[yellow]note:[/yellow] {note}")
    if stats.errors:
        console.print(Panel("\n".join(stats.errors), title="Errors", border_style="red"))


@hermes_app.command("doctor")
def hermes_doctor() -> None:
    """Preflight the Hermes memory provider — config + readiness, no network.

    Reports whether ``is_available()`` would let Hermes activate the provider,
    and surfaces the capture/write-back settings and how many observer letters
    (dreams) exist to inject. Makes no Qdrant or Anthropic call.
    """
    from my_daemon.hermes import HERMES_AVAILABLE
    from my_daemon.hermes.provider import MyDaemonProvider

    s = _load()
    provider = MyDaemonProvider(settings=s)
    available = provider.is_available()

    agent_dir = s.vault.path / s.agent.folder_name
    letters = (
        sorted(p.name for p in agent_dir.glob("observer-*.md")) if agent_dir.is_dir() else []
    )

    table = Table(title="Hermes provider doctor")
    table.add_column("check")
    table.add_column("value")
    table.add_row("hermes ABC importable", "yes (in a Hermes venv)" if HERMES_AVAILABLE else "no (standalone)")
    table.add_row("provider_enabled", str(s.hermes.provider_enabled))
    table.add_row("vault exists", "yes" if s.vault.path.expanduser().exists() else f"NO ({s.vault.path})")
    table.add_row("is_available()", "[green]ready[/green]" if available else "[yellow]inactive[/yellow]")
    table.add_row("allow_write_back", str(s.hermes.allow_write_back))
    table.add_row("capture_folder", s.hermes.capture_folder)
    table.add_row("capture_requires_confirmation", str(s.hermes.capture_requires_confirmation))
    table.add_row("recall_top_k", str(s.hermes.recall_top_k))
    table.add_row("observer letters (dreams)", str(len(letters)) + (f" — latest {letters[-1]}" if letters else ""))
    console.print(table)

    if not available:
        if not s.hermes.provider_enabled:
            console.print(
                "[yellow]provider_enabled is false — flip hermes.provider_enabled in "
                "config.yaml once you're ready to let Hermes use My Daemon.[/yellow]"
            )
        if not s.vault.path.expanduser().exists():
            console.print(f"[red]Vault path does not exist: {s.vault.path}[/red]")
    if not s.hermes.allow_write_back:
        console.print(
            "[dim]Read-only: endorse/remember/sync_turn capture are no-ops until "
            "hermes.allow_write_back is true.[/dim]"
        )


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Wipe local state (data/ directory). The vault itself is never touched."""
    s = _load()
    data_dir = Path("./data").resolve()
    if not yes:
        confirm = Prompt.ask(f"This will delete {data_dir}. Type 'yes' to confirm")
        if confirm.strip().lower() != "yes":
            console.print("Aborted.")
            raise typer.Exit(code=1)
    if data_dir.is_dir():
        shutil.rmtree(data_dir)
        console.print(f"[green]Removed {data_dir}[/green]")
    else:
        console.print(f"[yellow]Nothing to remove at {data_dir}[/yellow]")
    _ = s  # quiet the unused-variable warning; loading validates config


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


@migrate_app.command("db")
def migrate_db() -> None:
    """Bring the state database up to the current schema version."""
    s = _load()
    db_path = s.feedback.db_path
    before = db_schema_version(db_path)
    applied = db_migrate(db_path)

    if not applied:
        console.print(
            f"[green]Already at schema v{before}[/green] — nothing to apply."
        )
        return

    names = {version: name for version, name, _ in DB_MIGRATIONS}
    for version in applied:
        console.print(f"  [cyan]v{version}[/cyan]  {names.get(version, '?')}")
    console.print(
        f"[green]Migrated[/green] {db_path} from v{before} to v{DB_SCHEMA_VERSION}."
    )


@migrate_app.command("status")
def migrate_status() -> None:
    """Show the state database's current schema version and what's pending."""
    s = _load()
    db_path = s.feedback.db_path
    found = db_schema_version(db_path)

    console.print(
        Panel(
            f"[bold]{db_path}[/bold]\n"
            f"current: [cyan]v{found}[/cyan]    target: [cyan]v{DB_SCHEMA_VERSION}[/cyan]",
            title="schema",
        )
    )

    pending = [(v, n) for v, n, _ in DB_MIGRATIONS if v > found]
    if not pending:
        console.print("[green]Up to date.[/green]")
        return

    table = Table(title="pending migrations")
    table.add_column("version", justify="right")
    table.add_column("name")
    for version, name in pending:
        table.add_row(str(version), name)
    console.print(table)


@app.command()
def activations(
    query_id: int | None = typer.Argument(None, help="Ledger query id. Omit to list recent queries."),
) -> None:
    """Show which notes fired for a query, and how strongly."""
    s = _load()
    ledger = ActivationLedger(db_path=s.feedback.db_path)
    registry = NoteRegistry(db_path=s.feedback.db_path)

    if query_id is None:
        table = Table(title="Recent queries")
        table.add_column("id", justify="right")
        table.add_column("when")
        table.add_column("surface")
        table.add_column("notes", justify="right")
        table.add_column("query")
        for row in ledger.recent(limit=20):
            table.add_row(
                str(row["id"]), row["ts"][:19], row["surface"],
                str(row["activation_count"]), row["text"][:60],
            )
        console.print(table)
        return

    rows = ledger.activations_for(query_id)
    if not rows:
        console.print(f"[yellow]No activations recorded for query {query_id}.[/yellow]")
        return
    paths = registry.paths_for(r.note_uuid for r in rows)
    table = Table(title=f"Activations for query {query_id}")
    table.add_column("note")
    table.add_column("source")
    table.add_column("strength", justify="right")
    table.add_column("rank", justify="right")
    for r in rows:
        table.add_row(
            paths.get(r.note_uuid, r.note_uuid), r.source,
            f"{r.strength:.3f}", str(r.rank or ""),
        )
    console.print(table)


@app.command("hot-notes")
def hot_notes(
    days: int | None = typer.Option(None, "--days", help="Restrict to the last N days."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Which notes your attention actually lands on."""
    from datetime import UTC, datetime, timedelta

    s = _load()
    ledger = ActivationLedger(db_path=s.feedback.db_path)
    registry = NoteRegistry(db_path=s.feedback.db_path)
    since = datetime.now(UTC) - timedelta(days=days) if days else None

    rows = ledger.hot_notes(limit=limit, since=since)
    if not rows:
        console.print("[yellow]No activations recorded yet.[/yellow]")
        return
    paths = registry.paths_for(u for u, _c, _s in rows)
    table = Table(title=f"Hot notes{f' (last {days}d)' if days else ''}")
    table.add_column("note")
    table.add_column("queries", justify="right")
    table.add_column("total strength", justify="right")
    for note_uuid, count, strength in rows:
        table.add_row(paths.get(note_uuid, note_uuid), str(count), f"{strength:.2f}")
    console.print(table)


@migrate_app.command("assign-uuids")
def migrate_assign_uuids(
    apply: bool = typer.Option(False, "--apply", help="Actually write. Dry-run otherwise."),
    path_glob: str | None = typer.Option(None, "--path-glob", help="Scope to matching notes, e.g. 'Projects/**'."),
    limit: int | None = typer.Option(None, "--limit", help="Stop after N notes."),
    grace_minutes: int = typer.Option(2, "--grace-minutes", help="Skip notes modified this recently."),
    no_backup: bool = typer.Option(False, "--no-backup", help="Skip the batch backup. Not recommended."),
) -> None:
    """Stamp a stable `uuid:` into every note's frontmatter."""
    s = _load()
    report = assign_uuids(
        s,
        apply=apply,
        path_glob=path_glob,
        limit=limit,
        grace_minutes=grace_minutes,
        backup=not no_backup,
    )

    table = Table(title="assign-uuids" + (" (DRY RUN)" if report.dry_run else ""))
    table.add_column("note")
    table.add_column("action")
    table.add_column("uuid", overflow="fold")
    for entry in report.entries[:50]:
        style = "yellow" if entry.action == "fallback" else ""
        table.add_row(entry.rel_path, entry.action, entry.uuid, style=style)
    console.print(table)
    if len(report.entries) > 50:
        console.print(f"[dim]… and {len(report.entries) - 50} more[/dim]")

    summary = (
        f"scanned {report.scanned}  ·  "
        + (f"would write {report.would_write}" if report.dry_run else f"written {report.written}")
        + f"  ·  already stamped {report.adopted}"
    )
    console.print(summary)

    if report.fallback:
        console.print(
            f"[yellow]{report.fallback} note(s) could not be written[/yellow] — they hold a "
            "path-derived identity, which is NOT stable across renames."
        )

    if report.dry_run:
        console.print("\n[cyan]Dry run.[/cyan] Re-run with --apply to write.")
    else:
        console.print(f"\nBackup: [dim]{report.backup_dir}[/dim]")
        console.print(f"Roll back with: [cyan]daemon migrate rollback-uuids {report.run_id}[/cyan]")


@migrate_app.command("list-runs")
def migrate_list_runs() -> None:
    """List `assign-uuids` runs available to roll back."""
    s = _load()
    runs = list_migration_runs(s)
    if not runs:
        console.print("[yellow]No migration runs found.[/yellow]")
        return
    for run_id in runs:
        console.print(run_id)


@migrate_app.command("rollback-uuids")
def migrate_rollback_uuids(
    run_id: str = typer.Argument(..., help="Run id from `daemon migrate list-runs`."),
    mode: str = typer.Option("key-removal", "--mode", help="key-removal | restore"),
    force: bool = typer.Option(False, "--force", help="restore mode: overwrite files edited since."),
    grace_minutes: int = typer.Option(2, "--grace-minutes"),
) -> None:
    """Undo an `assign-uuids` run."""
    s = _load()
    try:
        report = rollback_uuids(
            s, run_id, mode=mode, force=force, grace_minutes=grace_minutes
        )
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Reverted {len(report.reverted)} note(s)[/green] ({report.mode}).")
    if report.refused:
        console.print(
            f"[yellow]Refused {len(report.refused)}[/yellow] — changed since the migration. "
            "Use --force to overwrite (discards those edits)."
        )
        for rel in report.refused[:20]:
            console.print(f"  {rel}")


if __name__ == "__main__":
    app()
