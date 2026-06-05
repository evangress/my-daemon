# SPDX-License-Identifier: Apache-2.0
"""Hermes memory provider (PLAN-HERMES H1–H3): availability, prefetch caching,
dream injection, tool routing, non-blocking + gated write-back — all with a
stub core so no Qdrant/Anthropic is touched."""

from __future__ import annotations

import json
import time
from pathlib import Path

from my_daemon.config import HermesConfig, Settings
from my_daemon.hermes.provider import MyDaemonProvider
from my_daemon.integration.core import RecallBlock

# ---------------------------------------------------------------------------
# stub core — records calls, returns canned values
# ---------------------------------------------------------------------------


class StubCore:
    def __init__(self) -> None:
        self.dream: dict | None = None
        self.remembered: list[dict] = []
        self.endorsed: list[tuple[int, int]] = []
        self.recalled: list[str] = []

        class _VS:
            def ensure_collection(self) -> None:
                pass

        self.vector_store = _VS()

    def recall_block(self, query, *, budget_chars, top_k=None) -> RecallBlock:
        return RecallBlock(
            text=f"BLOCK for {query}",
            feedback_event_id=42,
            candidates=[
                {"rank": 1, "note_path": "Pullman Daemons.md"},
                {"rank": 2, "note_path": "Socratic Daemon.md"},
            ],
        )

    def recall(self, query, *, top_k=None, synthesize=False) -> dict:
        self.recalled.append(query)
        return {"feedback_event_id": 7, "candidates": []}

    def latest_dream(self) -> dict | None:
        return self.dream

    def read_dream(self, day) -> dict | None:
        return self.dream

    def neighbors(self, note_path, *, depth=1) -> dict:
        return {"note_path": note_path, "neighbors": []}

    def endorse(self, feedback_event_id, rank) -> dict:
        self.endorsed.append((feedback_event_id, rank))
        return {"ok": True}

    def remember(self, text, *, title=None, tags=None, source="hermes", confirmed=False, session_id=None) -> dict:
        self.remembered.append({"text": text, "source": source, "session_id": session_id})
        return {"ok": True}


def _settings(vault: Path, *, write_back: bool = False, enabled: bool = True) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.hermes = HermesConfig(
        provider_enabled=enabled,
        allow_write_back=write_back,
        capture_min_chars=10,
    )
    return s


def _provider(vault: Path, **kw) -> tuple[MyDaemonProvider, StubCore]:
    core = StubCore()
    provider = MyDaemonProvider(settings=_settings(vault, **kw), core=core)
    return provider, core


# ---------------------------------------------------------------------------
# is_available — network-free kill-switch
# ---------------------------------------------------------------------------


def test_is_available_respects_enabled_and_vault(tmp_path: Path, monkeypatch) -> None:
    # Guard: is_available must never build the (heavy, networked) core.
    import my_daemon.hermes.provider as prov_mod

    monkeypatch.setattr(
        prov_mod, "build_core", lambda *a, **k: (_ for _ in ()).throw(AssertionError("built core")),
    )

    provider = MyDaemonProvider(settings=_settings(tmp_path, enabled=True))
    assert provider.is_available() is True

    off = MyDaemonProvider(settings=_settings(tmp_path, enabled=False))
    assert off.is_available() is False

    missing = MyDaemonProvider(settings=_settings(tmp_path / "nope", enabled=True))
    assert missing.is_available() is False


# ---------------------------------------------------------------------------
# prefetch caches the feedback id + candidates for later reinforcement
# ---------------------------------------------------------------------------


def test_prefetch_returns_block_and_caches(tmp_path: Path) -> None:
    provider, _core = _provider(tmp_path)
    block = provider.prefetch("what is a daemon", session_id="s1")
    assert block == "BLOCK for what is a daemon"
    cached = provider._last_prefetch["s1"]
    assert cached["feedback_event_id"] == 42
    assert cached["candidates"][0]["note_path"] == "Pullman Daemons.md"


# ---------------------------------------------------------------------------
# system_prompt_block — identity + dream injection, graceful when empty
# ---------------------------------------------------------------------------


def test_system_prompt_block_injects_latest_letter(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path)
    core.dream = {"date": "2026-06-04", "body": "This week a philosophy cluster formed."}
    block = provider.system_prompt_block()
    assert "My Daemon" in block  # identity framing
    assert "2026-06-04" in block
    assert "philosophy cluster" in block


def test_system_prompt_block_degrades_without_letters(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path)
    core.dream = None
    block = provider.system_prompt_block()
    assert "hasn't dreamed" in block


# ---------------------------------------------------------------------------
# tools — read always, writes only when write-back is on
# ---------------------------------------------------------------------------


def test_tool_schemas_gate_on_write_back(tmp_path: Path) -> None:
    read_only, _ = _provider(tmp_path, write_back=False)
    names = {t["name"] for t in read_only.get_tool_schemas()}
    assert {"mydaemon_recall", "mydaemon_dream", "mydaemon_neighbors"} == names

    writable, _ = _provider(tmp_path, write_back=True)
    wnames = {t["name"] for t in writable.get_tool_schemas()}
    assert "mydaemon_endorse" in wnames and "mydaemon_remember" in wnames


def test_handle_tool_call_routes_to_core(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path)
    out = provider.handle_tool_call("mydaemon_recall", {"query": "daemon"})
    assert core.recalled == ["daemon"]
    assert json.loads(out)["feedback_event_id"] == 7

    core.dream = {"date": "2026-06-04", "body": "letter body"}
    letter = provider.handle_tool_call("mydaemon_dream", {})
    assert "letter body" in letter

    assert provider.handle_tool_call("nope", {}).startswith("Unknown tool")


# ---------------------------------------------------------------------------
# sync_turn — non-blocking, gated, captures + soft-reinforces
# ---------------------------------------------------------------------------


def test_sync_turn_noop_when_write_back_disabled(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path, write_back=False)
    provider.sync_turn("a long enough user message", "a long enough reply")
    provider.on_session_end()
    assert core.remembered == []


def test_sync_turn_captures_and_reinforces(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path, write_back=True)
    # Prefetch first so there's a cached candidate to reinforce.
    provider.prefetch("what is a daemon", session_id="s1")
    provider.sync_turn(
        "Tell me about the soul metaphor",
        "According to Pullman Daemons, the soul takes external animal form.",
        session_id="s1",
    )
    provider.on_session_end()  # drains the capture thread

    # Salient turn captured...
    assert len(core.remembered) == 1
    assert core.remembered[0]["session_id"] == "s1"
    # ...and the prefetched note the reply leaned on got endorsed.
    assert core.endorsed == [(42, 1)]


def test_sync_turn_skips_thin_turns(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path, write_back=True)
    provider.sync_turn("hi", "yo")  # below capture_min_chars
    provider.on_session_end()
    assert core.remembered == []


def test_sync_turn_returns_immediately(tmp_path: Path) -> None:
    provider, core = _provider(tmp_path, write_back=True)

    slow = core.remember

    def _slow(*args, **kwargs):
        time.sleep(0.3)
        return slow(*args, **kwargs)

    core.remember = _slow  # type: ignore[method-assign]

    t0 = time.perf_counter()
    provider.sync_turn("a sufficiently long user message", "a sufficiently long assistant reply")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, f"sync_turn blocked for {elapsed:.3f}s"

    provider.on_session_end()  # now the background capture finishes
    assert len(core.remembered) == 1
