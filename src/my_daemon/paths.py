# SPDX-License-Identifier: Apache-2.0
"""Platform-appropriate runtime paths, and where `config.yaml` is looked for.

Kept dependency-free — no pydantic, no yaml — so the lightweight Tkinter setup
window and `my_daemon.config` can both import it without pulling in NiceGUI /
sentence-transformers at startup time. That is also why the user-config-dir
rules are hand-rolled here rather than taken from `platformdirs`: this module
is the project's one place for "where does this live on this OS", and adding a
dependency to answer a five-line question would work against that.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_FILENAME = "config.yaml"


def user_config_dir() -> Path:
    r"""The per-user config directory for the daemon.

    Windows: %APPDATA%\my-daemon
    Linux/macOS: $XDG_CONFIG_HOME/my-daemon (or ~/.config/my-daemon)

    macOS gets the XDG location rather than ~/Library/Application Support on
    purpose: `config.yaml` is a file a person is expected to open and edit by
    hand, and ~/.config is where a CLI user will look for it.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "my-daemon"
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "my-daemon"


def user_config_path() -> Path:
    """The per-user ``config.yaml``, whether or not it exists."""
    return user_config_dir() / CONFIG_FILENAME


def config_search_paths(explicit: Path | str | None = None) -> list[Path]:
    """Every location that would be consulted for a config, in order.

    1. ``explicit`` — the ``--config PATH`` global CLI option, or the
       ``config_path`` argument to :func:`my_daemon.config.load_settings`.
    2. ``$MY_DAEMON_CONFIG``.
    3. ``./config.yaml`` — the repo-dev workflow, unchanged.
    4. :func:`user_config_path` — the installed-daemon workflow.

    An explicit choice (1 or 2) is *exclusive*: if you name a file, a typo in
    the name must fail rather than quietly resolve to some other config.
    Returned for the error message as much as for the lookup, so a user who
    sees the failure also sees where to put the file.
    """
    if explicit is not None:
        return [Path(explicit).expanduser()]
    from_env = os.environ.get("MY_DAEMON_CONFIG")
    if from_env:
        return [Path(from_env).expanduser()]
    return [Path.cwd() / CONFIG_FILENAME, user_config_path()]


def find_config(explicit: Path | str | None = None) -> Path | None:
    """First existing config in :func:`config_search_paths`, or None."""
    for candidate in config_search_paths(explicit):
        if candidate.is_file():
            return candidate
    return None


def log_path() -> Path:
    r"""Where the chat window's rotating log file lives.

    Windows: %LOCALAPPDATA%\my-daemon\daemon.log
    macOS:   ~/Library/Logs/my-daemon/daemon.log
    Linux:   $XDG_STATE_HOME/my-daemon/daemon.log (or ~/.local/state/...)
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "my-daemon" / "daemon.log"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "my-daemon" / "daemon.log"
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "my-daemon" / "daemon.log"
