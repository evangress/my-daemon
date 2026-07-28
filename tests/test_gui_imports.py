# SPDX-License-Identifier: Apache-2.0
"""The chat surface must not drag in Tkinter.

Found by CI on 2026-07-28: `my_daemon/gui/__init__.py` eagerly imported both
surfaces, so `from my_daemon.gui import launch_chat` — the first line of
`daemon chat` — pulled in the Tkinter setup window too. Any machine without
`python3-tk` (a bare server, a slim container, several distros' default Python)
therefore could not start the NiceGUI chat, which needs no Tkinter at all.

The CI symptom was a collection error; the bug was a user on a headless box
being unable to run the app.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _forget_gui_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [m for m in list(sys.modules) if m.startswith("my_daemon.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_the_chat_surface_imports_without_tkinter(monkeypatch: pytest.MonkeyPatch):
    """A headless Linux install has no python3-tk, and must still run `daemon chat`."""
    _forget_gui_modules(monkeypatch)
    # A None entry in sys.modules makes `import tkinter` raise ImportError,
    # which is exactly what a machine without python3-tk does.
    monkeypatch.setitem(sys.modules, "tkinter", None)

    gui = importlib.import_module("my_daemon.gui")

    assert callable(gui.launch_chat)


def test_the_setup_window_still_reports_a_missing_tkinter(monkeypatch: pytest.MonkeyPatch):
    """Laziness must not turn "Tkinter is missing" into a confusing AttributeError."""
    _forget_gui_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "tkinter", None)
    gui = importlib.import_module("my_daemon.gui")

    with pytest.raises(ImportError):
        _ = gui.launch_setup


def test_an_unknown_attribute_is_still_an_attribute_error(monkeypatch: pytest.MonkeyPatch):
    _forget_gui_modules(monkeypatch)
    gui = importlib.import_module("my_daemon.gui")

    with pytest.raises(AttributeError):
        _ = gui.no_such_surface
