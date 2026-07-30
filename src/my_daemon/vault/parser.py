# SPDX-License-Identifier: Apache-2.0
"""Parse a single markdown file into a Note: frontmatter, wikilinks, tags."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter
import yaml

from my_daemon.models import Note
from my_daemon.vault.dates import DEFAULT_DATE_KEYS, derive_occurred_at
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


def parse_note(
    file_path: Path,
    vault_root: Path,
    *,
    date_keys: Sequence[str] = DEFAULT_DATE_KEYS,
    mtime_trusted_before: date | None = None,
) -> Note:
    """Read a single markdown file and produce a :class:`Note`.

    Wikilinks here are stored as the raw target text. Resolution to vault-relative
    paths happens in the reader, which has the full title index.
    """

    text = file_path.read_text(encoding="utf-8")
    try:
        post = frontmatter.loads(text)
        body = post.content
        fm = dict(post.metadata)
    except yaml.YAMLError:
        # Real vaults hold YAML like `related: [[A]], [[B]]` — unquoted
        # wikilinks are invalid block-mapping syntax. One bad note must not
        # kill ingest: degrade to no metadata and keep the *raw* text as the
        # body, so the content is still embedded and any wikilinks inside the
        # broken block still register through the body regex below.
        body = text
        fm = {}

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
    occurred_at, occurred_at_source = derive_occurred_at(
        fm, rel_path, mtime, keys=date_keys, trusted_before=mtime_trusted_before
    )
    word_count = len(body.split())
    note_uuid, uuid_source = read_note_uuid(fm)

    return Note(
        path=file_path,
        relative_path=rel_path,
        title=title,
        body=body,
        frontmatter=fm,
        wikilinks=wikilinks,
        # Unresolved is the honest default: only the reader holds the title
        # index needed to turn a link into an identity. A Note that never goes
        # through it (single-note capture, agent_link) still keeps its links,
        # as dangling targets, instead of silently losing them.
        dangling_wikilinks=list(wikilinks),
        tags=tags,
        mtime=mtime,
        occurred_at=occurred_at,
        occurred_at_source=occurred_at_source,
        word_count=word_count,
        uuid=note_uuid,
        uuid_source=uuid_source,
    )
