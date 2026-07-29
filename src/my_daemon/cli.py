# SPDX-License-Identifier: Apache-2.0
"""Typer-based CLI: daemon init / ingest / query / status / search / graph / reset."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from my_daemon import __version__
from my_daemon.analysis import compute_report, persist_reports, simulate_evolution
from my_daemon.analysis.structural import report_dir_for
from my_daemon.config import (
    CONFIG_FILENAME,
    ConfigNotFoundError,
    Settings,
    load_settings,
    user_config_dir,
)
from my_daemon.doctor import (
    FAIL,
    MODEL_DOWNLOAD_SIZE,
    PASS,
    WARN,
    CheckResult,
    connection_error_types,
    run_checks,
    server_reachable,
    store_error_message,
)
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.integration.wiring import build_stores
from my_daemon.llm import LLMClient
from my_daemon.pipeline import QueryEngine, ingest_vault
from my_daemon.pipeline.agent_extract import run_extract
from my_daemon.pipeline.agent_link import run_link
from my_daemon.pipeline.agent_observe import run_observe
from my_daemon.pipeline.agent_reflect import run_reflect
from my_daemon.pipeline.backfill import backfill_activations
from my_daemon.pipeline.migrate_uuids import (
    assign_uuids,
    list_migration_runs,
    rollback_uuids,
)
from my_daemon.pipeline.theme_tags import apply_decision, propose_theme_tags
from my_daemon.retrieval.weights import apply_selection
from my_daemon.secrets import (
    ENV_VAR,
    delete_from_keychain,
    purge_dotenv_key,
    read_keychain,
    resolve_api_key,
    store_in_keychain,
)
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
from my_daemon.stores.graph import GraphLockTimeout
from my_daemon.stores.policy import RetrievalPolicyStore
from my_daemon.stores.snapshot import _backup_sqlite
from my_daemon.stores.themes import ThemeStore
from my_daemon.stores.vector import MEMORY_LOCATION, LocalStoreLockedError
from my_daemon.supervisor import (
    PreflightFailed,
    ScheduleUnit,
    Supervisor,
    UnsupportedPlatform,
    build_schedule_unit,
)

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
themes_app = typer.Typer(name="themes", help="Emergent themes and their tag proposals.")
app.add_typer(themes_app)
migrate_app = typer.Typer(name="migrate", help="Schema and identity migrations.")
app.add_typer(migrate_app)
schedule_app = typer.Typer(
    name="schedule", help="Generate the OS unit that runs `daemon run` unattended."
)
app.add_typer(schedule_app)

console = Console()

# Set by the app callback on every invocation, so it can never go stale between
# runs (the callback always fires, and always writes — None when --config was
# not passed).
_config_override: Path | None = None


_CONFIG_HELP = (
    "Config file to use, ahead of every other candidate. Default search order: "
    f"$MY_DAEMON_CONFIG, ./{CONFIG_FILENAME}, then the user config directory. "
    "When none exists the daemon refuses and names each location."
)


# B008 is Typer's whole calling convention; ruff exempts `@app.command()` but
# not `@app.callback()`.
@app.callback()
def main(
    config: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        metavar="PATH",
        help=_CONFIG_HELP,
    ),
) -> None:
    """My Daemon — a personal memory companion over your Obsidian vault."""
    global _config_override
    _config_override = config
    if config is not None:
        # Make the choice authoritative for the whole process, not just for
        # `_load()`: the chat GUI and the Hermes provider call `load_settings`
        # themselves, and `--config` would otherwise mean something different
        # depending on which surface you launched.
        os.environ["MY_DAEMON_CONFIG"] = str(config)


def _load() -> Settings:
    """Resolve settings, or refuse loudly.

    The old version caught a `FileNotFoundError` that `load_settings` never
    raised — a missing config silently became defaults, so a scheduled job ran
    happily against a vault that did not exist. Now the miss is typed and the
    exit code is the 1 the docs always claimed.
    """
    try:
        return load_settings(_config_override)
    except ConfigNotFoundError as exc:
        # No markup: the message carries filesystem paths, which may contain
        # square brackets that rich would eat. `soft_wrap` because a path
        # broken across a terminal-width boundary is not a path you can paste.
        console.print(str(exc), style="red", soft_wrap=True)
        raise typer.Exit(code=1) from exc


# Construction lives in `integration/wiring.py` so the CLI, the GUI and the
# Hermes core cannot drift apart again. These stay as thin shims because a
# dozen commands reference them.


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
    return VectorStore.from_config(s.vector_store.qdrant, dim=dim, hybrid=s.embeddings.hybrid)


def _build_graph_store(s: Settings) -> GraphStore:
    return GraphStore(path=s.graph.path)


def _build_feedback_store(s: Settings) -> FeedbackStore:
    return FeedbackStore(db_path=s.feedback.db_path)


def _build_agent_state(s: Settings) -> AgentStateStore:
    # Same SQLite file as feedback — one state file to back up / wipe.
    return AgentStateStore(db_path=s.feedback.db_path)


def _store_error_types() -> tuple[type[BaseException], ...]:
    """`LocalStoreLockedError` plus whatever "store unreachable" looks like today.

    Still a function, not a constant, so ``connection_error_types``' lazy
    qdrant/httpx imports stay off the CLI startup path.
    """

    return (LocalStoreLockedError, *connection_error_types())


def _require_reachable_server(s: Settings) -> None:
    """Fail on a dead Qdrant server before anything expensive happens.

    Reported live 2026-07-28: a config still in server mode from before
    embedded became the default downloaded ~130MB of embedding model and *then*
    failed with "cannot reach the vector store", because `ingest` built the
    embedder first to learn its dimension. The connection is the cheap check,
    so it goes first.

    Server mode only. Embedded mode has no server to probe, and opening the
    folder just to test it would fight its single-process lock.
    """

    qdrant = s.vector_store.qdrant
    # `is_embedded`, not `url is None`: `url` carries a historical default of
    # http://localhost:6333, so it is never None and testing it would probe a
    # server for every embedded config too.
    if qdrant.is_embedded:
        return
    if not server_reachable(qdrant.url):
        # Raised, not printed: `_store_errors` already renders exactly this
        # condition in one place, and two spellings of "Qdrant is down" is the
        # drift that wrapper exists to prevent.
        raise ConnectionError("connection refused")


@contextlib.contextmanager
def _store_errors(s: Settings) -> Iterator[None]:
    """Turn a dead vector store into one actionable line, at the CLI boundary.

    Qdrant being down is the single most common way a command fails, and it
    used to arrive as a `[Errno 111] Connection refused` traceback from deep
    inside retrieval. The translation lives in exactly one place — here, over
    `doctor.store_error_message` — so `query`, `ask`, `search` and `ingest`
    cannot drift into saying four different things about one condition.
    """

    try:
        yield
    except _store_error_types() as exc:
        if isinstance(exc, GraphLockTimeout):
            # `GraphLockTimeout` is a `TimeoutError`, so it lands here — but it
            # names the process holding the graph, and calling that "Qdrant is
            # unreachable" would send the reader to the wrong machine entirely.
            raise
        console.print(store_error_message(s, exc), style="red", soft_wrap=True)
        console.print("[dim]Run `daemon doctor` for the full preflight.[/dim]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        # Doctor checks that a key is *present*; only Anthropic can say whether
        # it's *valid*. A placeholder key therefore surfaces here, at first
        # synthesis — same contract as Qdrant-down: one line, exit 1.
        from anthropic import AuthenticationError

        if not isinstance(exc, AuthenticationError):
            raise
        console.print(
            "ANTHROPIC_API_KEY was rejected by the API (401). The key is set "
            "but not valid — run `daemon setup` to store the real one "
            "(retrieval still works without one: `daemon query --no-llm`).",
            style="red",
            soft_wrap=True,
        )
        raise typer.Exit(code=1) from exc


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"my-daemon {__version__}")


@app.command()
def init(
    vault: str | None = typer.Option(None, help="Path to your Obsidian vault."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config.yaml."),
    user: bool = typer.Option(
        False,
        "--user",
        help="Write into the user config directory instead of the current directory.",
    ),
) -> None:
    """Generate config.yaml + .env from examples, prompting for the vault path.

    Default target is the current directory, which keeps the repo-dev workflow
    exactly as it was. `--user` writes to the per-user config directory instead
    — the right choice for an installed daemon, since relative state paths
    anchor to wherever the config ends up living.
    """
    cwd = Path.cwd()
    dest = user_config_dir() if user else cwd
    config_path = dest / CONFIG_FILENAME
    env_path = dest / ".env"
    # Templates always come from the checkout you invoked from.
    config_example = cwd / "config.example.yaml"
    env_example = cwd / ".env.example"

    if config_path.exists() and not force:
        console.print(f"[yellow]{config_path} already exists. Use --force to overwrite.[/yellow]")
        raise typer.Exit(code=1)
    if not config_example.is_file():
        console.print(f"[red]Missing {config_example}. Run from the project root.[/red]")
        raise typer.Exit(code=1)

    # Spelled as a conditional rather than `or` so the prompt's return type is
    # inferred as `str` instead of picking up this variable's optionality.
    vault_path = (
        vault
        if vault
        else Prompt.ask(
            "Vault path",
            default="~/Documents/Obsidian/MyVault",
        )
    )
    dest.mkdir(parents=True, exist_ok=True)
    text = config_example.read_text(encoding="utf-8")
    text = text.replace("~/Documents/Obsidian/MyVault", vault_path)
    config_path.write_text(text, encoding="utf-8")
    console.print(f"[green]Wrote {config_path}[/green]")

    if not env_path.exists() and env_example.is_file():
        shutil.copy(env_example, env_path)
        console.print(f"[green]Wrote {env_path} — for optional MY_DAEMON_* overrides.[/green]")

    console.print(
        "[cyan]Next:[/cyan] run [bold]daemon setup[/bold] to store your ANTHROPIC_API_KEY "
        "in the OS credential store (it is never written to a file)."
    )

    if user:
        console.print(f"Relative paths in that file (./data/…) now resolve under {dest}.")


@app.command()
def ingest(
    full: bool = typer.Option(False, "--full", help="Force a full rebuild instead of incremental."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Ingest the vault into the vector store and the graph."""
    s = _load()

    def _progress(i: int, total: int, rel_path: str) -> None:
        if verbose:
            console.log(f"[{i + 1}/{total}] {rel_path}")

    with _store_errors(s):
        # Before the embedder: building that can pull ~130MB of model on a
        # first run, and it only goes first because `_build_vector_store`
        # needs its dimension. `_store_errors` renders whatever this raises.
        _require_reachable_server(s)
        embedder = _build_embedder(s)
        sparse_embedder = _build_sparse_embedder(s)
        vector_store = _build_vector_store(s, dim=embedder.dimension)
        graph_store = _build_graph_store(s)

        stats = ingest_vault(
            s,
            embedder,
            vector_store,
            graph_store,
            sparse_embedder=sparse_embedder,
            full_rebuild=full,
            progress=_progress,
        )

    table = Table(title="Ingest summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Notes scanned", str(stats.notes_scanned))
    table.add_row("Notes new/updated", str(stats.notes_new_or_updated))
    table.add_row("Notes renamed (no re-embed)", str(stats.notes_renamed))
    table.add_row("Notes metadata-refreshed", str(stats.notes_metadata_refreshed))
    table.add_row("Notes skipped (unchanged)", str(stats.skipped_unchanged))
    table.add_row("Notes deleted", str(stats.notes_deleted))
    table.add_row("Chunks upserted", str(stats.chunks_upserted))
    console.print(table)
    if stats.errors:
        console.print(Panel("\n".join(stats.errors), title="Errors", border_style="red"))


