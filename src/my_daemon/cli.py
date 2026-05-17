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
from my_daemon.config import Settings, load_settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.llm import LLMClient
from my_daemon.pipeline import QueryEngine, ingest_vault
from my_daemon.pipeline.agent_extract import run_extract
from my_daemon.pipeline.agent_link import run_link
from my_daemon.pipeline.agent_reflect import run_reflect
from my_daemon.stores import AgentStateStore, FeedbackStore, GraphStore, VectorStore

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

    if verbose:
        console.log(
            f"seeds={len(response.retrieval.seeds)} expanded={len(response.retrieval.expanded)} "
            f"latency_ms={response.latency_ms} feedback_id={response.feedback_event_id}"
        )


@app.command()
def ask(text: str = typer.Argument(...)) -> None:
    """Alias for query."""
    query(text=text)


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


if __name__ == "__main__":
    app()
