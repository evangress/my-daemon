# SPDX-License-Identifier: Apache-2.0
"""Hermes memory-provider plugin — the PRIMARY face onto My Daemon.

This is Path A of PLAN-HERMES.md: a native Hermes memory provider. When active,
Hermes automatically injects our memory block into the system prompt, prefetches
relevant memories before every turn, syncs each turn back, and exposes our
explicit tools. The hooks map almost one-to-one onto :class:`DaemonCore`.

My Daemon never hard-depends on Hermes. The ``MemoryProvider`` ABC is resolved
**lazily** at import: when Hermes is importable we subclass the real ABC; when
it isn't (e.g. this repo's own test run) we fall back to ``object`` so the
module still imports and is unit-testable. The lone external dependency is the
MIT-licensed Hermes host process — nothing is added to ``my_daemon``'s runtime
deps (see PLAN-HERMES §10).

Off-device note: My Daemon is **local-only** (Qdrant, SQLite, the filesystem).
Conversation captures stay on disk; nothing leaves the device except the user's
own configured Anthropic calls, which Hermes never sees (the key stays daemon-side).

Signatures follow the verified Hermes interface in PLAN-HERMES's appendix. If a
future Hermes release tweaks a hook's signature, adjust it here — the core
adapter underneath is unaffected.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import threading
from pathlib import Path
from typing import Any

from my_daemon.config import Settings, load_settings
from my_daemon.integration.core import DaemonCore, build_core
from my_daemon.stores.activations import HERMES_PREFETCH_SURFACE

# Candidate import paths for Hermes's memory-provider ABC, most-likely first.
_ABC_CANDIDATES = (
    "hermes.agent.memory_provider",
    "hermes_agent.agent.memory_provider",
    "hermes_agent.memory_provider",
    "agent.memory_provider",
)


def _resolve_memory_provider_base() -> type:
    """Return Hermes's ``MemoryProvider`` ABC if importable, else ``object``."""
    for modpath in _ABC_CANDIDATES:
        try:
            module = importlib.import_module(modpath)
        except ImportError:
            continue
        base = getattr(module, "MemoryProvider", None)
        if isinstance(base, type):
            return base
    return object


_MemoryProviderBase = _resolve_memory_provider_base()

#: True when subclassing the real Hermes ABC (i.e. running inside Hermes).
HERMES_AVAILABLE = _MemoryProviderBase is not object

_DEFAULT_IDENTITY = (
    "You are speaking with the help of *My Daemon* — a local, private memory "
    "layer over the user's own notes and past conversations. The recalled "
    "fragments below are grounded in their words, not invented; cite the note "
    "a claim came from, and say plainly when memory is silent on something. "
    "The letter that follows is your daemon's own reflection on what it has "
    "been noticing lately — read it as continuity, in your own voice."
)


