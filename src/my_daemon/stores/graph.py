# SPDX-License-Identifier: Apache-2.0
"""NetworkX MultiDiGraph wrapper with pickle persistence.

The graph holds the only state the daemon cannot re-derive from the vault:
every edge weight M1 has learned. Persistence is therefore deliberately
paranoid about two failure modes that ordinary daily use actually hits.

*Interrupted writes.* ``pickle.dump`` straight into the live file means a
Ctrl-C, a crash, or a full disk leaves a truncated pickle behind, and every
later command dies on ``UnpicklingError``. Saves go to a temp file in the same
directory and land via ``os.replace`` — the same discipline ``vault.writer``
already applies to the user's markdown. A reader sees either the whole
previous graph or the whole new one, never half of either. When the file *is*
corrupt anyway (a disk fault, an older build's half-write), :meth:`GraphStore.load`
says so by name instead of raising a raw pickle traceback.

*Concurrent processes.* The realistic configuration is a GUI open all day —
which saves the graph on every endorse click — alongside a nightly cron agent
that loads and saves the same file. A lock file next to the graph serialises
the writers: :meth:`GraphStore.save` takes it, and :meth:`GraphStore.transaction`
holds it across a whole load→mutate→save cycle, which is what a read-modify-write
such as the reinforcement path needs to avoid a lost update.

Reads are deliberately *not* locked. Atomic replace already makes them safe,
and the no-contention path (one ``os.open`` and one ``os.unlink``) stays cheap
because of it. This guards a single-user tool against its own concurrent
processes; it is not a transaction manager.
"""

from __future__ import annotations

import contextlib
import json
import os
import pickle
import socket
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from collections.abc import Iterator
from pathlib import Path

import networkx as nx

from my_daemon.models import GraphStats, Note
from my_daemon.vault.identity import effective_uuid

#: How long a writer waits for the lock before giving up. Generous enough for
#: an endorse click to survive a passing agent save, short enough that a human
#: is not left staring at a hung command.
DEFAULT_LOCK_TIMEOUT = 30.0

#: When we cannot prove the holder is dead (another host, or an OS that will
#: not tell us), treat a lock older than this as abandoned.
DEFAULT_LOCK_STALE_AFTER = 300.0

_LOCK_POLL_INTERVAL = 0.05


class GraphCorruptError(RuntimeError):
    """The graph file exists but could not be read back as a graph."""


class GraphLockTimeout(TimeoutError):
    """Another process held the graph lock for longer than we were willing to wait."""


def _pid_alive(pid: int) -> bool | None:
    """``True``/``False`` when we can tell, ``None`` when we cannot.

    ``None`` matters: on an unknown answer the caller must fall back to the age
    heuristic rather than assume either way — assuming "dead" would break a
    live process's lock, assuming "alive" would wedge the daemon forever.
    """

    if not isinstance(pid, int) or pid <= 0:
        return None

    if os.name == "nt":
        # `os.kill(pid, 0)` on Windows does not probe — CPython routes it to
        # TerminateProcess. Ask the API directly instead.
        try:
            import ctypes

            # `windll` only exists in typeshed under win32; `unused-ignore`
            # keeps the gate honest when mypy *is* run for that platform.
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined, unused-ignore]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False if kernel32.GetLastError() == 87 else None  # 87 = no such pid
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 — an unknown answer is a valid answer here
            return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return None
    return True


def _read_lock_info(lock_path: Path) -> dict | None:
    """The lock's contents; ``None`` if it is gone, ``{}`` if it says nothing useful."""

    try:
        raw = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        # Present but unreadable. Still a held lock — let the age check decide,
        # rather than reporting it as released and spinning on it.
        return {}
    try:
        info = json.loads(raw)
    except ValueError:
        # A lock whose holder died between creating and writing it. Unparseable
        # is still a held lock; the age check below decides its fate.
        return {}
    return info if isinstance(info, dict) else {}


def _lock_age(lock_path: Path, info: dict) -> float:
    acquired = info.get("acquired_at")
    if isinstance(acquired, int | float):
        return max(0.0, time.time() - float(acquired))
    try:
        return max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return 0.0


