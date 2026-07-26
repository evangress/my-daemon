# SPDX-License-Identifier: Apache-2.0
"""Durability of the two files the daemon cannot re-derive cheaply.

``graph.gpickle`` holds every learned edge weight; ``manifest.json`` holds the
incremental-ingest bookkeeping. Both used to be written by dumping straight
into the live file, so a Ctrl-C mid-save left a truncated file that every
later command choked on — and nothing anywhere serialised two daemon
processes writing the same graph.

The contract these tests pin down:
  * a save that dies part-way leaves the *previous* file byte-identical;
  * a corrupt graph raises a named, actionable error, not a raw traceback;
  * a corrupt manifest is a warning and a full rescan, because it is
    derivable state;
  * writers take a lock file, wait for each other, time out with a message
    that names the holder, and never deadlock behind a dead process.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import my_daemon.stores.graph as graph_mod
import my_daemon.vault.writer as writer_mod
from my_daemon.config import Settings
from my_daemon.models import Note
from my_daemon.pipeline.ingest import _load_manifest, _save_manifest, ingest_vault
from my_daemon.stores.graph import GraphCorruptError, GraphLockTimeout, GraphStore
from my_daemon.vault.identity import derive_path_uuid

_UUIDS = {
    "A.md": "aaaaaaaa-0000-4000-8000-000000000001",
    "B.md": "bbbbbbbb-0000-4000-8000-000000000002",
}


def _note(rel_path: str) -> Note:
    return Note(
        path=Path("/vault") / rel_path,
        relative_path=rel_path,
        title=rel_path.removesuffix(".md"),
        body="body",
        wikilink_uuids=[],
        dangling_wikilinks=[],
        tags=[],
        mtime=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        word_count=1,
        uuid=_UUIDS[rel_path],
    )


def _saved_store(tmp_path: Path, *rel_paths: str) -> GraphStore:
    store = GraphStore(path=tmp_path / "graph.gpickle")
    for rel in rel_paths:
        store.add_note(_note(rel), chunk_ids=[f"{rel}::0"])
    store.save()
    return store


# ---------------------------------------------------------------------------
# Corrupt graph → a named, actionable error
# ---------------------------------------------------------------------------


def test_truncated_pickle_raises_graph_corrupt_error(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    _saved_store(tmp_path, "A.md")
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])

    with pytest.raises(GraphCorruptError) as excinfo:
        GraphStore(path=path).load()

    message = str(excinfo.value)
    assert "daemon ingest --full" in message
    assert str(path) in message


def test_garbage_bytes_raise_graph_corrupt_error(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    path.write_bytes(b"this is not a pickle at all")

    with pytest.raises(GraphCorruptError):
        GraphStore(path=path).load()


def test_empty_file_raises_graph_corrupt_error(tmp_path: Path):
    """Zero bytes is what an interrupted `open("wb")` used to leave behind."""
    path = tmp_path / "graph.gpickle"
    path.write_bytes(b"")

    with pytest.raises(GraphCorruptError):
        GraphStore(path=path).load()


def test_a_valid_pickle_of_the_wrong_type_is_corrupt(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    path.write_bytes(pickle.dumps({"not": "a graph"}))

    with pytest.raises(GraphCorruptError):
        GraphStore(path=path).load()


def test_a_plain_graph_pickle_is_corrupt(tmp_path: Path):
    """A graph, but not a *multi-di*graph.

    Everything downstream calls multigraph-only API (`out_edges(keys=True)`,
    keyed `remove_edge`). Accepting a plain `Graph` here would defer the
    failure to somewhere with no mention of the file that caused it.
    """
    import networkx as nx

    path = tmp_path / "graph.gpickle"
    path.write_bytes(pickle.dumps(nx.Graph()))

    with pytest.raises(GraphCorruptError) as excinfo:
        GraphStore(path=path).load()

    assert "daemon ingest --full" in str(excinfo.value)


def test_missing_graph_file_is_still_an_empty_graph(tmp_path: Path):
    store = GraphStore(path=tmp_path / "graph.gpickle")
    store.load()
    assert store.graph.number_of_nodes() == 0


def test_load_roundtrips_a_saved_graph(tmp_path: Path):
    _saved_store(tmp_path, "A.md", "B.md")
    reloaded = GraphStore(path=tmp_path / "graph.gpickle")
    reloaded.load()
    assert reloaded.stats().note_count == 2


# ---------------------------------------------------------------------------
# Atomic graph writes
# ---------------------------------------------------------------------------


def test_a_save_that_dies_mid_write_leaves_the_previous_graph_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "graph.gpickle"
    store = _saved_store(tmp_path, "A.md")
    good_bytes = path.read_bytes()

    def exploding_dump(obj, fh, *args, **kwargs):  # noqa: ANN001, ANN202
        fh.write(b"\x80\x05half a pickle")
        raise OSError("disk full")

    monkeypatch.setattr(graph_mod.pickle, "dump", exploding_dump)
    store.add_note(_note("B.md"), chunk_ids=["b0"])
    with pytest.raises(OSError, match="disk full"):
        store.save()

    assert path.read_bytes() == good_bytes
    # The half-written temp file and the lock file are both cleaned up.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["graph.gpickle"]

    monkeypatch.undo()
    reloaded = GraphStore(path=path)
    reloaded.load()
    assert reloaded.stats().note_count == 1


def test_save_leaves_no_lock_file_behind(tmp_path: Path):
    _saved_store(tmp_path, "A.md")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["graph.gpickle"]


def test_save_creates_missing_parent_directories(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "graph.gpickle"
    store = GraphStore(path=path)
    store.add_note(_note("A.md"), chunk_ids=["a0"])
    store.save()
    assert path.is_file()


def test_read_only_store_still_refuses_to_save(tmp_path: Path):
    store = GraphStore(path=tmp_path / "graph.gpickle", read_only=True)
    with pytest.raises(RuntimeError, match="read-only"):
        store.save()
    # And it did not so much as take the lock on its way out.
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Inter-process lock
# ---------------------------------------------------------------------------


def test_a_second_writer_times_out_while_the_first_holds_the_lock(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    holder = GraphStore(path=path)
    waiter = GraphStore(path=path, lock_timeout=0.2)

    with holder.lock(), pytest.raises(GraphLockTimeout) as excinfo:
        waiter.save()

    message = str(excinfo.value)
    assert "graph.gpickle.lock" in message
    assert f"pid {os.getpid()}" in message


def test_the_lock_is_released_when_the_holder_finishes(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    holder = GraphStore(path=path)
    waiter = GraphStore(path=path, lock_timeout=0.2)

    with holder.lock():
        pass
    waiter.add_note(_note("A.md"), chunk_ids=["a0"])
    waiter.save()

    assert not (tmp_path / "graph.gpickle.lock").exists()
    assert path.is_file()


def test_the_lock_is_released_even_when_the_body_raises(tmp_path: Path):
    store = GraphStore(path=tmp_path / "graph.gpickle")
    with pytest.raises(ValueError, match="boom"), store.lock():
        raise ValueError("boom")
    assert not (tmp_path / "graph.gpickle.lock").exists()


def test_saving_inside_an_open_lock_does_not_deadlock(tmp_path: Path):
    """The reinforcement seam is lock → mutate → save; save must be reentrant."""
    store = GraphStore(path=tmp_path / "graph.gpickle", lock_timeout=1.0)
    with store.lock():
        store.add_note(_note("A.md"), chunk_ids=["a0"])
        store.save()
        store.save()
    assert not (tmp_path / "graph.gpickle.lock").exists()
    assert (tmp_path / "graph.gpickle").is_file()


def test_a_lock_left_by_a_dead_process_is_broken(tmp_path: Path):
    """A crash mid-transaction must not wedge the daemon forever."""
    path = tmp_path / "graph.gpickle"
    lock_path = tmp_path / "graph.gpickle.lock"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    assert dead.wait(timeout=30) == 0  # reaped, so the pid is genuinely gone
    lock_path.write_text(
        json.dumps(
            {
                "pid": dead.pid,
                "host": socket.gethostname(),
                "token": "stale-token",
                "acquired_at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    store = GraphStore(path=path, lock_timeout=2.0)
    store.add_note(_note("A.md"), chunk_ids=["a0"])
    store.save()

    assert path.is_file()
    assert not lock_path.exists()


def test_a_lock_from_another_host_expires_by_age(tmp_path: Path):
    """We cannot test liveness of a pid on another machine — age is the fallback."""
    path = tmp_path / "graph.gpickle"
    lock_path = tmp_path / "graph.gpickle.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 1,
                "host": "some-other-machine",
                "token": "stale-token",
                "acquired_at": time.time() - 10_000,
            }
        ),
        encoding="utf-8",
    )

    store = GraphStore(path=path, lock_timeout=2.0, lock_stale_after=60.0)
    store.save()

    assert path.is_file()


def test_a_fresh_lock_from_another_host_is_respected(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    lock_path = tmp_path / "graph.gpickle.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 1,
                "host": "some-other-machine",
                "token": "live-token",
                "acquired_at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    store = GraphStore(path=path, lock_timeout=0.2, lock_stale_after=600.0)
    with pytest.raises(GraphLockTimeout):
        store.save()

    assert lock_path.read_text(encoding="utf-8").count("live-token") == 1


def test_transaction_reloads_mutates_and_persists(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    _saved_store(tmp_path, "A.md")

    other = GraphStore(path=path)
    with other.transaction():
        other.add_note(_note("B.md"), chunk_ids=["b0"])

    reread = GraphStore(path=path)
    reread.load()
    assert reread.stats().note_count == 2


def test_transaction_holds_the_lock_for_its_whole_body(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    holder = GraphStore(path=path)
    waiter = GraphStore(path=path, lock_timeout=0.2)

    with holder.transaction(), pytest.raises(GraphLockTimeout):
        waiter.save()

    assert not (tmp_path / "graph.gpickle.lock").exists()


def test_transaction_does_not_save_when_the_body_raises(tmp_path: Path):
    path = tmp_path / "graph.gpickle"
    _saved_store(tmp_path, "A.md")
    good_bytes = path.read_bytes()

    other = GraphStore(path=path)
    with pytest.raises(ValueError, match="nope"), other.transaction():
        other.add_note(_note("B.md"), chunk_ids=["b0"])
        raise ValueError("nope")

    assert path.read_bytes() == good_bytes
    assert not (tmp_path / "graph.gpickle.lock").exists()


# ---------------------------------------------------------------------------
# Manifest: corrupt is a warning, not a crash
# ---------------------------------------------------------------------------


def test_load_manifest_returns_empty_for_truncated_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    path = tmp_path / "manifest.json"
    path.write_text('{"aaaa": {"path": "A.md", "chunk_ids": [', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="my_daemon.pipeline.ingest"):
        assert _load_manifest(path) == {}

    assert "manifest" in caplog.text
    assert str(path) in caplog.text


def test_load_manifest_returns_empty_for_wrong_json_shape(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert _load_manifest(path) == {}


def test_load_manifest_roundtrips(tmp_path: Path):
    path = tmp_path / "manifest.json"
    _save_manifest(path, {"aaaa": {"path": "A.md"}})
    assert _load_manifest(path) == {"aaaa": {"path": "A.md"}}


def test_manifest_save_that_fails_to_land_leaves_the_previous_one_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "manifest.json"
    _save_manifest(path, {"aaaa": {"path": "A.md"}})
    good_text = path.read_text(encoding="utf-8")

    def exploding_replace(src, dst):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(writer_mod.os, "replace", exploding_replace)
    with pytest.raises(OSError, match="disk full"):
        _save_manifest(path, {"aaaa": {"path": "A.md"}, "bbbb": {"path": "B.md"}})

    assert path.read_text(encoding="utf-8") == good_text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json"]


# ---------------------------------------------------------------------------
# Corrupt manifest, end to end through the pipeline
# ---------------------------------------------------------------------------


class FakeEmbedder:
    dimension = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeVectorStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.upserted: list[str] = []

    def ensure_collection(self) -> None:
        pass

    def delete_by_note_uuid(self, note_uuid: str) -> None:
        self.deleted.append(note_uuid)

    def set_note_path(self, note_uuid: str, rel_path: str) -> None:
        pass

    def upsert(self, chunks, vectors, sparse_vectors=None) -> None:  # noqa: ANN001
        self.upserted.extend(c.id for c in chunks)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("# A\n\nSee [[B]].\n\n#memory\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n\nSomething about daemons.\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(vault: Path, tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = vault
    s.graph.path = tmp_path / "data" / "graph.gpickle"
    s.graph.manifest_path = tmp_path / "data" / "manifest.json"
    s.feedback.db_path = tmp_path / "data" / "state.db"
    return s


def test_ingest_survives_a_corrupt_manifest_by_rescanning(
    settings: Settings, caplog: pytest.LogCaptureFixture
):
    store = GraphStore(path=settings.graph.path)
    first = ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)
    assert first.notes_new_or_updated == 2

    # A second run with an intact manifest skips everything — the baseline the
    # corrupt case has to differ from.
    second = ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)
    assert second.skipped_unchanged == 2
    assert second.notes_new_or_updated == 0

    settings.graph.manifest_path.write_text('{"aaaa": {"path": "A.md",', encoding="utf-8")

    vector_store = FakeVectorStore()
    with caplog.at_level(logging.WARNING, logger="my_daemon.pipeline.ingest"):
        third = ingest_vault(settings, FakeEmbedder(), vector_store, store)

    assert "manifest" in caplog.text
    assert third.notes_scanned == 2
    assert third.notes_new_or_updated == 2
    assert third.skipped_unchanged == 0
    assert third.notes_deleted == 0
    assert third.errors == []
    # The manifest healed itself, so the next run skips again.
    healed = json.loads(settings.graph.manifest_path.read_text(encoding="utf-8"))
    assert sorted(healed) == sorted(derive_path_uuid(rel) for rel in ("A.md", "B.md"))
    fourth = ingest_vault(settings, FakeEmbedder(), FakeVectorStore(), store)
    assert fourth.skipped_unchanged == 2