class MyDaemonProvider(_MemoryProviderBase):  # type: ignore[misc,valid-type]
    """Memory provider backed by :class:`DaemonCore`.

    Construct with no arguments in production (Hermes does this via the plugin
    shim's ``register``); pass ``settings`` / ``core`` to inject for tests.
    """

    name = "my-daemon"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        core: DaemonCore | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._core = core
        self._config_path = config_path
        self._session_id = ""
        self._hermes_home: Path | None = None
        # session_id -> {"feedback_event_id": int, "candidates": [...]}, the most
        # recent prefetch, so sync_turn can soft-reinforce what the assistant used.
        self._last_prefetch: dict[str, dict] = {}
        self._pending: list[threading.Thread] = []
        self._pending_lock = threading.Lock()

    # -- settings / lifecycle ---------------------------------------------

    def _settings_obj(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(self._config_path)
        return self._settings

    def is_available(self) -> bool:
        """Cheap, network-free probe (Hermes contract). False disables the provider.

        Returns False unless the vault path exists and ``provider_enabled`` is
        set — the latter is the kill-switch so a misconfigured Hermes never
        starts touching memory by accident.
        """
        try:
            s = self._settings_obj()
        except Exception:  # noqa: BLE001 — a bad config means "not available", not a crash
            return False
        if not s.hermes.provider_enabled:
            return False
        return Path(s.vault.path).expanduser().exists()

    def initialize(self, session_id: str = "", **kwargs: Any) -> None:
        """Build the stores once and ensure the vector collection exists.

        Hermes passes ``hermes_home`` (profile isolation) among kwargs.
        """
        self._session_id = session_id or ""
        home = kwargs.get("hermes_home")
        self._hermes_home = Path(home) if home else None
        if self._core is None:
            self._core = build_core(self._settings_obj())
        # One network touch at session start is acceptable; tolerate Qdrant down.
        with contextlib.suppress(Exception):
            self._core.vector_store.ensure_collection()

    def shutdown(self) -> None:
        self._drain()

    # -- ambient read: prefetch + system prompt ---------------------------

    def system_prompt_block(self) -> str:
        """Session-start block: identity framing + last night's letter (the dream).

        Injected once into the volatile tier. Degrades gracefully to just the
        framing when the daemon hasn't dreamed yet.
        """
        s = self._settings_obj()
        identity = s.hermes.identity.strip() or _DEFAULT_IDENTITY
        parts = [identity]
        dream = self._core.latest_dream() if self._core else None
        if dream:
            body = dream["body"]
            budget = s.hermes.dream_block_budget_chars
            if len(body) > budget:
                body = body[:budget].rstrip() + "…"
            parts.append(f"### Your daemon's latest letter ({dream['date']})\n\n{body}")
        else:
            parts.append(
                "_No observer letters yet — your daemon hasn't dreamed. "
                "Run `daemon consolidate` to write the first one._"
            )
        return "\n\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Ambient associative recall before each turn → a cited context block.

        Logs a FeedbackEvent (inside ``recall``) and caches its id + candidates
        so ``sync_turn`` can reinforce whichever note the assistant actually used.

        Recorded under the **ambient** surface. Nobody asked for this lookup —
        it fires on every turn — so it must not be read back as evidence of what
        the user has been thinking about (fingerprint recall, theme clustering).
        The deliberate ``mydaemon_recall`` tool keeps the intentional surface.
        """
        if self._core is None:
            return ""
        s = self._settings_obj()
        block = self._core.recall_block(
            query,
            budget_chars=s.hermes.prefetch_budget_chars,
            top_k=s.hermes.recall_top_k,
            surface=HERMES_PREFETCH_SURFACE,
        )
        self._last_prefetch[session_id or self._session_id] = {
            "feedback_event_id": block.feedback_event_id,
            "candidates": block.candidates,
        }
        return block.text

    # -- write-back: turn capture + soft reinforcement --------------------

    def sync_turn(self, user: str, assistant: str, *, session_id: str = "") -> None:
        """Capture + reinforce after each turn. **Must be non-blocking** — runs
        on a daemon thread so Hermes's loop never stalls on us (Hermes contract).
        """
        if self._core is None or not self._settings_obj().hermes.allow_write_back:
            return
        sid = session_id or self._session_id
        thread = threading.Thread(
            target=self._sync_turn_worker,
            args=(user, assistant, sid),
            daemon=True,
        )
        with self._pending_lock:
            self._pending.append(thread)
        thread.start()

    def _sync_turn_worker(self, user: str, assistant: str, sid: str) -> None:
        # A background thread must never raise — swallow and move on.
        try:
            self._reinforce_used(assistant, sid)
            if self._is_salient(user, assistant):
                self._core.remember(  # type: ignore[union-attr]
                    self._format_turn(user, assistant),
                    title=self._turn_title(user),
                    source="hermes",
                    confirmed=False,
                    session_id=sid,
                )
        except Exception:  # noqa: BLE001 — never propagate from the capture thread
            pass

    def _reinforce_used(self, assistant: str, sid: str) -> None:
        """Soft, positive-only reinforcement: if the assistant leaned on a
        prefetched note (its filename stem appears in the reply), endorse that
        seed→note path. Matches the project's no-negative-decrements stance."""
        pf = self._last_prefetch.get(sid)
        if not pf or not pf.get("candidates"):
            return
        reply = assistant.lower()
        for c in pf["candidates"]:
            stem = Path(c["note_path"]).stem.lower()
            if stem and stem in reply:
                self._core.endorse(pf["feedback_event_id"], c["rank"])  # type: ignore[union-attr]
                break

    def _is_salient(self, user: str, assistant: str) -> bool:
        return len((user or "").strip()) + len((assistant or "").strip()) >= (
            self._settings_obj().hermes.capture_min_chars
        )

    @staticmethod
    def _format_turn(user: str, assistant: str) -> str:
        return f"**User:** {user.strip()}\n\n**Assistant:** {assistant.strip()}\n"

    @staticmethod
    def _turn_title(user: str) -> str:
        flat = " ".join(user.split())
        return flat[:60].rstrip() + ("…" if len(flat) > 60 else "") or "conversation"

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror Hermes's durable MEMORY.md/USER.md facts into the vault.

        These are higher-trust than ambient turn capture, so they land
        ``status: confirmed``.
        """
        if self._core is None or not self._settings_obj().hermes.allow_write_back:
            return
        with contextlib.suppress(Exception):
            self._core.remember(
                content,
                title=f"Hermes memory · {target}",
                source="hermes-memory",
                confirmed=True,
                session_id=self._session_id,
            )

    def on_session_end(self, messages: list[Any] | None = None) -> None:
        """Final flush: let any in-flight capture threads finish."""
        self._drain()

    def _drain(self, *, timeout: float = 30.0) -> None:
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
        for thread in pending:
            thread.join(timeout=timeout)

    # -- deliberate tools -------------------------------------------------

    def get_tool_schemas(self) -> list[dict]:
        """Anthropic-style tool schemas Hermes registers into its tool registry.

        Read tools always; the two write tools appear only when write-back is on
        (so the agent isn't offered a tool that would refuse).
        """
        schemas: list[dict] = [
            {
                "name": "mydaemon_recall",
                "description": (
                    "Search the user's personal memory (their notes + past "
                    "conversations) and return ranked, cited candidates. "
                    "Retrieval-only by default — you do the reasoning."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to recall."},
                        "top_k": {"type": "integer", "description": "Max candidates (default 8)."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mydaemon_dream",
                "description": (
                    "Read one of the daemon's observer letters (its reflections). "
                    "Omit 'date' for the latest."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD; omit for latest."},
                    },
                },
            },
            {
                "name": "mydaemon_neighbors",
                "description": "List notes adjacent to a given note in the memory graph.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "note_path": {"type": "string", "description": "Vault-relative note path."},
                        "depth": {"type": "integer", "description": "Hops to expand (default 1)."},
                    },
                    "required": ["note_path"],
                },
            },
        ]
        if self._settings_obj().hermes.allow_write_back:
            schemas.extend(
                [
                    {
                        "name": "mydaemon_endorse",
                        "description": (
                            "Reinforce the memory path behind a recall candidate "
                            "you found genuinely useful, so it surfaces more easily later."
                        ),
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "feedback_event_id": {
                                    "type": "integer",
                                    "description": "The id returned by a prior recall.",
                                },
                                "rank": {
                                    "type": "integer",
                                    "description": "1-based rank of the candidate to endorse.",
                                },
                            },
                            "required": ["feedback_event_id", "rank"],
                        },
                    },
                    {
                        "name": "mydaemon_remember",
                        "description": (
                            "Capture a fact worth keeping into the user's vault so "
                            "it becomes recallable later."
                        ),
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "The fact to remember."},
                                "title": {"type": "string", "description": "Optional short title."},
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional tags.",
                                },
                                "confirmed": {
                                    "type": "boolean",
                                    "description": "True if the user explicitly confirmed this fact.",
                                },
                            },
                            "required": ["text"],
                        },
                    },
                ]
            )
        return schemas

    def handle_tool_call(self, name: str, arguments: dict | None) -> str:
        """Dispatch a tool call to the core. Returns a string (JSON for structured
        results, markdown for a letter) so Hermes can feed it straight back."""
        if self._core is None:
            return "My Daemon is not initialized."
        args = arguments or {}
        if name == "mydaemon_recall":
            data = self._core.recall(
                args["query"],
                top_k=args.get("top_k"),
                synthesize=False,
            )
            return json.dumps(data, default=str)
        if name == "mydaemon_dream":
            day = args.get("date")
            letter = self._core.read_dream(day) if day else self._core.latest_dream()
            if not letter:
                return "No observer letters yet."
            return f"# Observer letter · {letter['date']}\n\n{letter['body']}"
        if name == "mydaemon_neighbors":
            return json.dumps(
                self._core.neighbors(args["note_path"], depth=int(args.get("depth", 1))),
                default=str,
            )
        if name == "mydaemon_endorse":
            return json.dumps(
                self._core.endorse(int(args["feedback_event_id"]), int(args["rank"])),
                default=str,
            )
        if name == "mydaemon_remember":
            return json.dumps(
                self._core.remember(
                    args["text"],
                    title=args.get("title"),
                    tags=args.get("tags") or [],
                    source="hermes-tool",
                    confirmed=bool(args.get("confirmed", False)),
                    session_id=self._session_id,
                ),
                default=str,
            )
        return f"Unknown tool: {name}"

    # -- Hermes setup integration -----------------------------------------

    def get_config_schema(self) -> dict:
        """Fields Hermes's `memory setup` prompts for. Both optional — leaving
        them blank falls back to MY_DAEMON_CONFIG / the project-local config.yaml."""
        return {
            "fields": [
                {
                    "name": "config_path",
                    "type": "string",
                    "required": False,
                    "label": "Path to my-daemon config.yaml (optional)",
                },
                {
                    "name": "vault_path",
                    "type": "string",
                    "required": False,
                    "label": "Obsidian vault path (optional override)",
                },
            ]
        }

    def save_config(self, config: dict) -> None:
        """Persist the setup answers under $HERMES_HOME/my-daemon.json and apply them."""
        cfg_path = config.get("config_path")
        if cfg_path:
            self._config_path = Path(cfg_path).expanduser()
            self._settings = None  # force reload from the chosen file
        if self._hermes_home is not None:
            with contextlib.suppress(Exception):
                target = self._hermes_home / "my-daemon.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(config, indent=2), encoding="utf-8")
