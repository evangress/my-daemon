# SPDX-License-Identifier: Apache-2.0
"""Placeholder for the full end-to-end test from §9 of the scaffold.

This test requires:
  - A running Qdrant (``docker-compose up -d qdrant``).
  - The sentence-transformers model downloaded.

It's marked xfail-by-default so a fresh ``pytest`` run still passes without
those dependencies. Remove the skip when wiring up the integration CI.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MY_DAEMON_E2E") != "1",
    reason="Set MY_DAEMON_E2E=1 to run the end-to-end ingest+query test (requires Qdrant).",
)


def test_e2e_placeholder() -> None:
    # The real implementation will: spin up an ephemeral Qdrant collection, ingest
    # the fixture vault, run a query like "what is a daemon", and assert the
    # ranked sources include Pullman Daemons.md and Socratic Daemon.md.
    raise NotImplementedError("E2E test is scaffolded but not yet implemented.")