def _describe_holder(info: dict) -> str:
    pid, host = info.get("pid"), info.get("host")
    if pid is None:
        return "another process"
    return f"pid {pid} on {host}" if host else f"pid {pid}"


def _break_if_stale(lock_path: Path, *, stale_after: float) -> bool:
    """Remove an abandoned lock. Returns ``True`` if the lock is now gone.

    Two independent grounds for abandonment, in order of confidence:
    the holder's pid is provably dead on this host, or — when liveness is
    unknowable (another host, an OS that will not say) — the lock is older
    than ``stale_after``. A *live* holder is never broken on age alone; the
    caller's timeout will fire instead, with a message naming who is holding it.
    """

    info = _read_lock_info(lock_path)
    if info is None:
        return True  # released while we were looking

    alive: bool | None = None
    if info.get("host") == socket.gethostname():
        alive = _pid_alive(info.get("pid"))  # type: ignore[arg-type]

    if alive is True:
        return False
    if alive is None and _lock_age(lock_path, info) <= stale_after:
        return False

    # Re-read before unlinking: if the holder released and someone else took
    # the lock in the meantime, it is no longer ours to break.
    if _read_lock_info(lock_path) != info:
        return False
    with contextlib.suppress(OSError):
        lock_path.unlink()
    return True


def _tag_node(tag: str) -> str:
    return f"tag::{tag}"


def _dangling_node(target: str) -> str:
    """A wikilink target with no note behind it. Its own namespace, because it
    has no identity to key on and must never collide with a real note."""

    return f"dangling::{target}"


def _note_node(note_uuid: str) -> str:
    """Note nodes are keyed by *identity*, so a rename or a folder move keeps
    the node — and every learned edge weight on it — exactly where it was."""

    return f"note::{note_uuid}"


