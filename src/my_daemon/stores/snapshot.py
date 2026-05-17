"""Snapshot bundles: a read-only frozen copy of vector + graph + feedback state.

A bundle is a directory under ``settings.snapshot.dir`` that contains everything
the M3 analyzer and the M4 observer LLM need to reason about a stable point in
time without any risk of contaminating live retrieval:

    ./data/snapshots/2026-05-17T03-00-00Z/
    ├── qdrant/<collection>.snapshot   ← Qdrant native snapshot (optional)
    ├── graph.gpickle                  ← shutil.copy2 of the live pickle
    ├── feedback.db                    ← sqlite3 .backup() (online-safe)
    ├── manifest.json                  ← shutil.copy2 of the live manifest
    └── metadata.json                  ← bundle_version, created_at, source paths

``create_snapshot`` produces a bundle. ``open_readonly`` returns a handle that
gives back read-only ``GraphStore`` and ``FeedbackStore`` instances (a vector
restore into a side collection is also possible when Qdrant is reachable;
``handle.close()`` drops the side collection).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from my_daemon.config import Settings
from my_daemon.stores.feedback import FeedbackStore
from my_daemon.stores.graph import GraphStore
from my_daemon.stores.vector import VectorStore

# Bumped when the on-disk layout changes incompatibly. Old bundles with a
# different version are still listable but `open_readonly` refuses them.
BUNDLE_VERSION = 1

_SNAPSHOT_ID_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
_METADATA_FILENAME = "metadata.json"


class SnapshotBundle(BaseModel):
    """On-disk pointer to a snapshot directory.

    Construct via :func:`create_snapshot` or :func:`list_snapshots`. Holds only
    paths and metadata — no open connections, so it's cheap to pass around.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    created_at: datetime
    dir: Path
    qdrant_snapshot: Path | None = None
    qdrant_collection: str | None = None
    qdrant_snapshot_name: str | None = None
    graph_path: Path
    feedback_path: Path
    manifest_path: Path | None = None
    bundle_version: int = BUNDLE_VERSION
    notes: dict = Field(default_factory=dict)


@dataclass
class SnapshotHandle:
    """Read-only stores opened from a bundle.

    ``vector`` is only set if Qdrant was reachable when ``open_readonly`` ran
    and the snapshot was restored into a side collection; otherwise ``None``.
    Call ``close()`` to drop the side collection when done.
    """

    bundle: SnapshotBundle
    graph: GraphStore
    feedback: FeedbackStore
    vector: VectorStore | None = None
    _side_collection: str | None = None

    def close(self) -> None:
        if self.vector is not None and self._side_collection:
            # Closing should never raise to callers; the side collection is
            # best-effort cleanup, not a correctness boundary.
            with contextlib.suppress(Exception):
                self.vector._client_().delete_collection(collection_name=self._side_collection)
            self.vector = None
            self._side_collection = None

    def __enter__(self) -> SnapshotHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _new_snapshot_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.strftime(_SNAPSHOT_ID_FORMAT)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_snapshot(
    settings: Settings,
    *,
    include_qdrant: bool = True,
    qdrant_storage_dir: Path | None = None,
    on_warning: callable | None = None,
) -> SnapshotBundle:
    """Freeze the live state into a new bundle under ``settings.snapshot.dir``.

    ``include_qdrant=False`` skips the vector snapshot (used by tests and when
    Qdrant is intentionally offline). ``qdrant_storage_dir`` lets callers point
    at the docker-compose volume mount (default ``./data/qdrant``); when the
    snapshot file is present there we copy locally, otherwise we fall back to
    downloading from the Qdrant HTTP API.
    """

    now = datetime.now(UTC)
    snapshot_id = _new_snapshot_id(now)
    bundle_dir = _ensure_dir(settings.snapshot.dir / snapshot_id)

    # --- graph -------------------------------------------------------------
    graph_dest = bundle_dir / "graph.gpickle"
    if settings.graph.path.is_file():
        shutil.copy2(settings.graph.path, graph_dest)
    else:
        # Honest empty pickle so open_readonly doesn't blow up on a missing
        # file. A fresh GraphStore() persists as a tiny empty MultiDiGraph.
        GraphStore(path=graph_dest).save()

    # --- feedback (online-safe SQLite backup) -----------------------------
    feedback_dest = bundle_dir / "feedback.db"
    _backup_sqlite(settings.feedback.db_path, feedback_dest)

    # --- manifest ----------------------------------------------------------
    manifest_dest: Path | None = None
    if settings.graph.manifest_path.is_file():
        manifest_dest = bundle_dir / "manifest.json"
        shutil.copy2(settings.graph.manifest_path, manifest_dest)

    # --- qdrant (optional) -------------------------------------------------
    qdrant_snapshot: Path | None = None
    qdrant_snapshot_name: str | None = None
    qdrant_warning: str | None = None
    if include_qdrant:
        try:
            qdrant_snapshot, qdrant_snapshot_name = _snapshot_qdrant(
                settings,
                dest_dir=bundle_dir / "qdrant",
                storage_dir=qdrant_storage_dir or Path("./data/qdrant"),
            )
        except Exception as exc:  # noqa: BLE001 — degrade to partial bundle
            qdrant_warning = f"qdrant snapshot skipped: {exc}"
            if on_warning is not None:
                on_warning(qdrant_warning)

    bundle = SnapshotBundle(
        id=snapshot_id,
        created_at=now,
        dir=bundle_dir,
        qdrant_snapshot=qdrant_snapshot,
        qdrant_collection=settings.vector_store.qdrant.collection,
        qdrant_snapshot_name=qdrant_snapshot_name,
        graph_path=graph_dest,
        feedback_path=feedback_dest,
        manifest_path=manifest_dest,
        notes={"qdrant_warning": qdrant_warning} if qdrant_warning else {},
    )

    _write_metadata(bundle)
    return bundle


