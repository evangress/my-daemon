# SPDX-License-Identifier: Apache-2.0
"""Ingestion pipeline: vault → notes → chunks → vectors + graph.

Incremental by default. A small manifest tracks each note's mtime and the chunk
ids it contributed, so unchanged notes are skipped and stale chunks are removed
when a note shrinks or disappears.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.models import Chunk, Note
from my_daemon.stores import GraphStore, VectorStore
from my_daemon.vault import VaultReader, chunk_note


@dataclass
class IngestStats:
    notes_scanned: int = 0
    notes_new_or_updated: int = 0
    notes_deleted: int = 0
    chunks_upserted: int = 0
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def _load_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)


def ingest_vault(
    settings: Settings,
    embedder: Embedder,
    vector_store: VectorStore,
    graph_store: GraphStore,
    sparse_embedder: SparseEmbedder | None = None,
    full_rebuild: bool = False,
    progress: callable | None = None,  # type: ignore[type-arg]
) -> IngestStats:
    stats = IngestStats()

    reader = VaultReader(settings.vault.path, exclude_dirs=settings.vault.exclude_dirs)

    if full_rebuild:
        manifest: dict = {}
        graph_store.graph.clear()
        # Vector store is reset per-note below; for a full rebuild we just clear by note.
    else:
        manifest = _load_manifest(settings.graph.manifest_path)
        graph_store.load()

    vector_store.ensure_collection()

    notes: list[Note] = list(reader.read_all())
    stats.notes_scanned = len(notes)
    current_paths = {n.relative_path for n in notes}

    # Deletions: anything in the manifest no longer on disk
    for rel_path in list(manifest.keys()):
        if rel_path not in current_paths:
            vector_store.delete_by_note(rel_path)
            graph_store.remove_note(rel_path)
            manifest.pop(rel_path, None)
            stats.notes_deleted += 1

    to_process: list[Note] = []
    for note in notes:
        prev = manifest.get(note.relative_path)
        if (
            not full_rebuild
            and prev is not None
            and prev.get("mtime") == note.mtime.isoformat()
        ):
            stats.skipped_unchanged += 1
            continue
        to_process.append(note)

    for i, note in enumerate(to_process):
        if progress:
            progress(i, len(to_process), note.relative_path)
        try:
            chunks: list[Chunk] = chunk_note(
                note,
                max_tokens=settings.chunking.max_tokens,
                overlap_tokens=settings.chunking.overlap_tokens,
            )
            if note.relative_path in manifest:
                vector_store.delete_by_note(note.relative_path)
                graph_store.remove_note(note.relative_path)

            if chunks:
                texts = [c.text for c in chunks]
                vectors = embedder.encode(texts)
                sparse = sparse_embedder.encode(texts) if sparse_embedder is not None else None
                vector_store.upsert(chunks, vectors, sparse_vectors=sparse)

            graph_store.add_note(note, chunk_ids=[c.id for c in chunks])

            manifest[note.relative_path] = {
                "mtime": note.mtime.isoformat(),
                "chunk_ids": [c.id for c in chunks],
                "title": note.title,
            }
            stats.notes_new_or_updated += 1
            stats.chunks_upserted += len(chunks)
        except Exception as exc:
            stats.errors.append(f"{note.relative_path}: {exc!r}")

    graph_store.save()
    _save_manifest(settings.graph.manifest_path, manifest)
    return stats


def ingest_note(
    settings: Settings,
    embedder: Embedder,
    vector_store: VectorStore,
    graph_store: GraphStore,
    note: Note,
    *,
    sparse_embedder: SparseEmbedder | None = None,
    save: bool = True,
) -> int:
    """Incrementally (re)ingest a single already-parsed :class:`Note`.

    Mirrors one iteration of :func:`ingest_vault`'s inner loop so a freshly
    *captured* note (e.g. a Hermes conversation turn written via
    ``integration.core.DaemonCore.remember``) becomes recallable in the same
    session without a full re-scan. The manifest is updated so the next full
    ingest treats the note as already-known. Returns the number of chunks
    upserted.
    """

    chunks: list[Chunk] = chunk_note(
        note,
        max_tokens=settings.chunking.max_tokens,
        overlap_tokens=settings.chunking.overlap_tokens,
    )
    manifest = _load_manifest(settings.graph.manifest_path)

    if note.relative_path in manifest:
        vector_store.delete_by_note(note.relative_path)
        graph_store.remove_note(note.relative_path)

    if chunks:
        vector_store.ensure_collection()
        vectors = embedder.encode([c.text for c in chunks])
        sparse = sparse_embedder.encode([c.text for c in chunks]) if sparse_embedder is not None else None
        vector_store.upsert(chunks, vectors, sparse_vectors=sparse)

    graph_store.add_note(note, chunk_ids=[c.id for c in chunks])
    if save:
        graph_store.save()

    manifest[note.relative_path] = {
        "mtime": note.mtime.isoformat(),
        "chunk_ids": [c.id for c in chunks],
        "title": note.title,
    }
    _save_manifest(settings.graph.manifest_path, manifest)
    return len(chunks)
