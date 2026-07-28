# SPDX-License-Identifier: Apache-2.0
"""The listener that counts which ranking got shown."""

from __future__ import annotations

from collections import Counter

from my_daemon.retrieval.trace import RetrievalTrace
from my_daemon.stores.policy import RetrievalPolicyStore


class PolicyRecorder:
    """Counts one impression per drafted candidate, per policy.

    A second listener rather than an extension of
    :class:`~my_daemon.pipeline.activation.ActivationRecorder`, because the two
    answer different questions and fail independently: losing the policy count
    should not cost the activation row that fingerprint recall and themes are
    built on.
    """

    def __init__(self, store: RetrievalPolicyStore) -> None:
        self.store = store

    def on_retrieval(self, trace: RetrievalTrace) -> str | None:
        # No team means the pool was score-ordered, so no comparison happened.
        # Counting it would treat an unfair ordering as if it were a draft.
        counts = Counter(rc.team for rc in trace.result.ranked if rc.team)
        if counts:
            self.store.record_impressions(counts)
        # The query_uid belongs to the activation recorder alone.
        return None
