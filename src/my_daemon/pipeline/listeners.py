# SPDX-License-Identifier: Apache-2.0
"""The listener set a production retrieval attaches.

One function, because the last time this decision lived in two places one of
them lost a listener and nobody noticed for two days of shipping.

:func:`~my_daemon.integration.wiring.build_orchestrator` had carried both
recorders since §IV.7 shipped, and `tests/test_wiring.py` proved it. But
nothing under ``src/`` ever called that factory: every real query went through
:class:`~my_daemon.pipeline.query.QueryEngine`, which hand-built its own
orchestrator with the activation recorder alone. So ``daemon policy`` read an
empty table regardless of how many queries had run, and the interleaving
measurement §IV.7 exists to make possible had never actually been taken.

The fix is not "remember to add it in both places" — it is that there is no
second place to add it to. Both construction paths call this.

It lives in ``pipeline/`` rather than in ``wiring.py`` for an import reason
worth stating: ``integration/__init__`` imports ``core``, which imports
``pipeline.query``, so ``query.py`` importing ``integration.wiring`` would
close a cycle. ``wiring.py`` already depends on ``pipeline`` — this keeps the
arrow pointing the way it already points.
"""

from __future__ import annotations

from my_daemon.pipeline.activation import ActivationRecorder
from my_daemon.pipeline.policy import PolicyRecorder
from my_daemon.retrieval.trace import RetrievalListener
from my_daemon.stores.activations import ActivationLedger
from my_daemon.stores.policy import RetrievalPolicyStore


def build_listeners(
    ledger: ActivationLedger,
    policy: RetrievalPolicyStore,
    *,
    record: bool = True,
) -> list[RetrievalListener]:
    """Both recorders, or neither.

    ``record=False`` exists for debug probes like ``daemon search``, which are
    not questions the user asked: they must pollute neither the fingerprint
    space nor the policy win rates. The two recorders stay separate objects —
    they answer different questions and should fail independently — but they
    are switched together, because a probe that counts as an impression is
    exactly as dishonest as one that counts as an activation.
    """

    if not record:
        return []
    return [ActivationRecorder(ledger), PolicyRecorder(policy)]
