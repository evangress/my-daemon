# SPDX-License-Identifier: Apache-2.0
"""Integration core — the single adapter every front end onto My Daemon shares.

Hermes (``my_daemon.hermes``) and the optional MCP server are thin faces over
:class:`DaemonCore`. See PLAN-HERMES.md §3.
"""

from my_daemon.integration.core import DaemonCore, RecallBlock, build_core

__all__ = ["DaemonCore", "RecallBlock", "build_core"]
