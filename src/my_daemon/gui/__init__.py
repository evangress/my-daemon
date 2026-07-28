# SPDX-License-Identifier: Apache-2.0
"""GUI surfaces for My Daemon: NiceGUI chat + Tkinter setup.

Both are exposed lazily, via :pep:`562` module ``__getattr__``. Importing them
eagerly meant that ``from my_daemon.gui import launch_chat`` — the first line of
``daemon chat`` — also imported the Tkinter setup window, so a machine without
``python3-tk`` could not start the NiceGUI chat even though that surface needs
no Tkinter at all. Bare servers, slim containers, and several distributions'
default Python all ship without it.

Laziness here is a compatibility requirement, not a startup-time optimisation:
the two surfaces have genuinely different dependencies and have to be able to
fail independently of one another.
"""

from typing import Any

__all__ = ["launch_chat", "launch_setup"]


def __getattr__(name: str) -> Any:
    # An ImportError from a missing Tkinter propagates as-is: "no module named
    # tkinter" is the actionable message, and folding it into an AttributeError
    # would send the reader hunting for a typo instead.
    if name == "launch_chat":
        from my_daemon.gui.app import launch_chat

        return launch_chat
    if name == "launch_setup":
        from my_daemon.gui.setup import launch_setup

        return launch_setup
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