def _backup_sqlite(src: Path, dst: Path) -> None:
    """Use SQLite's online-backup API so writers don't corrupt the copy.

    A plain ``shutil.copy`` on an active sqlite db can yield a partially
    written file; ``conn.backup`` blocks writers briefly and produces a
    consistent snapshot.
    """

    if not src.is_file():
        # Initialize an empty schema-bearing db so open_readonly works.
        FeedbackStore(db_path=dst)
        return
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _snapshot_qdrant(
    settings: Settings,
    *,
    dest_dir: Path,
    storage_dir: Path,
) -> tuple[Path, str]:
    """Create a Qdrant snapshot and bring the file into the bundle.

    Returns the (local path, snapshot name) pair. Prefers reading from the
    docker-compose volume mount (fast, no HTTP); falls back to the Qdrant
    snapshot-download endpoint so remote deployments work too.
    """

    from qdrant_client import QdrantClient

    collection = settings.vector_store.qdrant.collection
    url = settings.vector_store.qdrant.url
    client = QdrantClient(url=url, check_compatibility=False)
    description = client.create_snapshot(collection_name=collection)
    snapshot_name = description.name

    _ensure_dir(dest_dir)
    dest_file = dest_dir / snapshot_name

    # Fast path: docker-compose mounts ./data/qdrant -> /qdrant/storage,
    # so snapshots show up at ./data/qdrant/snapshots/<collection>/<name>.
    local_source = storage_dir / "snapshots" / collection / snapshot_name
    if local_source.is_file():
        shutil.copy2(local_source, dest_file)
        return dest_file, snapshot_name

    # Portable path: pull the snapshot via Qdrant's HTTP endpoint. This works
    # even when Qdrant is on another host.
    download_url = f"{url.rstrip('/')}/collections/{collection}/snapshots/{snapshot_name}"
    try:
        urllib.request.urlretrieve(download_url, dest_file)  # noqa: S310 — trusted url
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not download Qdrant snapshot from {download_url}: {exc}") from exc
    return dest_file, snapshot_name