def _run_query(text: str, *, no_synthesize: bool = False, verbose: bool = False) -> None:
    """The body of `daemon query`, as a plain function.

    Typer commands are not ordinary callables: their unfilled parameters hold
    ``OptionInfo`` sentinels, which are truthy. `ask` used to call `query()`
    directly and every flag silently inverted — synthesis off, verbose on. Both
    commands go through here now, so a real default is the only kind there is.
    """

    s = _load()
    with _store_errors(s):
        stores = build_stores(s)
        engine = QueryEngine(
            s,
            stores.embedder,
            stores.vector_store,
            stores.graph_store,
            stores.feedback_store,
            stores.llm,
            sparse_embedder=stores.sparse_embedder,
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
        table.add_row(
            str(i), f"{rc.combined_score:.3f}", f"{rc.chunk.note_path}\n› {heading}", preview
        )
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
def query(
    text: str = typer.Argument(..., help="The question to ask your daemon."),
    no_synthesize: bool = typer.Option(
        False, "--no-llm", help="Skip LLM synthesis; show ranked context only."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Ask the daemon a question."""
    _run_query(text, no_synthesize=no_synthesize, verbose=verbose)


@app.command()
def ask(
    text: str = typer.Argument(..., help="The question to ask your daemon."),
    no_synthesize: bool = typer.Option(
        False, "--no-llm", help="Skip LLM synthesis; show ranked context only."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Alias for query."""
    _run_query(text, no_synthesize=no_synthesize, verbose=verbose)


@app.command()
def select(
    feedback_id: int = typer.Argument(
        ..., help="The feedback event id returned by `daemon query -v`."
    ),
    rank: int = typer.Argument(
        ..., help="Which candidate to select (1-based, matching the table)."
    ),
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
    with graph_store.transaction():
        result = apply_selection(
            graph_store,
            seed_note_uuid=seed_note,
            selected_note_uuid=selected_note,
        )

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
    with _store_errors(s):
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


_STATUS_STYLE = {
    PASS: "[green]pass[/green]",
    WARN: "[yellow]warn[/yellow]",
    FAIL: "[red]FAIL[/red]",
}


def _print_checks(results: list[CheckResult]) -> None:
    table = Table(title="daemon doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for result in results:
        table.add_row(result.name, _STATUS_STYLE[result.status], result.detail)
    console.print(table)

    for result in results:
        if result.status != PASS and result.hint:
            marker = "[red]→[/red]" if result.failed else "[yellow]→[/yellow]"
            console.print(f"{marker} [bold]{result.name}[/bold]: {result.hint}", soft_wrap=True)


@app.command()
def doctor() -> None:
    """Preflight every moving part and say what to do about each failure.

    Runs in dependency order — config, vault, vector store, collection, API
    key, model cache, state DB, graph — so the first failure is usually the
    cause of the rest. Exits 1 if any hard check fails; warnings (no
    collection yet, cold model cache, no API key) are reported and exit 0,
    because none of them stop the daemon from working.
    """

    results = run_checks(_config_override)
    _print_checks(results)

    failures = [r for r in results if r.failed]
    if failures:
        console.print(
            f"\n[red]{len(failures)} check(s) failed.[/red] "
            "Fix the first one and re-run — later checks often depend on it."
        )
        raise typer.Exit(code=1)
    warnings = [r for r in results if r.status == WARN]
    if warnings:
        console.print(f"\n[yellow]{len(warnings)} warning(s)[/yellow], nothing fatal.")
    else:
        console.print("\n[green]All checks passed.[/green]")


# ---------------------------------------------------------------------------
# daemon run — the foreground supervisor
# ---------------------------------------------------------------------------


@app.command()
def run(
    once: bool = typer.Option(
        False,
        "--once",
        help="Preflight, run one incremental ingest, then exit. The cron-friendly shape.",
    ),
) -> None:
    """Watch the vault and keep the daemon's memory current, in the foreground.

    Startup runs a doctor-lite preflight (vault, vector store) and refuses if
    either fails, then does one incremental ingest. After that it watches the
    vault for `.md` changes, ingests once the vault has been quiet for
    `run.debounce_seconds`, and runs `daemon consolidate` nightly at
    `run.consolidate_at` (when both writeback gates are open).

    Nothing here daemonizes — that is `daemon schedule`'s job. SIGINT/SIGTERM
    finish the work in flight, close the vector store and exit 0.
    """
    s = _load()
    supervisor = Supervisor(s, emit=_supervisor_log)
    try:
        with _store_errors(s):
            asyncio.run(supervisor.run(once=once))
    except PreflightFailed as exc:
        for failure in exc.failures:
            console.print(f"{failure.name}: {failure.detail}", style="red", soft_wrap=True)
            if failure.hint:
                console.print(f"  {failure.hint}", style="yellow", soft_wrap=True)
        console.print("[dim]Run `daemon doctor` for the full preflight.[/dim]")
        raise typer.Exit(code=1) from exc


def _supervisor_log(line: str) -> None:
    """One timestamped line. Plain enough for `journalctl`, which prefixes its
    own timestamp but not a local one, and never markup — these lines carry
    filesystem paths that rich would try to read as tags."""
    stamp = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim]{stamp}[/dim] {escape(line)}", soft_wrap=True)


# ---------------------------------------------------------------------------
# daemon schedule — generated units for the supervisor
# ---------------------------------------------------------------------------


def _schedule_unit() -> ScheduleUnit:
    s = _load()
    try:
        return build_schedule_unit(s)
    except (UnsupportedPlatform, ValueError) as exc:
        console.print(str(exc), style="red", soft_wrap=True)
        raise typer.Exit(code=1) from exc


def _print_unit(unit: ScheduleUnit) -> None:
    if unit.executable_missing:
        console.print(
            f"[yellow]Warning: {unit.executable} does not exist. The unit below would "
            "install cleanly and then fail at every boot. Install the package into this "
            "environment (`uv sync` / `pip install -e .`) and re-run.[/yellow]",
            soft_wrap=True,
        )
    console.print(unit.text, soft_wrap=True, markup=False, highlight=False)
    console.print("\nThen:", style="bold")
    for line in unit.instructions:
        console.print(f"  {line}", soft_wrap=True, markup=False, highlight=False)


@schedule_app.command("show")
def schedule_show() -> None:
    """Print the scheduler unit for this platform. Writes nothing."""
    unit = _schedule_unit()
    if unit.target is not None:
        console.print(f"[dim]# would be written to {unit.target}[/dim]", soft_wrap=True)
    _print_unit(unit)


@schedule_app.command("install")
def schedule_install(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the unit instead of writing it."),
) -> None:
    """Write the scheduler unit, and print the commands that activate it.

    The activation commands are never run for you: enabling a unit that will
    outlive this shell — and hold your vault's embedded vector store — is a
    decision to make with your eyes open, not a side effect of a subcommand.
    """
    unit = _schedule_unit()

    if unit.target is None:
        # Windows has no file to write; the unit *is* a command.
        console.print(
            "[yellow]Nothing to write on this platform — register the task with:[/yellow]"
        )
        _print_unit(unit)
        return

    if dry_run:
        console.print(f"[yellow]dry-run — would write {unit.target}[/yellow]", soft_wrap=True)
        _print_unit(unit)
        return

    unit.target.parent.mkdir(parents=True, exist_ok=True)
    unit.target.write_text(unit.text, encoding="utf-8")
    console.print(f"[green]Wrote {unit.target}[/green]", soft_wrap=True)
    _print_unit(unit)


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
    # Resolve the config here so a missing one is the same one-line refusal as
    # every other command. The GUI builds its own Settings a moment later; if
    # this succeeds, so will that.
    _load()

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
    all_: bool = typer.Option(
        False, "--all", help="Process every eligible note, not just changed ones."
    ),
    note: str | None = typer.Option(
        None, "--note", help="Vault-relative path; restrict to one note."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be processed; touch nothing."
    ),
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

    stats = run_extract(
        s, state, llm, all_=all_, only_note=note, dry_run=dry_run, progress=_progress
    )

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
    note: str | None = typer.Option(
        None, "--note", help="Vault-relative path; restrict to one note."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; touch nothing."),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Skip the LLM second-opinion on suggestions."
    ),
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
        s,
        state,
        embedder,
        vector_store,
        graph_store,
        llm,
        only_note=note,
        dry_run=dry_run,
        progress=_progress,
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
    theme: str | None = typer.Option(
        None, "--theme", help="Restrict to one theme; default is all configured."
    ),
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

    stats = run_reflect(
        s, state, feedback, llm, only_theme=theme, dry_run=dry_run, progress=_progress
    )

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
    console.print(
        f"[green]Dense model '{s.embeddings.model}' ready in {cache} (dim={embedder.dimension})[/green]"
    )

    if s.embeddings.hybrid:
        sparse = _build_sparse_embedder(s)
        assert sparse is not None  # hybrid=True guarantees a builder result
        sparse_cache = sparse.download()
        console.print(
            f"[green]Sparse model '{s.embeddings.sparse_model}' ready in {sparse_cache}[/green]"
        )


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
    snapshot_id: str = typer.Argument(
        ..., help="The snapshot id (timestamp form, e.g. 2026-05-17T03-00-00Z)."
    ),
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
    snapshot_id: str = typer.Argument(
        ..., help="The snapshot id to analyze (from `daemon snapshot list`)."
    ),
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
            lookback_days=lookback_days
            if lookback_days is not None
            else cfg.simulate_lookback_days,
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
                d.src,
                "→",
                d.dst,
                d.kind,
                f"{d.before:.3f}",
                f"{d.after:.3f}",
                f"{d.delta:+.3f}",
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
            s,
            state,
            feedback_store,
            graph_store,
            llm,
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
    letters = sorted(p.name for p in agent_dir.glob("observer-*.md")) if agent_dir.is_dir() else []

    table = Table(title="Hermes provider doctor")
    table.add_column("check")
    table.add_column("value")
    table.add_row(
        "hermes ABC importable", "yes (in a Hermes venv)" if HERMES_AVAILABLE else "no (standalone)"
    )
    table.add_row("provider_enabled", str(s.hermes.provider_enabled))
    table.add_row(
        "vault exists", "yes" if s.vault.path.expanduser().exists() else f"NO ({s.vault.path})"
    )
    table.add_row(
        "is_available()", "[green]ready[/green]" if available else "[yellow]inactive[/yellow]"
    )
    table.add_row("allow_write_back", str(s.hermes.allow_write_back))
    table.add_row("capture_folder", s.hermes.capture_folder)
    table.add_row("capture_requires_confirmation", str(s.hermes.capture_requires_confirmation))
    table.add_row("recall_top_k", str(s.hermes.recall_top_k))
    table.add_row(
        "observer letters (dreams)",
        str(len(letters)) + (f" — latest {letters[-1]}" if letters else ""),
    )
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


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover — unreachable, the loop returns first


def _path_size(path: Path) -> int:
    """Bytes on disk, for a file or a whole tree. Unreadable entries count as 0."""

    if path.is_file():
        with contextlib.suppress(OSError):
            return path.stat().st_size
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            with contextlib.suppress(OSError):
                total += child.stat().st_size
    return total


def _delete(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _compose_storage_dir(s: Settings) -> Path:
    """Where docker-compose mounts Qdrant's storage, relative to the config.

    Only ever consulted in server mode. The daemon does not own this directory
    — Docker does — which is exactly why it needs naming rather than sweeping
    up in a broader delete.
    """

    root = s.config_path.parent if s.config_path else Path.cwd()
    return root / "data" / "qdrant"


def _reset_targets(s: Settings, *, models: bool, everything: bool) -> list[tuple[str, Path]]:
    """(label, path) for everything this invocation is allowed to delete.

    Config-resolved, every one of them: the old version deleted `./data`
    relative to the process's CWD, which from cron meant `$HOME/data` — either
    nothing at all, or somebody else's.
    """

    targets: list[tuple[str, Path]] = [
        ("graph", s.graph.path),
        ("graph lock", GraphStore(path=s.graph.path).lock_path),
        ("ingest manifest", s.graph.manifest_path),
        ("snapshots", s.snapshot.dir),
        ("consolidation reports", s.consolidation.out_dir),
    ]
    if models:
        targets.append(("model cache", s.embeddings.cache_folder))
    if everything:
        db = s.feedback.db_path
        targets.append(("state db (learned weights, ledger, themes)", db))
        targets.append(("state db wal", db.with_name(db.name + "-wal")))
        targets.append(("state db shm", db.with_name(db.name + "-shm")))
    return [(label, path) for label, path in targets if path.exists()]


def _drop_server_collection(s: Settings) -> str:
    """Drop the live collection. Server mode only — embedded is covered by the path delete."""

    store = _build_vector_store(s, dim=1)
    try:
        store._client_().delete_collection(collection_name=store.collection)
        return f"[green]Dropped collection '{store.collection}' at {s.vector_store.qdrant.url}.[/green]"
    except Exception as exc:  # noqa: BLE001 — reported, never fatal: this is cleanup
        return (
            f"[yellow]Could not drop collection '{store.collection}' "
            f"at {s.vector_store.qdrant.url}: {exc}[/yellow]"
        )
    finally:
        store.close()


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt (for scripts)."),
    models: bool = typer.Option(
        False,
        "--models",
        help=f"Also delete the embedding model cache (a {MODEL_DOWNLOAD_SIZE} re-download).",
    ),
    everything: bool = typer.Option(
        False,
        "--all",
        help="Also delete feedback.db — learned weights, the activation ledger and themes. Irreplaceable.",
    ),
) -> None:
    """Delete the daemon's rebuildable state. The vault itself is never touched.

    Every target comes from the resolved config, is listed with its size before
    the prompt, and is deleted by name — nothing is swept up by deleting a
    parent directory. Three tiers, by cost of loss:

    * **default** — graph, manifest, vectors, snapshots, reports. All of it
      comes back from `daemon ingest --full`.
    * **`--models`** — the embedding cache. A re-download, not a loss.
    * **`--all`** — `feedback.db`. The learned edge weights, the activation
      ledger and the themes are *not* derivable from the vault. Nothing else
      in this command is irreversible; this is.

    In server mode the collection is dropped over HTTP rather than by deleting
    Qdrant's storage folder, which the daemon does not own and must never
    `rmtree` under a running container.
    """

    s = _load()
    qdrant = s.vector_store.qdrant
    targets = _reset_targets(s, models=models, everything=everything)
    notes: list[str] = []

    server_up = False
    embedded_path = qdrant.path  # non-None is exactly `qdrant.is_embedded`
    if embedded_path is not None:
        if embedded_path != MEMORY_LOCATION and Path(embedded_path).exists():
            targets.insert(0, ("vectors (embedded qdrant)", Path(embedded_path)))
    else:
        server_up = server_reachable(qdrant.url)
        storage = _compose_storage_dir(s)
        if not storage.is_dir():
            pass
        elif server_up:
            notes.append(
                f"[yellow]Refusing to delete {storage} while Qdrant answers at "
                f"{qdrant.url} — stop the container first (`docker compose down`) if you "
                "want the folder gone. Dropping the collection instead, which resets the "
                "vectors either way.[/yellow]"
            )
        else:
            targets.append(("vectors (qdrant server storage)", storage))

    if not targets and not (server_up and not qdrant.is_embedded):
        for note in notes:
            console.print(note, soft_wrap=True)
        console.print("[yellow]Nothing to remove — the state paths are already clear.[/yellow]")
        return

    table = Table(title="Reset targets")
    table.add_column("what")
    table.add_column("path", overflow="fold")
    table.add_column("size", justify="right")
    for label, path in targets:
        table.add_row(label, str(path), _human_size(_path_size(path)))
    if server_up:
        table.add_row(
            "vectors (server collection)", f"{qdrant.url} → {qdrant.collection}", "dropped"
        )
    console.print(table)
    for note in notes:
        console.print(note, soft_wrap=True)

    kept = []
    if not models:
        kept.append("the model cache (`--models` to include it)")
    if not everything:
        kept.append("feedback.db — learned weights, ledger, themes (`--all` to include it)")
    if kept:
        console.print("[dim]Keeping " + "; ".join(kept) + ".[/dim]")

    if not yes:
        confirm = Prompt.ask("Type 'yes' to delete the above")
        if confirm.strip().lower() != "yes":
            console.print("Aborted. Nothing was deleted.")
            raise typer.Exit(code=1)

    for label, path in targets:
        _delete(path)
        console.print(f"[green]Removed[/green] {label}: {path}")

    if not qdrant.is_embedded:
        if server_up:
            console.print(_drop_server_collection(s), soft_wrap=True)
        else:
            console.print(
                f"[yellow]Qdrant at {qdrant.url} is unreachable, so collection "
                f"'{qdrant.collection}' was not dropped.[/yellow] Start it and re-run, "
                "or let `daemon ingest --full` overwrite it.",
                soft_wrap=True,
            )

    console.print("\nRun `daemon ingest --full` to rebuild. The vault was not touched.")


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------

#: Bumped if the bundle layout ever changes incompatibly. `restore` refuses
#: anything it does not recognise rather than half-applying it.
BACKUP_BUNDLE_VERSION = 1

_BACKUP_METADATA = "metadata.json"
_BACKUP_PREFIX = "backup-"
_BACKUP_TIMESTAMP = "%Y-%m-%dT%H-%M-%SZ"

_BACKUP_VECTORS_NOTE = (
    "Vectors are not included by design — they are re-derivable from the vault. "
    "After a restore, run `daemon ingest --full` to rebuild them."
)


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime(_BACKUP_TIMESTAMP)


def _backup_sources(s: Settings) -> list[tuple[str, Path]]:
    """(name-in-bundle, live path) for everything a bundle carries.

    Deliberately short. This is the state that is *not* re-derivable from the
    vault, plus the config needed to make sense of it — nothing else earns a
    place in a file you want small enough to copy often.
    """

    sources = [
        ("feedback.db", s.feedback.db_path),
        ("graph.gpickle", s.graph.path),
        ("manifest.json", s.graph.manifest_path),
    ]
    if s.config_path is not None:
        sources.append(("config.yaml", s.config_path))
    return sources


def _write_bundle(s: Settings, bundle_dir: Path) -> dict:
    """Copy the live state into ``bundle_dir`` and return the metadata written."""

    bundle_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    for name, source in _backup_sources(s):
        if not source.is_file():
            continue
        dest = bundle_dir / name
        if name == "feedback.db":
            # SQLite's online-backup API, not shutil: a plain copy of a live
            # database misses everything still sitting in the -wal, and the
            # realistic moment to take a backup is with the GUI open.
            _backup_sqlite(source, dest)
        else:
            shutil.copy2(source, dest)
        files[name] = {"bytes": dest.stat().st_size, "source": str(source)}

    metadata = {
        "kind": "my-daemon-backup",
        "bundle_version": BACKUP_BUNDLE_VERSION,
        "id": bundle_dir.name,
        "created_at": datetime.now(UTC).isoformat(),
        "my_daemon_version": __version__,
        "schema_version": db_schema_version(s.feedback.db_path),
        "vectors": "excluded — rebuild with `daemon ingest --full`",
        "files": files,
    }
    (bundle_dir / _BACKUP_METADATA).write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    return metadata


def _read_bundle_metadata(bundle_dir: Path) -> dict:
    """Validate a bundle and return its metadata, or raise ``ValueError`` saying why."""

    if not bundle_dir.is_dir():
        raise ValueError(f"{bundle_dir} is not a directory")
    meta_path = bundle_dir / _BACKUP_METADATA
    if not meta_path.is_file():
        raise ValueError(f"{bundle_dir} has no {_BACKUP_METADATA} — not a backup bundle")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{meta_path} is unreadable: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("kind") != "my-daemon-backup":
        raise ValueError(f"{meta_path} does not describe a my-daemon backup")
    version = metadata.get("bundle_version")
    if version != BACKUP_BUNDLE_VERSION:
        raise ValueError(
            f"bundle version {version} is not the {BACKUP_BUNDLE_VERSION} this build writes"
        )
    return metadata


def _list_bundles(root: Path) -> list[tuple[Path, dict | None]]:
    if not root.is_dir():
        return []
    found: list[tuple[Path, dict | None]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            found.append((child, _read_bundle_metadata(child)))
        except ValueError:
            found.append((child, None))
    return found


def _replace_file(src: Path, dst: Path) -> None:
    """Land ``src`` on ``dst`` atomically, via a sibling temp file."""

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.restore-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


@app.command()
def backup(
    # B008 is Typer's calling convention; ruff exempts it only for the scalar
    # annotations, not for `Path`.
    dest: Path | None = typer.Argument(  # noqa: B008
        None, help="Where to write the bundle. Default: backup.dir."
    ),
    show_list: bool = typer.Option(
        False, "--list", help="List existing bundles instead of writing one."
    ),
) -> None:
    """Copy the state the vault cannot regenerate into a timestamped bundle.

    The bundle holds `feedback.db` (learned weights, activation ledger,
    themes), `graph.gpickle`, the ingest manifest, and a copy of the config
    that produced them, plus a small `metadata.json` recording versions and
    the schema version. Vectors are excluded on purpose.

    Unlike a snapshot, a bundle lives outside `./data` — the tree `daemon
    reset` clears — because a backup inside the blast radius is not a backup.
    """

    s = _load()
    root = dest or s.backup.dir
    if show_list:
        _print_backups(root)
        return

    bundle_dir = root / (_BACKUP_PREFIX + _timestamp())
    metadata = _write_bundle(s, bundle_dir)

    table = Table(title=f"Backup {bundle_dir.name}")
    table.add_column("file")
    table.add_column("size", justify="right")
    for name, info in sorted(metadata["files"].items()):
        table.add_row(name, _human_size(info["bytes"]))
    console.print(table)
    console.print(f"Written to {bundle_dir}", soft_wrap=True)
    console.print(f"[dim]{_BACKUP_VECTORS_NOTE}[/dim]")


def _print_backups(root: Path) -> None:
    bundles = _list_bundles(root)
    if not bundles:
        console.print(f"[yellow]No backups in {root}.[/yellow] Run `daemon backup` to make one.")
        return
    table = Table(title=f"Backups in {root}")
    table.add_column("bundle")
    table.add_column("created")
    table.add_column("schema", justify="right")
    table.add_column("size", justify="right")
    for path, metadata in bundles:
        if metadata is None:
            table.add_row(path.name, "[red]not a bundle[/red]", "—", _human_size(_path_size(path)))
            continue
        table.add_row(
            path.name,
            str(metadata.get("created_at", "?")),
            f"v{metadata.get('schema_version', '?')}",
            _human_size(_path_size(path)),
        )
    console.print(table)


@app.command()
def backups() -> None:
    """List the bundles in `backup.dir` (same as `daemon backup --list`)."""

    _print_backups(_load().backup.dir)


@app.command()
def restore(
    bundle: Path = typer.Argument(  # noqa: B008 — see `backup`
        ..., help="The backup bundle directory to restore."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt (for scripts)."),
) -> None:
    """Put a backup bundle's state back, keeping what it replaces.

    Validates the bundle first, refuses one written by a newer build (a state
    database is never migrated downwards), and copies the current state into a
    sibling `pre-restore-<timestamp>` folder before touching anything — so an
    accidental restore is itself undoable.
    """

    s = _load()
    try:
        metadata = _read_bundle_metadata(bundle)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=1) from exc

    bundle_schema = metadata.get("schema_version")
    if isinstance(bundle_schema, int) and bundle_schema > DB_SCHEMA_VERSION:
        console.print(
            f"[red]This bundle's state database is at schema v{bundle_schema}, newer than "
            f"the v{DB_SCHEMA_VERSION} this build understands.[/red] Upgrade my-daemon and "
            "try again — a database is never migrated downwards.",
            soft_wrap=True,
        )
        raise typer.Exit(code=1)

    restorable = [
        (name, bundle / name, live)
        for name, live in _backup_sources(s)
        if name != "config.yaml" and (bundle / name).is_file()
    ]
    if not restorable:
        console.print(f"[red]{bundle} holds none of the restorable state files.[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Restore {bundle.name}")
    table.add_column("file")
    table.add_column("onto", overflow="fold")
    for name, _src, live in restorable:
        table.add_row(name, str(live))
    console.print(table)
    console.print(
        f"[dim]created {metadata.get('created_at')} by my-daemon "
        f"{metadata.get('my_daemon_version')} at schema v{bundle_schema}[/dim]"
    )

    if not yes:
        confirm = Prompt.ask("Type 'yes' to replace the current state")
        if confirm.strip().lower() != "yes":
            console.print("Aborted. Nothing was restored.")
            raise typer.Exit(code=1)

    pre_restore = bundle.parent / f"pre-restore-{_timestamp()}"
    _write_bundle(s, pre_restore)
    console.print(f"[green]Current state saved to[/green] {pre_restore}", soft_wrap=True)

    # The graph lock is the one piece of cross-process serialisation the daemon
    # has; holding it across the whole swap keeps a GUI endorse-click from
    # saving a graph on top of the one being restored.
    graph_store = GraphStore(path=s.graph.path)
    with graph_store.lock():
        for name, src, live in restorable:
            _replace_file(src, live)
            if name == "feedback.db":
                # A -wal/-shm left from the replaced database would be applied
                # to the restored one. The bundle copy is already consistent.
                for suffix in ("-wal", "-shm"):
                    sidecar = live.with_name(live.name + suffix)
                    with contextlib.suppress(OSError):
                        sidecar.unlink()
            console.print(f"[green]Restored[/green] {name} → {live}")

    console.print(
        f"\n[dim]{_BACKUP_VECTORS_NOTE}[/dim]\n"
        "Run `daemon ingest --full` if the vault has changed since this backup was taken."
    )


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
        console.print(f"[green]Already at schema v{before}[/green] — nothing to apply.")
        return

    names = {version: name for version, name, _ in DB_MIGRATIONS}
    for version in applied:
        console.print(f"  [cyan]v{version}[/cyan]  {names.get(version, '?')}")
    console.print(f"[green]Migrated[/green] {db_path} from v{before} to v{DB_SCHEMA_VERSION}.")


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
    query_id: int | None = typer.Argument(
        None, help="Ledger query id. Omit to list recent queries."
    ),
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
                str(row["id"]),
                row["ts"][:19],
                row["surface"],
                str(row["activation_count"]),
                row["text"][:60],
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
            paths.get(r.note_uuid, r.note_uuid),
            r.source,
            f"{r.strength:.3f}",
            str(r.rank or ""),
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


@themes_app.command("list")
def themes_list() -> None:
    """Show the themes the dream phase has found."""
    s = _load()
    store = ThemeStore(db_path=s.feedback.db_path)
    all_themes = store.all()
    if not all_themes:
        console.print("[yellow]No themes yet — run `daemon consolidate`.[/yellow]")
        return

    table = Table(title="Themes")
    table.add_column("id", justify="right")
    table.add_column("label")
    table.add_column("status")
    table.add_column("queries", justify="right")
    table.add_column("runs", justify="right")
    for theme in all_themes:
        style = {"accepted": "green", "dormant": "dim", "rejected": "red"}.get(theme.status, "")
        table.add_row(
            str(theme.id),
            theme.label,
            theme.status,
            str(theme.query_count),
            str(theme.runs_seen),
            style=style,
        )
    console.print(table)


@themes_app.command("accept")
def themes_accept(
    theme_id: int = typer.Argument(..., help="Theme id from `daemon themes list`."),
    label: str | None = typer.Option(None, "--label", help="Rename it while accepting."),
) -> None:
    """Accept a theme. Its label is then yours — the observer never renames it."""
    s = _load()
    store = ThemeStore(db_path=s.feedback.db_path)
    theme = store.get(theme_id)
    if theme is None:
        console.print(f"[red]No theme {theme_id}.[/red]")
        raise typer.Exit(code=1)

    store.set_status(theme_id, "accepted")
    store.set_label(theme_id, label=label or theme.label, summary=theme.summary, locked=True)
    count = propose_theme_tags(store)
    console.print(
        f"[green]Accepted[/green] '{label or theme.label}'. "
        f"{count} tag proposal(s) queued — review with `daemon themes review`."
    )


@themes_app.command("reject")
def themes_reject(theme_id: int = typer.Argument(...)) -> None:
    """Reject a theme. It stays in the record, but proposes nothing."""
    s = _load()
    ThemeStore(db_path=s.feedback.db_path).set_status(theme_id, "rejected")
    console.print(f"[green]Rejected theme {theme_id}.[/green]")


@themes_app.command("review")
def themes_review(
    apply_all: bool = typer.Option(False, "--accept-all", help="Accept every pending proposal."),
    reject_all: bool = typer.Option(False, "--reject-all", help="Reject every pending proposal."),
) -> None:
    """Review pending theme-tag proposals, one note at a time."""
    s = _load()
    store = ThemeStore(db_path=s.feedback.db_path)
    registry = NoteRegistry(db_path=s.feedback.db_path)
    pending = store.pending_proposals()
    if not pending:
        console.print("[green]Nothing pending.[/green]")
        return

    paths = registry.paths_for(p.note_uuid for p in pending)
    for proposal in pending:
        rel = paths.get(proposal.note_uuid, proposal.note_uuid)
        if reject_all:
            decision = "rejected"
        elif apply_all:
            decision = "accepted"
        else:
            console.print(f"\n[bold]{rel}[/bold]  ← [cyan]{proposal.tag}[/cyan]")
            answer = Prompt.ask("  apply?", choices=["y", "n", "skip"], default="n")
            if answer == "skip":
                continue
            decision = "accepted" if answer == "y" else "rejected"

        result = apply_decision(
            s,
            store,
            registry,
            proposal.id,
            decision,
            grace_minutes=s.agent.write_grace_minutes,
        )
        mark = "[green]✓[/green]" if result.changed else "[dim]·[/dim]"
        console.print(f"  {mark} {rel}: {result.reason}")


@migrate_app.command("backfill-activations")
def migrate_backfill_activations(
    apply: bool = typer.Option(False, "--apply", help="Actually write. Dry-run otherwise."),
    days: int | None = typer.Option(
        None, "--days", help="Only feedback rows from the last N days."
    ),
) -> None:
    """Recover an activation ledger from the historical feedback log."""
    from datetime import UTC, datetime, timedelta

    s = _load()
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    report = backfill_activations(
        s.feedback.db_path,
        NoteRegistry(db_path=s.feedback.db_path),
        dry_run=not apply,
        since=since,
    )

    table = Table(title="backfill-activations" + (" (DRY RUN)" if report.dry_run else ""))
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Feedback rows scanned", str(report.feedback_rows_scanned))
    table.add_row(
        "Queries would write" if report.dry_run else "Queries written",
        str(report.queries_written),
    )
    table.add_row("Queries skipped", str(report.queries_skipped))
    table.add_row("Activations recovered", str(report.activations_written))
    table.add_row("Activations dropped", str(report.activations_dropped))
    console.print(table)

    if report.warning:
        console.print(f"[yellow]{report.warning}[/yellow]")
    if report.dry_run:
        console.print("\n[cyan]Dry run.[/cyan] Re-run with --apply to write.")


@migrate_app.command("assign-uuids")
def migrate_assign_uuids(
    apply: bool = typer.Option(False, "--apply", help="Actually write. Dry-run otherwise."),
    path_glob: str | None = typer.Option(
        None, "--path-glob", help="Scope to matching notes, e.g. 'Projects/**'."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Stop after N notes."),
    grace_minutes: int = typer.Option(
        2, "--grace-minutes", help="Skip notes modified this recently."
    ),
    no_backup: bool = typer.Option(
        False, "--no-backup", help="Skip the batch backup. Not recommended."
    ),
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
    force: bool = typer.Option(
        False, "--force", help="restore mode: overwrite files edited since."
    ),
    grace_minutes: int = typer.Option(2, "--grace-minutes"),
) -> None:
    """Undo an `assign-uuids` run."""
    s = _load()
    try:
        report = rollback_uuids(s, run_id, mode=mode, force=force, grace_minutes=grace_minutes)
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


@themes_app.command("tune")
def themes_tune(
    windows: int = typer.Option(
        4, "--windows", help="How many sequential replays of your history to score."
    ),
    min_cluster_size: int = typer.Option(3, "--min-cluster-size"),
    apply: bool = typer.Option(
        False, "--apply", help="Print the config edit for the recommended threshold."
    ),
) -> None:
    """Measure the theme match threshold against your own history.

    The threshold decides whether tonight's cluster inherits an existing
    theme's label or mints a new one, so it is what makes your themes stable or
    churny. It shipped as a judgement call; this replays your ledger at a range
    of values and reports what each would have done. Reads only — no themes are
    created, and your live theme store is untouched.
    """

    from my_daemon.analysis.theme_tuning import (
        DEFAULT_MATCH_THRESHOLD_SWEEP,
        recommend,
        sweep_match_threshold,
    )

    s = _load()
    ledger = ActivationLedger(db_path=s.feedback.db_path)
    if ledger.total_queries() == 0:
        console.print("[yellow]No queries recorded yet — nothing to tune against.[/yellow]")
        return

    with console.status("replaying your ledger…"):
        reports = sweep_match_threshold(
            ledger,
            thresholds=DEFAULT_MATCH_THRESHOLD_SWEEP,
            windows=windows,
            min_cluster_size=min_cluster_size,
        )

    best = recommend(reports)
    table = Table(title=f"Theme match threshold over {windows} windows")
    table.add_column("threshold", justify="right")
    table.add_column("mean churn", justify="right")
    table.add_column("themes", justify="right")
    table.add_column("created", justify="right")
    table.add_column("matched", justify="right")
    table.add_column("dormant", justify="right")
    table.add_column("avg cluster", justify="right")
    for report in reports:
        mark = " ←" if best and report.threshold == best.threshold else ""
        style = "green" if mark else ""
        table.add_row(
            f"{report.threshold:.2f}{mark}",
            f"{report.mean_churn:.2f}",
            str(report.themes_final),
            str(report.themes_created),
            str(report.themes_matched),
            str(report.themes_dormant),
            f"{report.mean_cluster_size:.1f}",
            style=style,
        )
    console.print(table)

    current = s.consolidation.theme_match_threshold
    if best is None:
        console.print(
            "[yellow]Not enough history to recommend a threshold yet.[/yellow] "
            "Themes need several consolidate runs' worth of queries before churn "
            f"means anything. Keeping [bold]{current:.2f}[/bold]."
        )
        return

    console.print(
        f"\nLowest churn with themes surviving: [bold green]{best.threshold:.2f}[/bold green] "
        f"(churn {best.mean_churn:.2f}, {best.themes_final} themes). "
        f"Currently configured: [bold]{current:.2f}[/bold]."
    )
    if apply:
        console.print(
            "\nAdd to your config.yaml:\n\n"
            "[dim]consolidation:\n"
            f"  theme_match_threshold: {best.threshold}[/dim]"
        )
    elif abs(best.threshold - current) > 1e-9:
        console.print("[dim]Re-run with --apply to see the config edit.[/dim]")


@app.command()
def policy() -> None:
    """Which retrieval policy your picks actually favour.

    Team-draft interleaving shows the seed ranking and the graph-expansion
    ranking equally often, so the win rates below are a fair comparison rather
    than a reflection of which one got the top slot.
    """

    s = _load()
    stats = RetrievalPolicyStore(db_path=s.feedback.db_path).stats()
    if not stats:
        console.print(
            "[yellow]No picks recorded yet.[/yellow] Use `daemon select` or click a "
            "candidate in the GUI, and this fills in."
        )
        return

    table = Table(title="Retrieval policy win rates")
    table.add_column("policy")
    table.add_column("shown", justify="right")
    table.add_column("picked", justify="right")
    table.add_column("win rate", justify="right")
    table.add_column("last win")
    for stat in stats:
        table.add_row(
            stat.policy,
            str(stat.impressions),
            str(stat.wins),
            f"{stat.win_rate:.1%}",
            stat.last_win_at.strftime("%Y-%m-%d") if stat.last_win_at else "—",
        )
    console.print(table)

    total = sum(stat.wins for stat in stats)
    if total < 20:
        console.print(
            f"[dim]{total} pick(s) so far — too few to read much into. "
            "Interleaving removes position bias, not sampling error.[/dim]"
        )


@graph_app.command("todos")
def graph_todos_cmd(
    limit: int = typer.Option(20, "--limit"),
    min_links: int = typer.Option(
        1, "--min-links", help="Only targets wanted by at least this many notes."
    ),
) -> None:
    """Notes you keep meaning to write.

    Every `[[Name]]` with nothing behind it is a decision you already made and
    have not carried out yet. Ranked by how many of your notes are reaching for
    it.
    """

    from my_daemon.analysis.todos import graph_todos

    s = _load()
    graph = GraphStore(path=s.graph.path)
    graph.load()

    todos = [t for t in graph_todos(graph, limit=limit) if t.incoming_links >= min_links]
    if not todos:
        console.print(
            "[green]No unwritten wikilinks.[/green] Every `[[link]]` in your vault "
            "has a note behind it."
        )
        return

    table = Table(title="Notes you keep meaning to write")
    table.add_column("target")
    table.add_column("wanted by", justify="right")
    table.add_column("linked from")
    for todo in todos:
        shown = ", ".join(todo.wanted_by[:3])
        if len(todo.wanted_by) > 3:
            shown += f", +{len(todo.wanted_by) - 3} more"
        table.add_row(todo.target, str(todo.incoming_links), shown or "—")
    console.print(table)


# ---------------------------------------------------------------------------
# `daemon key` — the credential store without a GUI
#
# `daemon setup` is a Tkinter window, which left a headless box — a server, an
# SSH session, the machine most likely to be running `daemon run` on a
# schedule — with no route to the encrypted store at all. The honest
# workaround was an environment variable, which is the layer this project
# deliberately moved away from.
#
# The key never travels through argv. A command-line argument is visible in
# `ps`, in shell history, and on Windows in Event 4688 process-creation logs;
# leaking it that way is exactly why `setx` was removed in the 2026-07-27
# rewrite, and a `--key` flag would quietly undo that.
# ---------------------------------------------------------------------------

key_app = typer.Typer(name="key", help="Store the Anthropic API key in your OS credential store.")
app.add_typer(key_app)

_KEY_PREFIX = "sk-ant-"


def _dotenv_candidates() -> list[Path]:
    """Where a legacy plaintext key might still be sitting.

    Config-optional on purpose: `daemon key set` has to work *before* there is
    a config, since storing the key is one of the first things a new install
    does.
    """

    paths: list[Path] = []
    try:
        settings = _load()
    except Exception:  # noqa: BLE001 — no config yet is the normal bootstrap case
        pass
    else:
        if settings.config_path is not None:
            paths.append(settings.config_path.parent / ".env")
    paths.append(Path.cwd() / ".env")
    return list(dict.fromkeys(paths))


def _key_fingerprint(value: str) -> str:
    """A one-way digest, so "did the rotation take?" is answerable.

    Deliberately not the last four characters, which is the usual shortcut:
    those are *part of the secret*, and this output lands in terminal
    scrollback and support-thread pastes.
    """

    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _warn_if_environment_shadows() -> None:
    if os.environ.get(ENV_VAR, "").strip():
        console.print(
            f"[yellow]Note:[/yellow] [bold]{ENV_VAR}[/bold] is set in this environment, "
            "and a real environment variable takes precedence over the credential "
            "store. The daemon will keep using that value and shadow what you just "
            "stored — unset it to let the stored key take effect."
        )


@key_app.command("set")
def key_set(
    from_stdin: bool = typer.Option(
        False, "--stdin", help="Read the key from stdin instead of prompting."
    ),
) -> None:
    """Store the Anthropic API key in your OS credential store.

    Prompts without echoing. Use `--stdin` to pipe it in — e.g.
    `pass show anthropic | daemon key set --stdin`. The key is never accepted
    as an argument, because argv is not private.
    """

    if from_stdin:
        value = sys.stdin.read().strip()
    else:
        value = typer.prompt("Anthropic API key", hide_input=True).strip()

    if not value:
        console.print("[red]No key given.[/red] Nothing was stored.")
        raise typer.Exit(1)

    if not value.startswith(_KEY_PREFIX):
        console.print(
            f"[yellow]Warning:[/yellow] that doesn't look like an Anthropic key "
            f"(they normally start with `{_KEY_PREFIX}`). Storing it anyway — the "
            "format is Anthropic's to change, and refusing a valid key would be "
            "worse than accepting one the API rejects."
        )

    ok, message = store_in_keychain(value)
    if not ok:
        console.print(f"[red]Could not reach an OS credential store.[/red] {message}")
        console.print(
            "On a headless Linux box this usually means no Secret Service is "
            f"running. See `daemon doctor`, or export [bold]{ENV_VAR}[/bold] "
            "as a fallback."
        )
        raise typer.Exit(1)

    console.print(f"[green]Stored.[/green] {message}")
    console.print(f"Key fingerprint: [bold]{_key_fingerprint(value)}[/bold] (sha256 prefix)")

    # Only after the store succeeded. Purging the last surviving copy of a key
    # we failed to save anywhere else would lose it outright.
    for path in _dotenv_candidates():
        if purge_dotenv_key(path):
            console.print(
                f"[green]Removed[/green] the plaintext key from [bold]{path}[/bold] "
                "(every other line left untouched)."
            )

    _warn_if_environment_shadows()


@key_app.command("clear")
def key_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Remove the stored API key from your OS credential store."""

    if read_keychain() is None:
        console.print("[yellow]Nothing to remove[/yellow] — no key in the credential store.")
        return

    if not yes and not typer.confirm("Delete the stored Anthropic API key?"):
        console.print("Left alone.")
        return

    if delete_from_keychain():
        console.print("[green]Deleted[/green] from the OS credential store.")
    else:
        console.print("[red]Could not delete[/red] — the credential store was unreachable.")
        raise typer.Exit(1)

    for path in _dotenv_candidates():
        if path.is_file() and ENV_VAR in path.read_text(encoding="utf-8"):
            console.print(
                f"[yellow]Note:[/yellow] a plaintext key is still in [bold]{path}[/bold]. "
                "Remove it by hand if you meant to revoke access here."
            )


@key_app.command("status")
def key_status() -> None:
    """Which layer supplies the API key — without printing it."""

    resolved = resolve_api_key(
        env_value=os.environ.get(ENV_VAR),
        dotenv_paths=_dotenv_candidates(),
    )
    described = {
        "environment": f"the [bold]{ENV_VAR}[/bold] environment variable",
        "keychain": "the OS credential store (encrypted at rest)",
        "dotenv": f"a plaintext `.env` file — [red]{resolved.dotenv_path}[/red]",
        "missing": "",
    }[resolved.source]

    if resolved.value is None:
        console.print(
            "[yellow]No key found[/yellow] in the environment, the OS credential "
            "store, or any `.env`. Run [bold]daemon key set[/bold]."
        )
        return

    console.print(f"Key resolves from: {described}")
    console.print(f"Key fingerprint:   [bold]{_key_fingerprint(resolved.value)}[/bold]")
    if resolved.is_plaintext:
        console.print(
            "\n[red]That file is readable by anyone with access to this machine.[/red] "
            "Run [bold]daemon key set[/bold] to move it into the credential store — "
            "it will strip the plaintext copy for you. Rotate the key afterwards: "
            "https://console.anthropic.com/settings/keys"
        )
