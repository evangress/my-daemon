# SPDX-License-Identifier: Apache-2.0
"""Ingestion pipeline: vault → notes → chunks → vectors + graph.

Incremental by default. A small manifest tracks each note's mtime and the chunk
ids it contributed, so unchanged notes are skipped and stale chunks are removed
when a note shrinks or disappears.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from my_daemon.config import Settings
from my_daemon.embeddings import Embedder, SparseEmbedder
from my_daemon.models import Chunk, Note, NoteRecord
from my_daemon.stores import GraphStore, NoteRegistry, VectorStore
from my_daemon.vault import VaultReader, chunk_note
from my_daemon.vault.identity import effective_uuid


@dataclass
class IngestStats:
    notes_scanned: int = 0
    notes_new_or_updated: int = 0
    notes_deleted: int = 0
    # Moved on disk but unchanged in content: a payload update, no embedding.
    notes_renamed: int = 0
    # Frontmatter changed but the body didn't: tags and links refresh, but
    # there is nothing new to embed.
    notes_metadata_refreshed: int = 0
    chunks_upserted: int = 0
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def _body_hash(note: Note) -> str:
    """Content identity, independent of mtime — which a sync client will bump
    for reasons that have nothing to do with the text."""

    return hashlib.sha256(note.body.encode("utf-8")).hexdigest()


def _frontmatter_hash(note: Note) -> str:
    """Frontmatter identity, so a tag edit is not mistaken for no edit at all.

    Skipping on ``body_sha256`` alone made every frontmatter-only change
    invisible until a full rebuild — including every tag added in Obsidian and
    every theme tag the user accepted.
    """

    return hashlib.sha256(
        repr(sorted(note.frontmatter.items(), key=lambda kv: kv[0])).encode("utf-8")
    ).hexdigest()


def _register(registry: NoteRegistry, note_uuid: str, note: Note, chunk_count: int) -> None:
    registry.upsert(
        NoteRecord(
            uuid=note_uuid,
            rel_path=note.relative_path,
            title=note.title,
            mtime=note.mtime,
            body_sha256=_body_hash(note),
            frontmatter_sha256=_frontmatter_hash(note),
            tags=note.tags,
            word_count=note.word_count,
            chunk_count=chunk_count,
            uuid_source=note.uuid_source or ("assigned" if note.uuid else "derived_path"),
            in_frontmatter=note.uuid is not None,
        )
    )


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

    registry = NoteRegistry(db_path=settings.feedback.db_path)

    notes: list[Note] = list(reader.read_all())
    stats.notes_scanned = len(notes)
    by_uuid: dict[str, Note] = {effective_uuid(n): n for n in notes}

    # Deletions: anything in the manifest whose identity is no longer on disk.
    for note_uuid in list(manifest.keys()):
        if note_uuid not in by_uuid:
            vector_store.delete_by_note_uuid(note_uuid)
            graph_store.remove_note(note_uuid)
            manifest.pop(note_uuid, None)
            registry.soft_delete(note_uuid)
            stats.notes_deleted += 1

    to_process: list[tuple[str, Note]] = []
    for note_uuid, note in by_uuid.items():
        prev = manifest.get(note_uuid)
        if full_rebuild or prev is None:
            to_process.append((note_uuid, note))
            continue

        body_hash = _body_hash(note)
        if prev.get("body_sha256") == body_hash:
            # The body is identical, so nothing needs embedding. Two cheaper
            # things may still have changed.
            chunk_ids = prev.get("chunk_ids", [])
            renamed = prev.get("path") != note.relative_path
            fm_changed = prev.get("frontmatter_sha256") != _frontmatter_hash(note)

            if renamed:
                # Chunk ids derive from the uuid, so the points are already
                # right and one payload field is all that's stale.
                vector_store.set_note_path(note_uuid, note.relative_path)
                manifest[note_uuid]["path"] = note.relative_path
            if renamed or fm_changed:
                # `update_note` is differential, so tags and wikilinks refresh
                # while learned edge weights survive.
                graph_store.update_note(note, chunk_ids=chunk_ids)
                _register(registry, note_uuid, note, len(chunk_ids))
                manifest[note_uuid]["mtime"] = note.mtime.isoformat()
                manifest[note_uuid]["frontmatter_sha256"] = _frontmatter_hash(note)
                stats.notes_renamed += int(renamed)
                stats.notes_metadata_refreshed += int(fm_changed and not renamed)
            else:
                stats.skipped_unchanged += 1
            continue

        to_process.append((note_uuid, note))

    for i, (note_uuid, note) in enumerate(to_process):
        if progress:
            progress(i, len(to_process), note.relative_path)
        try:
            chunks: list[Chunk] = chunk_note(
                note,
                max_tokens=settings.chunking.max_tokens,
                overlap_tokens=settings.chunking.overlap_tokens,
            )
            # Chunks are replaced wholesale — their ids fold in the text, so an
            # edit orphans every old point.
            if note_uuid in manifest:
                vector_store.delete_by_note_uuid(note_uuid)

            if chunks:
                texts = [c.text for c in chunks]
                vectors = embedder.encode(texts)
                sparse = sparse_embedder.encode(texts) if sparse_embedder is not None else None
                vector_store.upsert(chunks, vectors, sparse_vectors=sparse)

            # The graph is updated differentially, NOT replaced. Removing the
            # node first would take every other note's links *to* this one with
            # it, and re-adding would reset the edge weights M1 has learned.
            graph_store.update_note(note, chunk_ids=[c.id for c in chunks])

            manifest[note_uuid] = {
                "path": note.relative_path,
                "mtime": note.mtime.isoformat(),
                "body_sha256": _body_hash(note),
                "frontmatter_sha256": _frontmatter_hash(note),
                "chunk_ids": [c.id for c in chunks],
                "title": note.title,
            }
            _register(registry, note_uuid, note, len(chunks))
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
    note_uuid = effective_uuid(note)

    if note_uuid in manifest:
        vector_store.delete_by_note_uuid(note_uuid)

    if chunks:
        vector_store.ensure_collection()
        vectors = embedder.encode([c.text for c in chunks])
        sparse = sparse_embedder.encode([c.text for c in chunks]) if sparse_embedder is not None else None
        vector_store.upsert(chunks, vectors, sparse_vectors=sparse)

    # Differential, for the same reason as `ingest_vault` — this path runs
    # repeatedly within a single Hermes session as turns are captured.
    graph_store.update_note(note, chunk_ids=[c.id for c in chunks])
    if save:
        graph_store.save()

    manifest[note_uuid] = {
        "path": note.relative_path,
        "mtime": note.mtime.isoformat(),
        "body_sha256": _body_hash(note),
        "frontmatter_sha256": _frontmatter_hash(note),
        "chunk_ids": [c.id for c in chunks],
        "title": note.title,
    }
    _save_manifest(settings.graph.manifest_path, manifest)
    _register(NoteRegistry(db_path=settings.feedback.db_path), note_uuid, note, len(chunks))
    return len(chunks)
