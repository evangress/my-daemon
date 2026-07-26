# SPDX-License-Identifier: Apache-2.0
"""NiceGUI chat window for the daemon.

Brand-aligned dark indigo palette per ../my-daemon-astro-website/BRAND.md:
the trichromatic story (gold question → violet daemon → cyan reply) is the
visual spine of the chat. Streaming responses, lazy retrieval pipeline.
The CLI's ``daemon chat`` command launches this; ``launch_chat`` is the entry point.
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nicegui import ui

from my_daemon.config import Settings, load_settings
from my_daemon.integration.core import DaemonCore, build_core
from my_daemon.models import RetrievalResult
from my_daemon.paths import log_path as _shared_log_path
from my_daemon.pipeline import build_retrieval_summary

log = logging.getLogger("my_daemon.chat")


def _configure_file_logging() -> Path:
    """Attach a rotating file handler to the root logger; return the log path.

    Called once at chat-window startup. Native mode hides stdout, so without
    this an unhandled exception would vanish into the void.
    """
    path = _shared_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    # Avoid duplicate handlers when launch_chat is somehow called twice in-process.
    if not any(getattr(h, "baseFilename", None) == str(path) for h in root.handlers):
        root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path

# Brand palette (OKLCH). Source: my-daemon-astro-website/BRAND.md § 2.
# A question (gold) calls a daemon (violet) which speaks back (cyan).
VIOLET = "oklch(72% 0.20 305)"   # the daemon — wordmark dot, primary CTA
CYAN = "oklch(78% 0.14 195)"     # synthesis / reply — daemon bubble accent
GOLD = "oklch(82% 0.155 75)"     # the human spark — user bubble accent, italic emphasis
# Surfaces — deep midnight indigo, slightly tinted as they rise.
BG = "oklch(14% 0.025 282)"
INK = "oklch(92% 0.018 90)"      # warm near-white body text
INK_MUTE = "oklch(92% 0.018 90 / 0.70)"
RULE = "oklch(60% 0.05 285 / 0.20)"  # hairline borders


@dataclass
class _DaemonContext:
    """Long-lived dependencies shared across UI events.

    Built once at startup so each send doesn't re-load the embedding model
    or re-parse the graph pickle.
    """

    settings: Settings
    core: DaemonCore


def _build_context() -> _DaemonContext:
    """One construction path — see `integration/wiring.py` for why."""
    s = load_settings()
    return _DaemonContext(settings=s, core=build_core(s))


def _render_memories(chat_column, memories) -> None:  # noqa: ANN001
    """The "you've been here before" strip — the GUI half of M-mem-5."""
    with chat_column, ui.row().classes("w-full justify-start"):
        rows = "".join(
            f"<div class='memory-row'><span class='memory-date'>{m.ts.date().isoformat()}</span>"
            f"<span class='memory-text'>{html.escape(m.text)}</span>"
            f"<span class='memory-notes'>{html.escape(', '.join(m.shared_notes))}</span></div>"
            for m in memories
        )
        ui.html(
            f"<div class='memory-panel'><div class='memory-title'>"
            f"You've been here before</div>{rows}</div>"
        )


def _render_sources(
    ctx: _DaemonContext,
    container: ui.column,
    result: RetrievalResult,
    feedback_event_id: int,
) -> None:
    """Add a clickable list of retrieved candidates under the daemon's reply.

    The user picks the one that actually fit — that pick attaches a
    ``candidate_selected`` signal to the feedback row and reinforces the
    graph path from the seed note to the chosen candidate's note via
    ``retrieval.weights.apply_selection``.
    """

    summary = build_retrieval_summary(result)
    ranked = summary["ranked"]
    if not ranked:
        return

    # Map chunk_id → preview text so we don't re-implement preview extraction.
    preview_by_id: dict[str, str] = {
        rc.chunk.id: rc.chunk.text.strip().replace("\n", " ")[:160] for rc in result.ranked
    }

    with container, ui.row().classes("w-full justify-start"):
        panel = ui.column().classes("sources-panel gap-2")
    cards: list[ui.column] = []
    locked = {"flag": False}

    with panel:
        ui.html('<div class="sources-prompt">Which one fit best?</div>')
        for i, entry in enumerate(ranked, start=1):
            card = ui.column().classes("source-card gap-0")
            cards.append(card)
            with card:
                # Note titles/paths are user-controlled vault content — escape
                # before splicing into ui.html, which renders raw HTML.
                path = html.escape(entry.get("note_path", "?"))
                heading = html.escape(" › ".join(entry.get("heading_path") or []))
                preview = html.escape(preview_by_id.get(entry.get("chunk_id", ""), ""))
                ui.html(
                    f'<span class="source-rank">#{i}</span>'
                    f'<span class="source-path">{path}</span>'
                )
                if heading:
                    ui.html(f'<div class="source-heading">› {heading}</div>')
                if preview:
                    ui.html(f'<div class="source-preview">{preview}…</div>')

        status = ui.html('<div class="sources-status"></div>')

    async def _pick(rank: int, entry: dict) -> None:
        if locked["flag"]:
            return
        locked["flag"] = True
        panel.classes(add="locked")
        cards[rank - 1].classes(add="picked")
        status.content = '<div class="sources-status">…recording your pick</div>'

        def _apply() -> str:
            # A click is explicit, so it is not gated by hermes.allow_write_back.
            res = ctx.core.endorse(feedback_event_id, rank, require_write_back=False)
            if not res.get("ok"):
                return res.get("reason", "could not record that pick")
            if res["edges_reinforced"] == 0:
                return "Recorded — this candidate was the seed itself; nothing to reinforce."
            hops = max(len(res["path"]) - 1, 0)
            return (
                f"Reinforced {res['edges_reinforced']} edge instance(s) "
                f"along a {hops}-hop path (Δweight {res['total_delta']:.2f})."
            )

        try:
            msg = await asyncio.to_thread(_apply)
            status.content = f'<div class="sources-status">{msg}</div>'
        except Exception as exc:
            log.exception("candidate selection failed")
            status.content = (
                f'<div class="sources-status">(error: {html.escape(str(exc))})</div>'
            )

    for i, entry in enumerate(ranked, start=1):
        # Capture i and entry by default-arg to dodge the late-binding closure pitfall.
        cards[i - 1].on("click", lambda _e, r=i, x=entry: _pick(r, x))


async def _stream_into_label(label: ui.markdown, generator: Iterator[str], accumulator: list[str]) -> None:
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
    # NiceGUI/Quasar primary maps to our violet — the daemon hue per brand § 2.
    ui.colors(primary=VIOLET, secondary=CYAN, accent=GOLD)
    ui.add_head_html(
        f"""
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,400;0,500;1,400;1,500&family=JetBrains+Mono:wght@400;500&family=Newsreader:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
        <style>
          :root {{
            --bg: {BG};
            --ink: {INK};
            --ink-mute: {INK_MUTE};
            --violet: {VIOLET};
            --cyan: {CYAN};
            --gold: {GOLD};
            --rule: {RULE};
            --font-body: 'Newsreader', Georgia, 'Cambria', serif;
            --font-display: 'Fraunces', Georgia, 'Cambria', serif;
            --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
          }}
          html, body {{
            background: var(--bg);
            color: var(--ink);
            font-family: var(--font-body);
            font-size: 16.5px;
            line-height: 1.65;
          }}
          /* Three radial gradients anchored at the corners — paper-warmth in the dark, per brand. */
          body::before {{
            content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
            background:
              radial-gradient(60ch at 12% 8%, oklch(72% 0.20 305 / 0.10), transparent 60%),
              radial-gradient(50ch at 92% 12%, oklch(78% 0.14 195 / 0.08), transparent 60%),
              radial-gradient(70ch at 50% 110%, oklch(40% 0.10 290 / 0.18), transparent 60%);
          }}
          .nicegui-content > * {{ position: relative; z-index: 1; }}

          /* Wordmark — violet dot + Fraunces small-caps, wide-tracked. Brand § 9. */
          .wordmark {{
            font-family: var(--font-display);
            font-weight: 500;
            font-variant-caps: all-small-caps;
            letter-spacing: 0.28em;
            color: var(--ink);
            font-size: 1.4rem;
            display: inline-flex; align-items: baseline; gap: 0.55em;
          }}
          .wordmark__dot {{
            width: 0.42em; height: 0.42em;
            border-radius: 50%;
            background: var(--violet);
            box-shadow: 0 0 12px oklch(72% 0.20 305 / 0.55);
            transform: translateY(-0.12em);
            display: inline-block;
          }}
          /* Tiny mono uppercase eyebrow — vault label, status chips. Brand § 3. */
          .eyebrow {{
            font-family: var(--font-mono);
            text-transform: uppercase;
            font-size: 0.68rem;
            letter-spacing: 0.32em;
            color: var(--ink-mute);
          }}
          /* Italic Fraunces whisper above the chat — the brand's default tone. */
          .epigraph {{
            font-family: var(--font-display);
            font-style: italic;
            font-size: 1.02rem;
            color: var(--ink-mute);
            opacity: 0.85;
          }}
          .epigraph .accent {{ color: var(--gold); }}

          /* Bubbles — bordered surfaces with a 2px hue stripe on the speaker's side.
             User on the right glows gold; daemon on the left glows cyan. */
          .user-bubble {{
            background: linear-gradient(135deg,
              oklch(22% 0.04 80 / 0.55),
              oklch(18% 0.03 282 / 0.35));
            border: 1px solid oklch(82% 0.155 75 / 0.18);
            border-right: 2px solid oklch(82% 0.155 75 / 0.55);
            color: var(--ink);
            font-family: var(--font-display);
            font-style: italic;
            font-size: 1.04rem;
            border-radius: 8px;
            padding: 12px 18px;
            max-width: 70ch;
            line-height: 1.55;
            box-shadow: 0 1px 3px rgba(0,0,0,0.30);
          }}
          .daemon-bubble {{
            background: linear-gradient(135deg,
              oklch(22% 0.04 200 / 0.55),
              oklch(18% 0.03 282 / 0.35));
            border: 1px solid oklch(78% 0.14 195 / 0.18);
            border-left: 2px solid oklch(78% 0.14 195 / 0.55);
            color: var(--ink);
            font-family: var(--font-body);
            font-size: 1.02rem;
            border-radius: 8px;
            padding: 14px 20px;
            max-width: 70ch;
            line-height: 1.7;
            box-shadow: 0 1px 3px rgba(0,0,0,0.30);
          }}
          .daemon-bubble p:first-child {{ margin-top: 0; }}
          .daemon-bubble p:last-child  {{ margin-bottom: 0; }}
          .daemon-bubble ul, .daemon-bubble ol {{ margin: 0.4em 0; padding-left: 1.4em; }}
          .daemon-bubble em {{ color: var(--gold); font-style: italic; }}
          .daemon-bubble strong {{ color: var(--ink); font-weight: 600; }}
          .daemon-bubble h1, .daemon-bubble h2, .daemon-bubble h3 {{
            font-family: var(--font-display);
            font-weight: 500;
            letter-spacing: -0.01em;
            margin: 0.6em 0 0.3em;
          }}
          .daemon-bubble code {{
            font-family: var(--font-mono);
            background-color: oklch(60% 0.05 285 / 0.18);
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 0.9em;
          }}
          .daemon-bubble a {{ color: var(--cyan); text-decoration: underline dotted; }}
          .daemon-bubble blockquote {{
            border-left: 2px solid oklch(82% 0.155 75 / 0.45);
            padding-left: 0.9em;
            color: var(--ink-mute);
            font-style: italic;
            margin: 0.5em 0;
          }}

          /* Underlined input, not boxed — brand § 9. Bottom hairline turns gold on focus. */
          .input-rule {{
            border-bottom: 1px solid var(--rule);
            transition: border-color 220ms ease;
            background: transparent;
          }}
          .input-rule:focus-within {{ border-bottom-color: var(--gold); }}
          .input-rule .q-field__control,
          .input-rule .q-field__control::before,
          .input-rule .q-field__control::after {{
            background: transparent !important;
            border: 0 !important;
          }}
          .input-rule input, .input-rule textarea, .input-rule .q-field__native {{
            color: var(--ink) !important;
            font-family: var(--font-body);
            font-size: 1.05rem;
            caret-color: var(--gold);
          }}
          .input-rule .q-field__native::placeholder {{
            color: oklch(92% 0.018 90 / 0.40);
            font-style: italic;
          }}

          /* Primary CTA — pill, mono micro-caps. Brand § 9. */
          .cta-pill {{
            border-radius: 9999px !important;
            padding: 0 1.4rem !important;
            min-height: 2.4rem !important;
          }}
          .cta-pill .q-btn__content {{
            font-family: var(--font-mono);
            font-size: 0.7rem;
            letter-spacing: 0.24em;
            text-transform: uppercase;
            font-weight: 500;
          }}

          /* Candidate sources panel — under each daemon reply, the user picks
             which retrieved chunk was actually the right one. The pick is
             implicit-feedback signal that reinforces the graph path. */
          .sources-panel {{
            max-width: 70ch;
            border-left: 2px solid oklch(82% 0.155 75 / 0.35);
            padding: 4px 0 4px 14px;
            margin-left: 6px;
            opacity: 0.96;
          }}
          .sources-panel.locked {{ opacity: 0.55; }}
          .source-card {{
            display: block;
            text-align: left;
            background: oklch(18% 0.03 282 / 0.45);
            border: 1px solid oklch(60% 0.05 285 / 0.20);
            border-radius: 6px;
            padding: 8px 12px;
            cursor: pointer;
            transition: border-color 180ms ease, background 180ms ease, transform 180ms ease;
            color: var(--ink);
          }}
          .source-card:hover {{
            border-color: oklch(82% 0.155 75 / 0.55);
            background: oklch(22% 0.04 80 / 0.40);
            transform: translateY(-1px);
          }}
          .source-card.picked {{
            border-color: oklch(82% 0.155 75 / 0.85);
            background: oklch(22% 0.04 80 / 0.55);
          }}
          .sources-panel.locked .source-card {{ cursor: default; transform: none; }}
          .sources-panel.locked .source-card:hover {{
            border-color: oklch(60% 0.05 285 / 0.20);
            background: oklch(18% 0.03 282 / 0.45);
          }}
          .source-rank {{
            font-family: var(--font-mono);
            color: var(--gold);
            font-size: 0.78rem;
            letter-spacing: 0.15em;
            margin-right: 0.6em;
          }}
          .source-path {{
            font-family: var(--font-body);
            font-size: 0.96rem;
            color: var(--ink);
          }}
          .source-heading {{
            font-family: var(--font-mono);
            font-size: 0.72rem;
            color: var(--ink-mute);
            letter-spacing: 0.04em;
            margin-top: 2px;
          }}
          .source-preview {{
            font-family: var(--font-display);
            font-style: italic;
            font-size: 0.92rem;
            color: var(--ink-mute);
            margin-top: 4px;
            line-height: 1.45;
          }}
          .sources-prompt {{
            font-family: var(--font-mono);
            text-transform: uppercase;
            font-size: 0.62rem;
            letter-spacing: 0.32em;
            color: var(--ink-mute);
            margin-bottom: 6px;
          }}
          .sources-status {{
            font-family: var(--font-display);
            font-style: italic;
            font-size: 0.86rem;
            color: var(--ink-mute);
            margin-top: 6px;
          }}
          /* "You've been here before" — past questions that lit up the same
             notes. Quieter than the sources panel: it is context, not an answer. */
          .memory-panel {{
            border-left: 2px solid var(--rule);
            padding: 8px 0 8px 14px;
            margin: 4px 0 2px 0;
            max-width: 46rem;
          }}
          .memory-title {{
            font-family: var(--font-mono);
            font-size: 0.7rem;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            color: var(--ink-mute);
            margin-bottom: 6px;
          }}
          .memory-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: baseline;
            font-size: 0.86rem;
            margin-bottom: 4px;
          }}
          .memory-date {{
            font-family: var(--font-mono);
            font-size: 0.72rem;
            color: var(--ink-mute);
          }}
          .memory-text {{ color: var(--ink); }}
          .memory-notes {{
            font-family: var(--font-mono);
            font-size: 0.72rem;
            color: var(--ink-mute);
          }}
        </style>
        """
    )

    with ui.column().classes("w-full max-w-3xl mx-auto px-6 py-10 gap-5"):
        # Header — wordmark on the left, mono eyebrow vault label on the right.
        with ui.row().classes("items-baseline w-full justify-between"):
            ui.html(
                '<span class="wordmark"><span class="wordmark__dot"></span>My Daemon</span>'
            )
            ui.html(
                f'<span class="eyebrow">vault · {ctx.settings.vault.path.name}</span>'
            )

        # A whisper, not a shout. Italic Fraunces at low opacity is the brand's default tone.
        ui.html(
            'Ask, and I will walk the vault with you<span class="accent">.</span>'
        ).classes("epigraph")

        chat_column = ui.column().classes("w-full gap-3 pt-4")

        with ui.row().classes("w-full pt-6 gap-3 items-end"):
            input_box = (
                ui.input(placeholder="What would you like to remember?")
                .props("borderless dense autogrow")
                .classes("flex-grow input-rule")
            )
            send_btn = ui.button("Send").props("unelevated color=primary").classes("cta-pill")

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
                daemon_label = ui.markdown("…").classes("daemon-bubble")

        try:
            # `ask_stream` retrieves, recalls, streams, and logs. The GUI used
            # to reproduce all four and drifted out of sync with the CLI.
            stream = ctx.core.ask_stream(query, surface="gui")
            accumulator: list[str] = []
            daemon_label.content = ""
            await _stream_into_label(daemon_label, iter(stream), accumulator)

            if not stream.retrieval.ranked:
                daemon_label.content = (
                    "_Nothing in your notes matched. Try a different phrasing, "
                    "or ingest more of the vault._"
                )
                return

            if stream.memories and ctx.settings.memory.show_to_user:
                _render_memories(chat_column, stream.memories)
            if stream.feedback_event_id is not None:
                _render_sources(
                    ctx, chat_column, stream.retrieval, stream.feedback_event_id
                )
        except Exception as exc:
            log.exception("chat send failed")
            daemon_label.content = f"_(daemon error: {exc})_"
        finally:
            busy["flag"] = False
            send_btn.enable()
            input_box.run_method("focus")

    send_btn.on("click", handle_send)
    input_box.on("keydown.enter", handle_send)


def launch_chat(host: str = "127.0.0.1", port: int = 8765, native: bool = True) -> None:
    """Start the NiceGUI chat window.

    ``native=True`` (the default) opens a desktop window via pywebview, which
    is a required dependency. Pass ``native=False`` to expose the UI as a
    browser tab at ``http://<host>:<port>`` — useful for remote dev work.
    """
    log_path = _configure_file_logging()
    log.info("launching chat host=%s port=%d native=%s log=%s", host, port, native, log_path)
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
        dark=True,
        reload=False,
        show=not native,
        favicon="✦",
    )
