# SPDX-License-Identifier: Apache-2.0
"""Hermes plugin shim for My Daemon's memory provider.

Copy or symlink this directory into your Hermes tree at
``plugins/memory/my-daemon/`` and ``pip install my-daemon`` into Hermes's venv.
The real implementation lives in ``my_daemon.hermes`` (this repo, Apache-2.0);
this shim only registers it with Hermes.

Activate with ``memory: { provider: my-daemon }`` in ``~/.hermes/config.yaml``.
"""

from __future__ import annotations

from typing import Any

from my_daemon.hermes import MyDaemonProvider


def register(ctx: Any) -> None:
    """Hermes plugin entry point: register the memory provider."""
    ctx.register_memory_provider(MyDaemonProvider())
