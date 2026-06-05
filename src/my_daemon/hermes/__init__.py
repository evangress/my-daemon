# SPDX-License-Identifier: Apache-2.0
"""Hermes integration — the memory-provider plugin (PLAN-HERMES.md, Path A).

``MyDaemonProvider`` subclasses Hermes's ``MemoryProvider`` ABC when Hermes is
importable, else a plain ``object`` so this package imports and unit-tests
without Hermes installed. The thin shim that registers it inside a Hermes tree
lives at ``plugins/memory/my-daemon/`` in this repo.
"""

from my_daemon.hermes.provider import HERMES_AVAILABLE, MyDaemonProvider

__all__ = ["MyDaemonProvider", "HERMES_AVAILABLE"]
