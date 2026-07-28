# SPDX-License-Identifier: Apache-2.0
"""The activation ledger — the daemon's memory of its own retrievals.

Every query records which notes fired, via which source, at what strength. That
per-query pattern is a **fingerprint**: a sparse vector over note-UUID space.
Two questions worded completely differently that light up the same notes are the
same underlying concern, and cosine over fingerprints finds that where embedding
the query text would not.

Similarity runs as an inverted-index scan in SQLite rather than a second Qdrant
collection. At single-user scale (100k queries is ~14 years at 20/day) the
difference is below the perceptual threshold, the offline clustering phase wants
the whole matrix in memory anyway, and — decisively — recording an activation
must never fail because Qdrant is down. The ledger is how the system remembers
itself. ``note_ordinals`` and ``queries.l2_norm`` are populated regardless so a
vector backend stays a drop-in.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid as uuidlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from my_daemon.stores.db import last_insert_id, migrate, open_state_db

_MIN_SCHEMA_VERSION = 4

Source = Literal["vector_seed", "graph_expansion", "keyword", "recall_injected", "selected"]

# How much each kind of evidence is worth. A user-confirmed pick outweighs a
# search hit; an ambient recall injection counts for least.
STRENGTH_BY_SOURCE: dict[str, float] = {
    "vector_seed": 1.0,
    "graph_expansion": 0.6,
    "keyword": 0.5,
    "selected": 1.5,
    "recall_injected": 0.3,
}

# Hermes has two retrieval paths and they are *not* the same act. The
# `mydaemon_recall` tool is the agent deliberately looking something up on the
# user's behalf; `prefetch` fires on every conversational turn whether or not
# anyone wanted memory consulted. They get different surfaces so the ambient one
# can be filtered everywhere the intentional one is trusted.
HERMES_RECALL_SURFACE = "hermes_recall"
HERMES_PREFETCH_SURFACE = "hermes_prefetch"

# Surfaces that represent a question the user actually asked. Hermes `prefetch`
# fires on *every* conversational turn, so including it would let ambient
# lookups dominate the fingerprint space — themes would become
# themes-of-Hermes-turns.
INTENTIONAL_SURFACES: tuple[str, ...] = ("cli", "gui", HERMES_RECALL_SURFACE, "mcp")

# Retrievals nobody asked for. Recorded (they are still real activations, and
# the graph still learns from them) but never treated as evidence of what the
# user is thinking about.
AMBIENT_SURFACES: tuple[str, ...] = (HERMES_PREFETCH_SURFACE,)

# Below this many queries a document-frequency *ratio* means nothing — with two
# queries logged, a note in one of them scores 0.5 and every sane threshold
# would prune it. Hub-pruning is an optimization for a corpus big enough to have
# hubs; engaging it early would return nothing for the first weeks of use,
# exactly when the user is deciding whether to trust the feature.
_MIN_CORPUS_FOR_DF_PRUNING = 50


@dataclass(frozen=True)
class Activation:
    note_uuid: str
    source: Source
    rank: int | None = None
    raw_score: float | None = None
    chunk_id: str | None = None
    graph_distance: float | None = None
    seed_note_uuid: str | None = None
    strength: float | None = None  # computed if omitted


@dataclass(frozen=True)
class ActivationRow:
    note_uuid: str
    source: str
    strength: float
    rank: int | None
    raw_score: float | None
    graph_distance: float | None


@dataclass(frozen=True)
class FingerprintHit:
    query_id: int
    query_uid: str
    text: str
    ts: datetime
    score: float
    shared_notes: list[str]


def strength_for(source: str, rank: int | None) -> float:
    """Rank-based, deliberately — not score-based.

    Raw retrieval scores are incomparable across the dense→hybrid-RRF
    transition, across embedding-model changes, and across any historical
    backfill. Rank survives all three. ``raw_score`` is stored anyway so a
    future re-derivation stays possible.
    """

    positional = 1.0 / math.log2((rank or 1) + 1)
    return STRENGTH_BY_SOURCE.get(source, 0.5) * positional


class ActivationLedger:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            migrate(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return open_state_db(
            self.db_path, read_only=self.read_only, min_version=_MIN_SCHEMA_VERSION
        )

    # ---- writes -----------------------------------------------------------

    def record(
        self,
        *,
        query_uid: str | None = None,
        text: str,
        surface: str,
        ts: datetime | None = None,
        activations: Sequence[Activation],
        session_id: str | None = None,
        seed_count: int = 0,
        expanded_count: int = 0,
        latency_ms: int | None = None,
        origin: str = "live",
    ) -> int:
        """Insert the query and its activations in one transaction."""

        query_uid = query_uid or str(uuidlib.uuid4())
        ts = ts or datetime.now(UTC)
        rows = [
            (
                a.note_uuid,
                a.source,
                a.strength if a.strength is not None else strength_for(a.source, a.rank),
                a.raw_score,
                a.rank,
                a.chunk_id,
                a.graph_distance,
                a.seed_note_uuid,
            )
            for a in activations
        ]

        # Pre-IDF magnitude. The IDF-weighted, normalized vector is derived on
        # read, because df moves as the ledger grows.
        per_note: dict[str, float] = {}
        for note_uuid, _src, strength, *_rest in rows:
            per_note[note_uuid] = per_note.get(note_uuid, 0.0) + strength
        l2 = math.sqrt(sum(v * v for v in per_note.values()))

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO queries (
                    query_uid, ts, text, text_sha256, surface, session_id, origin,
                    seed_count, expanded_count, activation_count, l2_norm, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_uid,
                    ts.isoformat(),
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    surface,
                    session_id,
                    origin,
                    seed_count,
                    expanded_count,
                    len(rows),
                    l2,
                    latency_ms,
                ),
            )
            query_id = last_insert_id(cur)
            conn.executemany(
                "INSERT OR REPLACE INTO query_activations "
                "(query_id, note_uuid, source, strength, raw_score, rank, chunk_id, "
                " graph_distance, seed_note_uuid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(query_id, *r) for r in rows],
            )
            for note_uuid, strength in per_note.items():
                conn.execute(
                    """
                    INSERT INTO note_activation_stats
                        (note_uuid, query_count, last_activated_at, total_strength)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(note_uuid) DO UPDATE SET
                        query_count = query_count + 1,
                        last_activated_at = excluded.last_activated_at,
                        total_strength = total_strength + excluded.total_strength
                    """,
                    (note_uuid, ts.isoformat(), strength),
                )
        return query_id

    def link_feedback(self, query_uid: str, feedback_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE queries SET feedback_id = ? WHERE query_uid = ?",
                (feedback_id, query_uid),
            )

    # ---- reads ------------------------------------------------------------

    def get(self, query_uid: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM queries WHERE query_uid = ?", (query_uid,)).fetchone()
        return dict(row) if row else None

    def activations_for(self, query_id: int) -> list[ActivationRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT note_uuid, source, strength, rank, raw_score, graph_distance "
                "FROM query_activations WHERE query_id = ? ORDER BY strength DESC",
                (query_id,),
            ).fetchall()
        return [
            ActivationRow(
                note_uuid=r["note_uuid"],
                source=r["source"],
                strength=r["strength"],
                rank=r["rank"],
                raw_score=r["raw_score"],
                graph_distance=r["graph_distance"],
            )
            for r in rows
        ]

    def note_df(self, note_uuids: Iterable[str]) -> dict[str, int]:
        """How many queries each note has ever fired for."""
        wanted = list(dict.fromkeys(note_uuids))
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT note_uuid, query_count FROM note_activation_stats "
                f"WHERE note_uuid IN ({placeholders})",
                wanted,
            ).fetchall()
        found = {r["note_uuid"]: int(r["query_count"]) for r in rows}
        return {u: found.get(u, 0) for u in wanted}

    def total_queries(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0])

    def fingerprint(self, query_id: int) -> dict[str, float]:
        """IDF-weighted, L2-normalized sparse vector over note-UUID space."""

        rows = self.activations_for(query_id)
        if not rows:
            return {}
        raw: dict[str, float] = {}
        for r in rows:
            raw[r.note_uuid] = raw.get(r.note_uuid, 0.0) + r.strength
        return self._weight_and_normalize(raw)

    def idf(self, note_uuids: Iterable[str], *, total: int | None = None) -> dict[str, float]:
        """Smoothed inverse document frequency, ``log(1 + N / (1 + df))``.

        Public because *both* sides of a fingerprint comparison have to be
        weighted by it. Weighting only the probe and dividing by an unweighted
        magnitude is not a cosine — it inflates whichever query is built from
        rarer notes — and that was the bug this method exists to make hard to
        reintroduce.
        """

        wanted = list(dict.fromkeys(note_uuids))
        if not wanted:
            return {}
        n = max(total if total is not None else self.total_queries(), 1)
        df = self.note_df(wanted)
        return {u: math.log(1 + n / (1 + df.get(u, 0))) for u in wanted}

    def _weight_and_normalize(self, raw: Mapping[str, float]) -> dict[str, float]:
        idf = self.idf(raw.keys())
        weighted = {u: v * idf.get(u, 0.0) for u, v in raw.items()}
        magnitude = math.sqrt(sum(v * v for v in weighted.values()))
        if magnitude == 0:
            return {}
        return {u: v / magnitude for u, v in weighted.items()}

    def similar(
        self,
        probe: Mapping[str, float],
        *,
        top_k: int = 5,
        since: datetime | None = None,
        min_score: float = 0.15,
        max_df_ratio: float = 0.25,
        exclude_query_id: int | None = None,
        surfaces: Sequence[str] | None = None,
    ) -> list[FingerprintHit]:
        """Inverted-index cosine over the ledger.

        Cost is driven by skew, not size: rows scanned is the sum of df over the
        probe's notes. Terms above ``max_df_ratio`` are dropped first — a note
        present in a quarter of all queries carries no information and only
        costs scan.

        Two passes, and the second one is load-bearing. The first finds *which*
        queries share a note with the probe. The second reads those queries'
        **full** activation sets, because a candidate's magnitude cannot be
        computed from the shared notes alone — and it is a genuine cosine only
        when the candidate is IDF-weighted and normalized in the same space as
        the probe. The pre-IDF ``queries.l2_norm`` is still written by
        :meth:`record` (a vector backend would want it) but is deliberately not
        used here: mixing it with an IDF-weighted numerator is exactly the
        asymmetry this method used to have.
        """

        if not probe:
            return []
        total = max(self.total_queries(), 1)
        if total < _MIN_CORPUS_FOR_DF_PRUNING:
            terms = dict(probe)
        else:
            df = self.note_df(probe.keys())
            terms = {u: w for u, w in probe.items() if df.get(u, 0) / total <= max_df_ratio}
        if not terms:
            return []

        placeholders = ",".join("?" * len(terms))
        sql = [
            "SELECT DISTINCT qa.query_id, q.query_uid, q.text, q.ts",
            "FROM query_activations qa JOIN queries q ON q.id = qa.query_id",
            f"WHERE qa.note_uuid IN ({placeholders})",
        ]
        params: list = list(terms.keys())
        if since is not None:
            sql.append("AND q.ts >= ?")
            params.append(since.isoformat())
        if exclude_query_id is not None:
            sql.append("AND qa.query_id != ?")
            params.append(exclude_query_id)
        if surfaces:
            sql.append(f"AND q.surface IN ({','.join('?' * len(surfaces))})")
            params.extend(surfaces)

        with self._connect() as conn:
            candidates = {
                int(r["query_id"]): (r["query_uid"], r["text"], r["ts"])
                for r in conn.execute(" ".join(sql), params).fetchall()
            }
            if not candidates:
                return []
            ids = ",".join("?" * len(candidates))
            rows = conn.execute(
                f"SELECT query_id, note_uuid, strength FROM query_activations "
                f"WHERE query_id IN ({ids})",
                list(candidates),
            ).fetchall()

        # Sum per (query, note) before weighting: one note reached by two routes
        # is one component of the vector, not two. `fingerprint` sums the same
        # way, and the two representations have to agree or the cosine is taken
        # against a vector that never existed.
        summed: dict[int, dict[str, float]] = {}
        for r in rows:
            per_note = summed.setdefault(int(r["query_id"]), {})
            per_note[r["note_uuid"]] = per_note.get(r["note_uuid"], 0.0) + float(r["strength"])

        idf = self.idf({u for per_note in summed.values() for u in per_note}, total=total)

        hits = []
        for query_id, per_note in summed.items():
            weighted = {u: v * idf.get(u, 0.0) for u, v in per_note.items()}
            magnitude = math.sqrt(sum(v * v for v in weighted.values()))
            if not magnitude:
                continue
            # The probe arrives already IDF-weighted and unit-normalized, so
            # dividing the candidate by its own magnitude completes the cosine.
            shared = [
                (u, terms[u] * w / magnitude) for u, w in weighted.items() if u in terms and w
            ]
            score = sum(contribution for _, contribution in shared)
            if score < min_score:
                continue
            uid, text, ts = candidates[query_id]
            hits.append(
                FingerprintHit(
                    query_id=query_id,
                    query_uid=uid,
                    text=text,
                    ts=datetime.fromisoformat(ts),
                    score=score,
                    shared_notes=[u for u, _ in sorted(shared, key=lambda x: -x[1])],
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def query_fingerprints(
        self,
        *,
        limit: int = 4000,
        since: datetime | None = None,
        until: datetime | None = None,
        surfaces: Sequence[str] | None = None,
    ) -> list[tuple[int, str, dict[str, float]]]:
        """(query_id, text, fingerprint) for the offline clustering phase.

        Weighting and normalization happen here in one pass so the caller gets
        directly comparable vectors.

        ``until`` bounds the window on the *right*. Consolidation never needs
        it — "now" is always the right edge — but replaying history to tune a
        parameter does, and a post-hoc filter is not a substitute: clusters
        computed over the whole corpus and then discarded for containing later
        queries are not the clusters the earlier run would have found.
        """

        sql = ["SELECT id, text FROM queries WHERE 1=1"]
        params: list = []
        if since is not None:
            sql.append("AND ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            sql.append("AND ts <= ?")
            params.append(until.isoformat())
        if surfaces:
            sql.append(f"AND surface IN ({','.join('?' * len(surfaces))})")
            params.extend(surfaces)
        sql.append("ORDER BY id DESC LIMIT ?")
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()

        out = []
        for row in rows:
            fingerprint = self.fingerprint(int(row["id"]))
            if fingerprint:
                out.append((int(row["id"]), row["text"], fingerprint))
        return out

    def hot_notes(self, *, limit: int = 20, since: datetime | None = None):
        """Which notes your attention actually lands on."""
        if since is None:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT note_uuid, query_count, total_strength "
                    "FROM note_activation_stats ORDER BY query_count DESC, "
                    "total_strength DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                (r["note_uuid"], int(r["query_count"]), float(r["total_strength"])) for r in rows
            ]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qa.note_uuid, COUNT(DISTINCT qa.query_id) AS c, "
                "       SUM(qa.strength) AS s "
                "FROM query_activations qa JOIN queries q ON q.id = qa.query_id "
                "WHERE q.ts >= ? GROUP BY qa.note_uuid "
                "ORDER BY c DESC, s DESC LIMIT ?",
                (since.isoformat(), limit),
            ).fetchall()
        return [(r["note_uuid"], int(r["c"]), float(r["s"])) for r in rows]

    def recent(self, *, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queries ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def compact(self, *, older_than: datetime) -> int:
        """Collapse old detail rows into a stored fingerprint.

        Compaction, not deletion: the fingerprint stays comparable forever at
        reduced fidelity, and only the per-source forensics are lost. Matters
        because Hermes prefetch can generate 10-50x the intentional query rate.
        """

        with self._connect() as conn:
            ids = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM queries WHERE ts < ? AND fingerprint_json IS NULL",
                    (older_than.isoformat(),),
                ).fetchall()
            ]
        for query_id in ids:
            fingerprint = self.fingerprint(query_id)
            top = dict(sorted(fingerprint.items(), key=lambda x: -x[1])[:20])
            with self._connect() as conn:
                conn.execute(
                    "UPDATE queries SET fingerprint_json = ? WHERE id = ?",
                    (json.dumps(top), query_id),
                )
                conn.execute("DELETE FROM query_activations WHERE query_id = ?", (query_id,))
        return len(ids)
