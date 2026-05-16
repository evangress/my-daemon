"""NiceGUI chat window for the daemon.

Warm parchment palette, streaming responses, lazy retrieval pipeline.
The CLI's ``daemon chat`` command launches this; ``launch_chat`` is the entry point.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from nicegui import ui

from my_daemon.config import Settings, load_settings
from my_daemon.embeddings import Embedder
from my_daemon.llm import LLMClient
from my_daemon.models import FeedbackEvent
from my_daemon.retrieval import RetrievalOrchestrator
from my_daemon.stores import FeedbackStore, GraphStore, VectorStore

# Warm parchment palette — easier on the eyes than terminal black, less clinical than chatbot white.
BG_COLOR = "#F7EFE0"            # parchment cream
PANEL_COLOR = "#FFF9EE"         # lighter card surface
INK_COLOR = "#2E2A24"           # warm near-black for body text
ACCENT_COLOR = "#A8642B"        # copper / amber for the daemon
USER_BUBBLE = "#E9D8B7"         # soft tan for the human
DAEMON_BUBBLE = "#FFF4DD"       # softer cream for the daemon


@dataclass
class _DaemonContext:
    """Long-lived dependencies shared across UI events.

    Built once at startup so each send doesn't re-load the embedding model
    or re-parse the graph pickle.
    """

    settings: Settings
    embedder: Embedder
    vector_store: VectorStore
    graph_store: GraphStore
    feedback_store: FeedbackStore
    llm: LLMClient
    orchestrator: RetrievalOrchestrator


def _build_context() -> _DaemonContext:
    s = load_settings()
    embedder = Embedder(
        s.embeddings.model,
        batch_size=s.embeddings.batch_size,
        device=s.embeddings.device,
        cache_folder=s.embeddings.cache_folder,
    )
    vector_store = VectorStore(
        url=s.vector_store.qdrant.url,
        collection=s.vector_store.qdrant.collection,
        dim=embedder.dimension,
    )
    graph_store = GraphStore(path=s.graph.path)
    graph_store.load()
    feedback_store = FeedbackStore(db_path=s.feedback.db_path)
    llm = LLMClient(s.llm, api_key=s.anthropic_api_key)
    orchestrator = RetrievalOrchestrator(s, embedder, vector_store, graph_store)
    return _DaemonContext(s, embedder, vector_store, graph_store, feedback_store, llm, orchestrator)


async def _stream_into_label(label: ui.html, generator: Iterator[str], accumulator: list[str]) -> None:
    """Drain a sync text generator into a NiceGUI label without blocking the event loop."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def producer() -> None:
        try:
            for piece in generator:
                loop.call_soon_threadsafe(queue.put_nowait, piece)
        except Exception as exc:  # surface the error in the chat rather than dying silently
            loop.call_soon_threadsafe(queue.put_nowait, f"\n\n_(error: {exc})_")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=producer, daemon=True).start()

    while True:
        piece = await queue.get()
        if piece is None:
            return
        accumulator.append(piece)
        label.content = "".join(accumulator)


def _mount_ui(ctx: _DaemonContext) -> None:
    ui.colors(primary=ACCENT_COLOR)
    ui.add_head_html(
        f"""
        <style>
          body {{ background-color: {BG_COLOR}; }}
          .daemon-bubble {{
            background-color: {DAEMON_BUBBLE};
            color: {INK_COLOR};
            border-radius: 14px;
            padding: 14px 18px;
            max-width: 70ch;
            line-height: 1.55;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
            white-space: pre-wrap;
          }}
          .user-bubble {{
            background-color: {USER_BUBBLE};
            color: {INK_COLOR};
            border-radius: 14px;
            padding: 12px 16px;
            max-width: 70ch;
            line-height: 1.5;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
          }}
          .header-strip {{
            color: {ACCENT_COLOR};
            font-family: 'Georgia', 'Cambria', serif;
            letter-spacing: 0.02em;
          }}
        </style>
        """
    )

    with ui.column().classes("w-full max-w-3xl mx-auto p-6 gap-4"):
        with ui.row().classes("items-baseline w-full justify-between header-strip"):
            ui.label("✦ My Daemon").classes("text-3xl")
            ui.label(f"vault: {ctx.settings.vault.path.name}").classes("text-sm opacity-70")

        ui.label(
            "Ask. I'll search your notes and respond in your own voice."
        ).classes("text-sm").style(f"color: {INK_COLOR}; opacity: 0.65; font-style: italic")

        chat_column = ui.column().classes("w-full gap-3 pt-2")

        with ui.row().classes("w-full pt-4 gap-2 items-end"):
            input_box = ui.input(placeholder="What would you like to remember?").props(
                "outlined dense rounded autogrow"
            ).classes("flex-grow").style(f"background-color: {PANEL_COLOR}")
            send_btn = ui.button("Send").props("rounded color=primary")

    busy = {"flag": False}

    async def handle_send() -> None:
        query = (input_box.value or "").strip()
        if not query or busy["flag"]:
            return
        busy["flag"] = True
        input_box.value = ""
        send_btn.disable()

        with chat_column:
            with ui.row().classes("w-full justify-end"):
                ui.label(query).classes("user-bubble")
            with ui.row().classes("w-full justify-start"):
                daemon_label = ui.html("…").classes("daemon-bubble")

        t0 = datetime.now(UTC)
        try:
            result = await asyncio.to_thread(ctx.orchestrator.retrieve, query)

            if not result.ranked:
                daemon_label.content = (
                    "_Nothing in your notes matched. Try a different phrasing, "
                    "or ingest more of the vault._"
                )
                return

            accumulator: list[str] = []
            daemon_label.content = ""
            generator = ctx.llm.synthesize_stream(query, result.ranked)
            await _stream_into_label(daemon_label, generator, accumulator)
            answer = "".join(accumulator).strip()

            latency_ms = int((datetime.now(UTC) - t0).total_seconds() * 1000)
            ctx.feedback_store.log(
                FeedbackEvent(
                    timestamp=t0,
                    query=query,
                    retrieval_summary={
                        "ranked_count": len(result.ranked),
                        "seed_count": len(result.seeds),
                        "expanded_count": len(result.expanded),
                    },
                    answer=answer,
                    latency_ms=latency_ms,
                )
            )
        except Exception as exc:
            daemon_label.content = f"_(daemon error: {exc})_"
        finally:
            busy["flag"] = False
            send_btn.enable()
            input_box.run_method("focus")

    send_btn.on("click", handle_send)
    input_box.on("keydown.enter", handle_send)


def launch_chat(host: str = "127.0.0.1", port: int = 8765, native: bool = False) -> None:
    """Start the NiceGUI chat window.

    ``native=True`` opens a desktop window via pywebview (requires the optional
    extra). Otherwise the UI is reachable in a browser at ``http://<host>:<port>``.
    """
    ctx = _build_context()

    # NiceGUI 3.x requires either a script-file entry point or a ``root=`` callable
    # so it can rebuild the UI per client. We come in through the Typer CLI, not a
    # script, so we hand it a closure that mounts our UI.
    def root() -> None:
        _mount_ui(ctx)

    ui.run(
        root=root,
        title="My Daemon",
        host=host,
        port=port,
        native=native,
        dark=False,
        reload=False,
        show=not native,
        favicon="✦",
    )
