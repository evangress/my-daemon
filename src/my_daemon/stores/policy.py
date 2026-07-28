# SPDX-License-Identifier: Apache-2.0
"""Which retrieval policy earns the user's picks.

The activation ledger records *what fired*. This records *which ranking won*,
which is a different question and the one the adaptive loop was missing.

Before team-draft interleaving, a pick carried almost no usable information:
the pool was score-ordered, so seeds occupied every top slot, and a seed pick
hit ``apply_selection``'s "seed is the selection" early return and reinforced
nothing. The pick was real evidence that the user was satisfied — and the
system threw it away.

With the two rankings drafted alternately (see
:mod:`my_daemon.retrieval.interleave`), position is symmetric between them by
construction, so the pick's *team* is an unbiased comparison of the two
policies without any propensity model — the design point of interleaved
evaluation (Radlinski, Kurup & Joachims, CIKM 2008).

This store deliberately only counts. It does not feed back into ranking. The
honest sequence is: measure first, on real usage, and decide what to do about
an imbalance once there is one to look at. Auto-tuning the draft ratio from its
own win rate would be a closed loop with nothing outside it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from my_daemon.stores.db import migrate, open_state_db

_MIN_SCHEMA_VERSION = 6


@dataclass(frozen=True)
class PolicyStat:
    policy: str
    impressions: int
    wins: int
    last_win_at: datetime | None = None

    @property
    def win_rate(self) -> float:
        """Wins per impression, or 0.0 when the policy has never been shown.

        Zero rather than an exception or a NaN: this number is rendered on a
        fresh vault, where every policy has zero impressions, and a crash there
        would be a worse answer than "no evidence yet".
        """

        return self.wins / self.impressions if self.impressions else 0.0


class RetrievalPolicyStore:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            migrate(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return open_state_db(
            self.db_path, read_only=self.read_only, min_version=_MIN_SCHEMA_VERSION
        )

    def record_impressions(self, counts: Mapping[str, int]) -> None:
        """Note that each policy contributed ``n`` candidates to one pool.

        Zero counts are skipped: a retrieval that surfaced no expansion never
        put the expansion policy in front of the user, and counting it as an
        impression would dilute a win rate with queries where the policy never
        had a chance.
        """

        rows = [(policy, int(n)) for policy, n in counts.items() if n > 0]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO retrieval_policy_stats (policy, impressions) VALUES (?, ?) "
                "ON CONFLICT(policy) DO UPDATE SET impressions = impressions + excluded.impressions",
                rows,
            )

    def record_win(self, policy: str, *, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(UTC)).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO retrieval_policy_stats (policy, wins, last_win_at) VALUES (?, 1, ?) "
                "ON CONFLICT(policy) DO UPDATE SET wins = wins + 1, last_win_at = excluded.last_win_at",
                (policy, stamp),
            )

    def get(self, policy: str) -> PolicyStat:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM retrieval_policy_stats WHERE policy = ?", (policy,)
            ).fetchone()
        return _to_stat(row) if row else PolicyStat(policy=policy, impressions=0, wins=0)

    def stats(self) -> list[PolicyStat]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM retrieval_policy_stats ORDER BY policy").fetchall()
        return [_to_stat(r) for r in rows]


def _to_stat(row: sqlite3.Row) -> PolicyStat:
    stamp = row["last_win_at"]
    return PolicyStat(
        policy=row["policy"],
        impressions=int(row["impressions"]),
        wins=int(row["wins"]),
        last_win_at=datetime.fromisoformat(stamp) if stamp else None,
    )
