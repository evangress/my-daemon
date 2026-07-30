# SPDX-License-Identifier: Apache-2.0
"""Chunk a Note into retrievable units while preserving heading context."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from my_daemon.models import Chunk, Note
from my_daemon.vault.identity import effective_uuid

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

_DEFAULT_HEADER_SPLITS = [("#", "h1"), ("##", "h2"), ("###", "h3")]


def _stable_chunk_id(note_uuid: str, heading_path: tuple[str, ...], index: int, text: str) -> str:
    """Derived from the note's *identity*, never its path.

    A rename therefore leaves every chunk id untouched, which is what lets
    ingest turn a move into a payload update instead of a re-embed.
    """

    payload = f"{note_uuid}::{'/'.join(heading_path)}::{index}::{text}".encode()
    return hashlib.sha1(payload).hexdigest()[:16]


def _approx_token_count(s: str) -> int:
    # Rough heuristic that doesn't require loading a tokenizer at chunk time.
    # Real tokenization happens later at embed time; this is just for splitting decisions.
    return max(1, len(s) // 4)


def chunk_note(note: Note, max_tokens: int = 512, overlap_tokens: int = 50) -> list[Chunk]:
    """Split a Note's body into Chunks, splitting first on headings then on size."""

    note_uuid = effective_uuid(note)
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=_DEFAULT_HEADER_SPLITS)
    # langchain Documents, or the shim below — read structurally, not by type.
    pieces: list[Any] = header_splitter.split_text(note.body) or []

    if not pieces:
        # No headings at all — treat the whole body as one piece.
        if not note.body.strip():
            return []
        pieces = [_BodyShim(note.body)]

    # token-budgeted secondary splitter for oversized pieces
    char_budget = max_tokens * 4
    char_overlap = overlap_tokens * 4
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=char_budget,
        chunk_overlap=char_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    chunks: list[Chunk] = []
    index = 0
    for piece in pieces:
        heading_path = _heading_path_from(piece)
        text = piece.page_content if hasattr(piece, "page_content") else piece.text
        text = text.strip()
        if not text:
            continue

        sub_texts = (
            [text] if _approx_token_count(text) <= max_tokens else size_splitter.split_text(text)
        )

        for sub in sub_texts:
            sub = sub.strip()
            if not sub:
                continue
            wikilinks = [m.group(1).strip() for m in WIKILINK_RE.finditer(sub)]
            chunk_id = _stable_chunk_id(note_uuid, tuple(heading_path), index, sub)
            chunks.append(
                Chunk(
                    id=chunk_id,
                    note_uuid=note_uuid,
                    note_path=note.relative_path,
                    heading_path=heading_path,
                    text=sub,
                    chunk_index=index,
                    tags=list(note.tags),
                    wikilinks=wikilinks,
                    occurred_at=note.occurred_at,
                    occurred_at_source=note.occurred_at_source,
                    modified_at=note.mtime,
                )
            )
            index += 1
    return chunks


def _heading_path_from(piece: object) -> list[str]:
    meta = getattr(piece, "metadata", {}) or {}
    path: list[str] = []
    for key in ("h1", "h2", "h3"):
        if meta.get(key):
            path.append(str(meta[key]))
    return path


class _BodyShim:
    """Minimal stand-in for langchain Document when a note has no headings."""

    def __init__(self, body: str) -> None:
        self.page_content = body
        self.metadata: dict = {}
