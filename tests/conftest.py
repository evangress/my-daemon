# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import keyring
import keyring.backend
import keyring.backends.fail
import pytest

from my_daemon.config import LLMConfig
from my_daemon.llm.client import LLMClient
from my_daemon.models import Chunk, RetrievedChunk
from my_daemon.secrets import ENV_VAR, resolve_api_key


def _has_tkinter() -> bool:
    try:
        importlib.import_module("tkinter")
    except Exception:  # ModuleNotFoundError, or a broken/headless Tk install
        return False
    return True


#: Skip anything that reaches into `my_daemon.gui.setup`, which imports Tkinter
#: at module level. Tkinter is an *optional* toolkit: CI's 3.12 leg runs the
#: runner's system Python, which ships no `python3-tk`, and bare servers and
#: slim containers routinely lack it too. A suite that goes red because an
#: optional GUI library is missing is testing the machine, not the code.
requires_tkinter = pytest.mark.skipif(
    not _has_tkinter(), reason="needs Tkinter (install python3-tk)"
)


@pytest.fixture
def vault_root() -> Path:
    return (Path(__file__).parent / "fixtures" / "sample_vault").resolve()


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A real keyring backend that happens to live in a dict."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        # Real backends raise PasswordDeleteError for an absent entry rather
        # than KeyError; the fake has to match or it tests a fiction.
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(username) from None


@pytest.fixture(autouse=True)
def isolated_keychain():
    """Never touch the developer's real credential store.

    Autouse and suite-wide on purpose. Without it, a developer who has run
    `daemon setup` for real has an ANTHROPIC_API_KEY in their Secret Service /
    Keychain, and every "no key configured" assertion in the suite would
    silently pass for the wrong reason — or worse, pass on their machine and
    fail in CI.
    """
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def memory_keyring(isolated_keychain) -> MemoryKeyring:
    """The in-memory backend installed by :func:`isolated_keychain`, to seed."""
    backend = keyring.get_keyring()
    assert isinstance(backend, MemoryKeyring)
    return backend


@pytest.fixture
def no_keyring():
    """A machine with no usable credential store — headless Linux, cron."""
    previous = keyring.get_keyring()
    keyring.set_keyring(keyring.backends.fail.Keyring())
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def live_answer() -> Callable[[str, list[tuple[str, datetime | None, str]]], str]:
    """Ask the real model a question, through the project's real LLM wrapper.

    Only for `-m live_llm` fixtures (see `test_live_prompts.py`) — those check
    prompt *compliance*, which cannot be exercised against a fake client.

    Key resolution goes through `my_daemon.secrets.resolve_api_key` (env → OS
    credential store → legacy `.env`), the same precedence `load_settings`
    uses, rather than a direct `os.environ` check — but note the suite-wide
    `isolated_keychain` autouse fixture swaps in an empty in-memory keyring for
    every test, this one included, so in practice only the environment
    variable or a `.env` file can supply the key here. Skips with a clear
    message when neither does — one that does NOT send a reader down the
    `daemon setup` / `daemon key set` path, since that writes to the OS
    credential store this fixture cannot see.
    """
    resolved = resolve_api_key(
        env_value=os.environ.get(ENV_VAR),
        dotenv_paths=(Path.cwd() / ".env",),
    )
    if resolved.value is None:
        pytest.skip(
            f"No {ENV_VAR} resolved from the environment. These live tests can only "
            "read the key from an environment variable or .env — the suite's autouse "
            "isolated_keychain fixture blanks the OS credential store for every test, "
            "so a key stored via `daemon key set` is deliberately invisible here. Run "
            "with:\n"
            f"  {ENV_VAR}=$(...) .venv/bin/pytest tests/test_live_prompts.py -m live_llm"
        )

    llm = LLMClient(LLMConfig(), api_key=resolved.value)

    def _ask(query: str, excerpts: list[tuple[str, datetime | None, str]]) -> str:
        chunks = [
            RetrievedChunk(
                chunk=Chunk(
                    id=f"live-{i}",
                    note_path=note_path,
                    heading_path=[],
                    text=text,
                    chunk_index=0,
                    occurred_at=occurred_at,
                    occurred_at_source="frontmatter" if occurred_at is not None else None,
                )
            )
            for i, (note_path, occurred_at, text) in enumerate(excerpts)
        ]
        return llm.synthesize(query, chunks)

    return _ask
