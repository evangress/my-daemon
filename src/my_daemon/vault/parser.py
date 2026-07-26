# SPDX-License-Identifier: Apache-2.0
"""Parse a single markdown file into a Note: frontmatter, wikilinks, tags."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from my_daemon.models import Note
from my_daemon.vault.identity import read_note_uuid

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# Inline tag: # followed by a letter, then word/-/slash chars. Negative lookbehind
# prevents matching inside words ("foo#bar") and URLs ("https://x.io/#section").
INLINE_TAG_RE = re.compile(r"(?<![\w/#])#([A-Za-z][\w/\-]*)")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _extract_title(body: str, fallback: str) -> str:
    match = H1_RE.search(body)
    if match:
        return match.group(1).strip()
    return fallback


def _strip_code_fences(text: str) -> str:
    """Remove fenced code blocks so inline tags inside them don't pollute the tag list."""

    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def parse_note(file_path: Path, vault_root: Path) -> Note:
    """Read a single markdown file and produce a :class:`Note`.

    Wikilinks here are stored as the raw target text. Resolution to vault-relative
    paths happens in the reader, which has the full title index.
    """

    text = file_path.read_text(encoding="utf-8")
    post = frontmatter.loads(text)
    body = post.content
    fm = dict(post.metadata)

    rel_path = file_path.relative_to(vault_root).as_posix()
    title = _extract_title(body, fallback=file_path.stem)

    raw_wikilinks = [m.group(1).strip() for m in WIKILINK_RE.finditer(body)]
    # de-duplicate while preserving order
    seen: set[str] = set()
    wikilinks: list[str] = []
    for w in raw_wikilinks:
        if w not in seen:
            seen.add(w)
            wikilinks.append(w)

    tag_source = _strip_code_fences(body)
    inline_tags = {m.group(1) for m in INLINE_TAG_RE.finditer(tag_source)}
    fm_tags_raw = fm.get("tags") or []
    if isinstance(fm_tags_raw, str):
        fm_tags_raw = [fm_tags_raw]
    fm_tags = {str(t).lstrip("#") for t in fm_tags_raw}
    tags = sorted(inline_tags | fm_tags)

    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC)
    word_count = len(body.split())
    note_uuid, uuid_source = read_note_uuid(fm)

    return Note(
        path=file_path,
        relative_path=rel_path,
        title=title,
        body=body,
        frontmatter=fm,
        wikilinks=wikilinks,
        tags=tags,
        mtime=mtime,
        word_count=word_count,
        uuid=note_uuid,
        uuid_source=uuid_source,
    )