class GraphStore:
    """Notes, tags, and the wikilink/tag edges between them.

    Nodes carry a ``type`` attribute (``"note"`` or ``"tag"``) so callers can filter
    cleanly. Chunk ids belonging to each note are tracked on the node so retrieval
    expansion can pull all chunks of a neighbor without a separate index.
    """

    def __init__(
        self,
        path: Path,
        *,
        read_only: bool = False,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        lock_stale_after: float = DEFAULT_LOCK_STALE_AFTER,
    ) -> None:
        self.path = path
        self.read_only = read_only
        self.lock_timeout = lock_timeout
        self.lock_stale_after = lock_stale_after
        self.graph: nx.MultiDiGraph = nx.MultiDiGraph()
        # Guards this instance's lock bookkeeping, and makes `lock()` behave
        # for threads inside one process the way the lock file does for
        # processes. Reentrant so `save()` nests inside `transaction()`.
        self._lock_guard = threading.RLock()
        self._lock_depth = 0
        self._lock_token: str | None = None

    @property
    def lock_path(self) -> Path:
        """The lock file, alongside the graph so it shares its lifetime and permissions."""

        return self.path.with_name(self.path.name + ".lock")

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the inter-process write lock for the duration of the block.

        Reentrant per instance, so ``save()`` nests inside a caller's own
        ``lock()`` without deadlocking. Raises :class:`GraphLockTimeout` — with
        the holder named — rather than waiting forever, and breaks a lock whose
        holder is provably dead so a crash cannot wedge the daemon.
        """

        if not self._lock_guard.acquire(timeout=self.lock_timeout):
            raise GraphLockTimeout(
                f"timed out after {self.lock_timeout:g}s waiting for another thread "
                f"to finish writing {self.path}"
            )
        try:
            if self._lock_depth == 0:
                self._lock_token = self._acquire_lock_file()
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._release_lock_file()
        finally:
            self._lock_guard.release()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[GraphStore]:
        """Lock, reload from disk, hand back the store, then save.

        The seam for a read-modify-write. Taking the lock *before* loading is
        the whole point: without it two processes can both load, both mutate,
        and the second save silently discards the first's edits. The body is
        not saved if it raises.
        """

        with self.lock():
            self.load()
            yield self
            self.save()

    def _acquire_lock_file(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "token": token,
                "acquired_at": time.time(),
            }
        )
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                return token

            # Deadline first, unconditionally: the loop must terminate even if
            # some pathology (an unreadable lock file, an unlink we are not
            # permitted to make) keeps reporting the lock as breakable.
            if time.monotonic() >= deadline:
                holder = _describe_holder(_read_lock_info(self.lock_path) or {})
                raise GraphLockTimeout(
                    f"timed out after {self.lock_timeout:g}s waiting for {self.lock_path} "
                    f"— held by {holder}. If that process is gone, delete the lock file."
                )
            if not _break_if_stale(self.lock_path, stale_after=self.lock_stale_after):
                time.sleep(_LOCK_POLL_INTERVAL)

    def _release_lock_file(self) -> None:
        token, self._lock_token = self._lock_token, None
        info = _read_lock_info(self.lock_path)
        if info is None or info.get("token") != token:
            return  # broken as stale and re-taken by someone else — not ours to remove
        with contextlib.suppress(OSError):
            self.lock_path.unlink()

    def load(self) -> None:
        """Read the graph from disk. Unlocked — ``save`` replaces atomically.

        Raises :class:`GraphCorruptError` for a file that exists but will not
        unpickle, so the caller can print a recovery instruction instead of a
        traceback. The graph is rebuildable from the vault; only the learned
        edge weights are lost, which is a rebuild, not a reinstall.
        """

        if not self.path.is_file():
            self.graph = nx.MultiDiGraph()
            return
        # The pickle is written by this process's own `save()` into the user's
        # local data dir — not a transport format and never read from a
        # third party, so `pickle.load` is not an untrusted-input hazard here.
        with self.path.open("rb") as fh:
            try:
                loaded = pickle.load(fh)
            except Exception as exc:  # noqa: BLE001 — anything here means "not a graph"
                raise GraphCorruptError(
                    f"graph file is corrupt — run `daemon ingest --full` to rebuild "
                    f"({self.path}: {exc!r})"
                ) from exc
        # Specifically a MultiDiGraph, not merely "some graph": the rest of this
        # class calls multigraph-only API (`out_edges(keys=True)`, keyed
        # `remove_edge`). A plain Graph would sail past a looser check and then
        # blow up somewhere far from the file that caused it.
        if not isinstance(loaded, nx.MultiDiGraph):
            raise GraphCorruptError(
                f"graph file is corrupt — run `daemon ingest --full` to rebuild "
                f"({self.path}: unpickled a {type(loaded).__name__}, expected a MultiDiGraph)"
            )
        self.graph = loaded

    def save(self) -> None:
        """Persist the graph atomically, under the inter-process write lock."""

        if self.read_only:
            raise RuntimeError("GraphStore is read-only (opened from a snapshot bundle)")
        with self.lock():
            self._write_atomic()

    def _write_atomic(self) -> None:
        """Pickle into a sibling temp file, fsync, then ``os.replace`` onto the target.

        Same directory so the replace stays on one filesystem and is therefore
        actually atomic; ``fsync`` before the rename because this file is the
        daemon's memory and a power cut should cost at most the last save.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".graph-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                pickle.dump(self.graph, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def add_note(self, note: Note, chunk_ids: list[str]) -> None:
        node = _note_node(effective_uuid(note))
        self.graph.add_node(
            node,
            type="note",
            title=note.title,
            # Carried so reports can render prose without a registry lookup.
            rel_path=note.relative_path,
            mtime=note.mtime.isoformat(),
            chunk_ids=chunk_ids,
        )
        # If this node was previously a wikilink placeholder, promote it now.
        self.graph.nodes[node].pop("dangling", None)

        for target_uuid in note.wikilink_uuids:
            target_node = _note_node(target_uuid)
            if target_node not in self.graph:
                self.graph.add_node(target_node, type="note", title=target_uuid)
            self.graph.add_edge(node, target_node, kind="wikilink", weight=1.0)

        for target in note.dangling_wikilinks:
            # Keeps the graph complete: "notes you keep meaning to write".
            target_node = _dangling_node(target)
            if target_node not in self.graph:
                self.graph.add_node(target_node, type="note", title=target, dangling=True)
            self.graph.add_edge(node, target_node, kind="wikilink", weight=1.0)

        for tag in note.tags:
            tnode = _tag_node(tag)
            if tnode not in self.graph:
                self.graph.add_node(tnode, type="tag", title=tag)
            self.graph.add_edge(node, tnode, kind="tag", weight=1.0)

    def update_note(self, note: Note, chunk_ids: list[str]) -> None:
        """Re-ingest a note *differentially*, preserving what it doesn't own.

        A note owns its outgoing wikilink and tag edges and nothing else. This
        refreshes the node's attributes, adds the edges the author introduced,
        removes the ones they deleted, and — crucially — leaves surviving edges
        completely untouched, so the ``weight`` and ``last_reinforced_at`` that
        ``retrieval.weights`` accumulated on them carry across the edit.

        Prefer this over ``remove_note`` + ``add_note``: ``remove_node`` drops
        all incident edges in *both* directions, which deletes every other
        note's links *to* this one, and ``add_note`` recreates edges at
        ``weight=1.0``, discarding everything M1 has learned.

        A link the author deletes and later restores correctly starts over at
        1.0 — it genuinely left the set.
        """

        node = _note_node(effective_uuid(note))
        if node not in self.graph:
            self.add_note(note, chunk_ids)
            return

        self.graph.add_node(
            node,
            type="note",
            title=note.title,
            rel_path=note.relative_path,
            mtime=note.mtime.isoformat(),
            chunk_ids=chunk_ids,
        )
        self.graph.nodes[node].pop("dangling", None)

        desired: dict[tuple[str, str], str] = {
            (_note_node(u), "wikilink"): u for u in note.wikilink_uuids
        }
        desired.update({(_dangling_node(t), "wikilink"): t for t in note.dangling_wikilinks})
        desired.update({(_tag_node(tag), "tag"): tag for tag in note.tags})

        existing: dict[tuple[str, str], list] = {}
        for _src, dst, key, edata in list(self.graph.out_edges(node, keys=True, data=True)):
            kind = edata.get("kind")
            if kind not in ("wikilink", "tag"):
                continue  # not ours to manage — leave any other edge kind alone
            existing.setdefault((dst, kind), []).append(key)

        for slot, keys in existing.items():
            if slot not in desired:
                for key in keys:
                    self.graph.remove_edge(node, slot[0], key)

        for slot, label in desired.items():
            if slot in existing:
                continue  # survives the diff — its learned weight stays as-is
            dst, kind = slot
            if dst not in self.graph:
                if kind != "wikilink":
                    self.graph.add_node(dst, type="tag", title=label)
                elif dst.startswith("dangling::"):
                    self.graph.add_node(dst, type="note", title=label, dangling=True)
                else:
                    self.graph.add_node(dst, type="note", title=label)
            self.graph.add_edge(node, dst, kind=kind, weight=1.0)

    def remove_note(self, note_uuid: str) -> None:
        node = _note_node(note_uuid)
        if node in self.graph:
            self.graph.remove_node(node)

    def chunk_ids_for(self, note_uuid: str) -> list[str]:
        node = _note_node(note_uuid)
        if node not in self.graph:
            return []
        return list(self.graph.nodes[node].get("chunk_ids", []))

    def neighbors_within(
        self,
        note_uuid: str,
        depth: int,
        *,
        weighted: bool = False,
        exclude_tag_prefixes: tuple[str, ...] = (),
    ) -> dict[str, float]:
        """Distances from a note to reachable neighbor notes, up to ``depth`` hops.

        Walks both wikilink (note→note) and tag (note→tag→note) edges. With
        ``weighted=False`` distance is integer hops in the undirected projection.
        With ``weighted=True`` it's a Dijkstra distance over ``1/edge.weight`` —
        so reinforced edges feel shorter and neighbors behind them rank higher
        after the decay multiplier in ``retrieval.expand``. The hop budget
        (``depth``) is still applied as an integer-hop cap so reinforcement
        can't pull a chunk in from arbitrarily far away.
        """

        start = _note_node(note_uuid)
        if start not in self.graph:
            return {}

        ug = self.graph.to_undirected(as_view=True)
        if exclude_tag_prefixes:
            # Walking a daemon-authored theme tag would let the system's own
            # conclusions steer the retrieval that produced them.
            blocked = {
                n
                for n, d in self.graph.nodes(data=True)
                if d.get("type") == "tag"
                and str(d.get("title", "")).startswith(exclude_tag_prefixes)
            }
            if blocked:
                ug = ug.subgraph([n for n in ug.nodes if n not in blocked])

        # Hop budget first — bounds the reachable set independent of weight.
        hop_seen = {start: 0}
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            d = hop_seen[current]
            if d >= depth:
                continue
            for nb in ug.neighbors(current):
                if nb in hop_seen:
                    continue
                hop_seen[nb] = d + 1
                queue.append(nb)

        if weighted:
            # On a MultiGraph, Dijkstra hands the weight callback a dict of
            # parallel-edge data: ``{edge_key: {"weight": ..., ...}, ...}``.
            # We take the strongest (highest weight) of the parallel edges, so
            # a reinforced edge cancels out any weaker parallel siblings.
            def _edge_cost(_u: str, _v: str, edata: dict) -> float:
                weights = [
                    float(d.get("weight", 1.0)) for d in edata.values() if isinstance(d, dict)
                ] or [float(edata.get("weight", 1.0))]
                return 1.0 / max(max(weights), 0.1)

            try:
                weighted_dist = nx.single_source_dijkstra_path_length(
                    ug.subgraph(hop_seen.keys()),
                    start,
                    weight=_edge_cost,
                )
            except nx.NodeNotFound:
                weighted_dist = {start: 0.0}
            distances: dict[str, float] = {
                n: float(weighted_dist.get(n, hop_seen[n])) for n in hop_seen
            }
        else:
            distances = {n: float(d) for n, d in hop_seen.items()}

        notes_only: dict[str, float] = {}
        for node, dist in distances.items():
            if node == start:
                continue
            if self.graph.nodes[node].get("type") != "note":
                continue
            if self.graph.nodes[node].get("dangling"):
                continue
            rel = node.removeprefix("note::")
            notes_only[rel] = dist
        return notes_only

    def shortest_note_path(self, uuid_from: str, uuid_to: str) -> list[str] | None:
        """Return the shortest hop path between two notes as a list of node ids.

        Used by ``retrieval.weights.apply_selection`` to walk the edges it
        should reinforce. Returns ``None`` if either endpoint is missing or
        no path exists.
        """

        a, b = _note_node(uuid_from), _note_node(uuid_to)
        if a not in self.graph or b not in self.graph:
            return None
        ug = self.graph.to_undirected(as_view=True)
        try:
            return nx.shortest_path(ug, a, b)
        except nx.NetworkXNoPath:
            return None

    def stats(self) -> GraphStats:
        notes = [
            n
            for n, d in self.graph.nodes(data=True)
            if d.get("type") == "note" and not d.get("dangling")
        ]
        tag_nodes = [n for n, d in self.graph.nodes(data=True) if d.get("type") == "tag"]
        try:
            pr = nx.pagerank(self.graph) if self.graph.number_of_nodes() else {}
        except nx.PowerIterationFailedConvergence:
            pr = {}
        top_pr = sorted(
            (
                (n.removeprefix("note::"), s)
                for n, s in pr.items()
                if self.graph.nodes[n].get("type") == "note"
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        tag_counts = Counter(t.removeprefix("tag::") for t in tag_nodes)
        # rank tags by note-degree
        tag_degree = sorted(
            (
                (t.removeprefix("tag::"), self.graph.in_degree(t) + self.graph.out_degree(t))
                for t in tag_nodes
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        _ = tag_counts  # reserved for future use; currently degree-based
        return GraphStats(
            note_count=len(notes),
            tag_count=len(tag_nodes),
            edge_count=self.graph.number_of_edges(),
            top_pagerank=top_pr,
            top_tags=tag_degree,
        )
