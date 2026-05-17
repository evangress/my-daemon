# SPDX-License-Identifier: Apache-2.0
"""Platform-appropriate runtime paths.

Kept dependency-free so the lightweight Tkinter setup window can import it
without pulling in NiceGUI / sentence-transformers at startup time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


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
