# SPDX-License-Identifier: Apache-2.0
"""Recover an activation ledger from the feedback log.

Every query the daemon has ever answered already recorded its ranked
candidates in ``feedback.retrieval_summary``. That is a proto-activation
record, and replaying it is free signal — the ledger starts with history
instead of starting empty.

The honest limitation, reported rather than hidden: a query whose notes have
since been renamed or deleted yields a *partial* fingerprint, which makes it
look less similar to everything than it should.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from my_daemon.models import is_seed_distance
from my_daemon.stores.activations import Activation, ActivationLedger
from my_daemon.stores.db import open_state_db
from my_daemon.stores.registry import NoteRegistry

# Above this share of dropped activations the backfill is arguably more noise
# than signal, and the user should be told rather than quietly given it.
_DROP_RATE_WARNING = 0.20


@dataclass
class BackfillReport:
    feedback_rows_scanned: int = 0
    queries_written: int = 0
    queries_skipped: int = 0
    activations_written: int = 0
    activations_dropped: int = 0
    dry_run: bool = True

    @property
    def drop_rate(self) -> float:
        total = self.activations_written + self.activations_dropped
        return self.activations_dropped / total if total else 0.0

    @property
    def warning(self) -> str:
        if self.drop_rate <= _DROP_RATE_WARNING:
            return ""
        return (
            f"{self.drop_rate:.0%} of historical activations named notes that no "
            "longer resolve — those queries carry partial fingerprints and will "
            "look less similar to everything than they should."
        )


def _backfill_uid(feedback_id: int) -> str:
    """Deterministic, so re-running cannot duplicate a row."""
    return f"backfill:{feedback_id}"


def backfill_activations(
    db_path: Path,
    registry: NoteRegistry,
    *,
    dry_run: bool = True,
    since: datetime | None = None,
    limit: int = 20_000,
) -> BackfillReport:
    """Replay `feedback.retrieval_summary` rows into the activation ledger."""

    report = BackfillReport(dry_run=dry_run)
    ledger = ActivationLedger(db_path=db_path)

    sql = "SELECT id, timestamp, query, retrieval_summary FROM feedback WHERE 1=1"
    params: list = []
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    conn = open_state_db(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
        existing = {
            r["query_uid"]
            for r in conn.execute("SELECT query_uid FROM queries WHERE origin = 'backfill'")
        }
    finally:
        conn.close()

    for row in rows:
        report.feedback_rows_scanned += 1
        query_uid = _backfill_uid(int(row["id"]))
        if query_uid in existing:
            report.queries_skipped += 1
            continue

        try:
            summary = json.loads(row["retrieval_summary"] or "{}")
        except (ValueError, TypeError):
            report.queries_skipped += 1
            continue

        ranked = summary.get("ranked") or []
        if not ranked:
            report.queries_skipped += 1
            continue

        # Historical rows name notes by path. Resolve in one round trip.
        paths = {r.get("note_path") for r in ranked if r.get("note_path")}
        by_path = {p: registry.by_path(p) for p in paths}

        activations: list[Activation] = []
        seen: set[tuple[str, str]] = set()
        for rank, candidate in enumerate(ranked, start=1):
            record = by_path.get(candidate.get("note_path"))
            if record is None:
                report.activations_dropped += 1
                continue
            # Historical rows may carry an int hop count, a float weighted
            # distance, or nothing at all — one predicate covers all three.
            distance = candidate.get("graph_distance")
            source = "vector_seed" if is_seed_distance(distance) else "graph_expansion"
            if (record.uuid, source) in seen:
                continue
            seen.add((record.uuid, source))
            seed = by_path.get(candidate.get("seed_note_path"))
            activations.append(
                Activation(
                    note_uuid=record.uuid,
                    source=source,  # type: ignore[arg-type]
                    rank=rank,
                    raw_score=candidate.get("score"),
                    chunk_id=candidate.get("chunk_id"),
                    graph_distance=distance,
                    seed_note_uuid=seed.uuid if seed else None,
                )
            )

        if not activations:
            report.queries_skipped += 1
            continue

        report.queries_written += 1
        report.activations_written += len(activations)
        if dry_run:
            continue

        ledger.record(
            query_uid=query_uid,
            text=row["query"],
            # Pre-ledger rows predate surface tracking; do not pretend otherwise.
            surface="unknown",
            ts=_parse_ts(row["timestamp"]),
            activations=activations,
            seed_count=int(summary.get("seed_count") or 0),
            expanded_count=int(summary.get("expanded_count") or 0),
            origin="backfill",
        )
        ledger.link_feedback(query_uid, int(row["id"]))

    return report


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