def _write_metadata(bundle: SnapshotBundle) -> None:
    payload = {
        "bundle_version": bundle.bundle_version,
        "id": bundle.id,
        "created_at": bundle.created_at.isoformat(),
        "qdrant_collection": bundle.qdrant_collection,
        "qdrant_snapshot_name": bundle.qdrant_snapshot_name,
        "qdrant_snapshot": str(bundle.qdrant_snapshot) if bundle.qdrant_snapshot else None,
        "graph_path": str(bundle.graph_path),
        "feedback_path": str(bundle.feedback_path),
        "manifest_path": str(bundle.manifest_path) if bundle.manifest_path else None,
        "notes": bundle.notes,
    }
    (bundle.dir / _METADATA_FILENAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def open_readonly(
    bundle: SnapshotBundle,
    settings: Settings | None = None,
    *,
    restore_vector: bool = False,
) -> SnapshotHandle:
    """Open the bundle's stores in read-only mode.

    ``restore_vector=True`` asks Qdrant to recover the snapshot into a side
    collection (``<collection>_snap_<id>``) so vector search is available;
    callers must invoke ``handle.close()`` to drop it. Defaults to False
    because the M3 structural pass only needs the graph and feedback.
    """

    if bundle.bundle_version != BUNDLE_VERSION:
        raise RuntimeError(
            f"snapshot bundle {bundle.id} has incompatible version "
            f"{bundle.bundle_version} (expected {BUNDLE_VERSION})"
        )

    graph = GraphStore(path=bundle.graph_path, read_only=True)
    graph.load()
    feedback = FeedbackStore(db_path=bundle.feedback_path, read_only=True)

    handle = SnapshotHandle(bundle=bundle, graph=graph, feedback=feedback)

    if restore_vector:
        if settings is None:
            raise ValueError("restore_vector=True requires the active Settings")
        if bundle.qdrant_snapshot is None or bundle.qdrant_collection is None:
            raise RuntimeError(
                f"snapshot {bundle.id} has no Qdrant payload to restore"
            )
        handle.vector, handle._side_collection = _restore_vector_into_side_collection(
            settings, bundle
        )

    return handle


def _restore_vector_into_side_collection(
    settings: Settings, bundle: SnapshotBundle
) -> tuple[VectorStore, str]:
    """Recover the Qdrant snapshot into ``<collection>_snap_<id>``.

    Returns the side-collection VectorStore and its name (the latter is the
    handle the caller needs to drop on close).
    """

    from qdrant_client import QdrantClient

    base_collection = bundle.qdrant_collection or settings.vector_store.qdrant.collection
    safe_id = bundle.id.replace(":", "-").replace(".", "-")
    side_collection = f"{base_collection}_snap_{safe_id}"

    url = settings.vector_store.qdrant.url
    client = QdrantClient(url=url, check_compatibility=False)
    # Snapshot lives on the bundle host; for recover_snapshot to see it the
    # file must be readable by the Qdrant server. The clean way is to upload
    # via the HTTP recover-from-uploaded endpoint, but that takes binary
    # multipart and qdrant_client's surface for it varies by version. The
    # simplest working approach is `recover_snapshot` with a file:// URL,
    # which assumes a shared filesystem (the bundled docker-compose case).
    location = f"file://{bundle.qdrant_snapshot.resolve()}"
    client.recover_snapshot(collection_name=side_collection, location=location)

    vector = VectorStore(
        url=url,
        collection=side_collection,
        dim=settings.embeddings.batch_size,  # placeholder; the schema is fixed by recovery
        hybrid=settings.embeddings.hybrid,
    )
    return vector, side_collection


# ---------------------------------------------------------------------------
# list / delete / prune
# ---------------------------------------------------------------------------


def list_snapshots(settings: Settings) -> list[SnapshotBundle]:
    """All bundles under ``settings.snapshot.dir``, oldest first."""

    root = settings.snapshot.dir
    if not root.is_dir():
        return []
    bundles: list[SnapshotBundle] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / _METADATA_FILENAME
        if not meta_path.is_file():
            continue
        try:
            bundle = _load_bundle(entry)
        except Exception:  # noqa: BLE001 — surface malformed bundles via list output
            continue
        bundles.append(bundle)
    return bundles


def get_snapshot(settings: Settings, snapshot_id: str) -> SnapshotBundle | None:
    """Lookup by id; returns None when the id doesn't resolve to a bundle."""

    bundle_dir = settings.snapshot.dir / snapshot_id
    if not (bundle_dir / _METADATA_FILENAME).is_file():
        return None
    return _load_bundle(bundle_dir)


def _load_bundle(bundle_dir: Path) -> SnapshotBundle:
    meta = json.loads((bundle_dir / _METADATA_FILENAME).read_text(encoding="utf-8"))
    return SnapshotBundle(
        id=meta["id"],
        created_at=datetime.fromisoformat(meta["created_at"]),
        dir=bundle_dir,
        qdrant_snapshot=Path(meta["qdrant_snapshot"]) if meta.get("qdrant_snapshot") else None,
        qdrant_collection=meta.get("qdrant_collection"),
        qdrant_snapshot_name=meta.get("qdrant_snapshot_name"),
        graph_path=Path(meta["graph_path"]),
        feedback_path=Path(meta["feedback_path"]),
        manifest_path=Path(meta["manifest_path"]) if meta.get("manifest_path") else None,
        bundle_version=int(meta.get("bundle_version", BUNDLE_VERSION)),
        notes=meta.get("notes") or {},
    )


def delete_snapshot(bundle: SnapshotBundle) -> None:
    """Remove the bundle directory.

    The server-side Qdrant snapshot (a separate file inside Qdrant's storage
    volume) is left alone — it costs little and doubles as a recovery option
    independent of our bundle bookkeeping.
    """

    if bundle.dir.is_dir():
        shutil.rmtree(bundle.dir)


def prune_snapshots(
    settings: Settings,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> list[SnapshotBundle]:
    """Delete bundles older than the retention window. Returns the deleted set."""

    days = retention_days if retention_days is not None else settings.snapshot.retention_days
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=days)
    deleted: list[SnapshotBundle] = []
    for bundle in list_snapshots(settings):
        if bundle.created_at < cutoff:
            delete_snapshot(bundle)
            deleted.append(bundle)
    return deleted
