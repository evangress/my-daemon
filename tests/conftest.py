# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures."""

from __future__ import annotations

import importlib
from pathlib import Path

import keyring
import keyring.backend
import keyring.backends.fail
import pytest


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
