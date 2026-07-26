# SPDX-License-Identifier: Apache-2.0
"""Safe vault writeback. Every change the daemon makes to a user's markdown
file goes through this module so the safety discipline (containment check,
opt-out, snapshot, idempotent sentinel markers, atomic write) is established
once instead of three times.

The agent never rewrites the user's prose. It only:
- writes an `## Agent Notes` footer section between sentinel comment markers
- inserts wikilinks into the body for exact-title matches (skipping code fences
  and existing links)
- extends the frontmatter ``tags:`` list
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import frontmatter

AGENT_SECTION_HEADING = "## Agent Notes"
SENTINEL_START = "<!-- daemon:start -->"
SENTINEL_END = "<!-- daemon:end -->"

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_EXISTING_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")


@dataclass
class WriteResult:
    """Outcome of a write attempt. ``changed=False`` means no-op (already up to date or gated out)."""

    path: Path
    changed: bool
    reason: str = ""
    snapshot: Path | None = None


def is_writable(
    note_path: Path,
    *,
    vault_root: Path,
    agent_folder: str = "Agent",
    grace_minutes: int = 30,
    allow_agent_folder: bool = False,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for whether this file may be modified by the daemon.

    Gates, in order:
      1. Path must resolve inside ``vault_root`` (defends against [[../../etc/passwd]]).
      2. Path must not be inside ``<vault_root>/<agent_folder>``.
      3. Frontmatter must not contain ``daemon: ignore``.
      4. mtime must be older than ``grace_minutes`` (don't collide with a save in progress).

    ``allow_agent_folder`` relaxes gate 2 only. That gate exists to stop the
    daemon rewriting its own generated *prose*; stamping an identity key into
    its own files is a different act, and the observer letters need to be
    addressable like any other note. Gates 1, 3 and 4 always apply.
    """
    try:
        resolved = note_path.resolve(strict=True)
    except FileNotFoundError:
        return False, f"file does not exist: {note_path}"
    root_resolved = vault_root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return False, f"path escapes vault: {resolved}"

    rel_parts = resolved.relative_to(root_resolved).parts
    if not allow_agent_folder and rel_parts and rel_parts[0].lower() == agent_folder.lower():
        return False, f"inside {agent_folder}/ (daemon-owned)"

    try:
        post = frontmatter.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"could not parse frontmatter: {exc!r}"
    if str(post.metadata.get("daemon", "")).strip().lower() == "ignore":
        return False, "frontmatter daemon: ignore"

    mtime = resolved.stat().st_mtime
    if time.time() - mtime < grace_minutes * 60:
        return False, f"modified within last {grace_minutes} minutes — grace period"

    return True, "ok"


# ---------------------------------------------------------------------------
# Textual single-key frontmatter editing
#
# A `frontmatter.loads`/`dumps` round-trip cannot be used to change one key:
# PyYAML reorders keys, strips comments, requotes strings, expands flow-style
# lists into block style, re-renders dates, and normalizes line endings. That is
# acceptable nowhere, and catastrophic for a migration that touches every note
# in the vault. These helpers change exactly one line and leave every other byte
# of the file alone.
# ---------------------------------------------------------------------------

_BOM = "﻿"
_FENCE = ("---", "...")


def detect_newline(text: str) -> str:
    """The file's dominant line ending, so inserted lines match their neighbours."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    """``(first_key_index, closing_fence_index)`` of the frontmatter block.

    The opening fence only counts on the very first line — a ``---`` further
    down is a horizontal rule, not frontmatter. Returns ``None`` when there is
    no block, or when it is never closed.
    """

    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") in _FENCE:
            return 1, i
    return None


def _key_pattern(key: str) -> re.Pattern[str]:
    # `uuid\s*:` deliberately does not match `uuid_source:`.
    return re.compile(rf"^\s*{re.escape(key)}\s*:")


def _read_for_edit(note_path: Path) -> tuple[str, list[str], str]:
    """``(bom, lines_with_endings, dominant_newline)``.

    Reads with ``newline=""`` so universal-newline mode does *not* silently
    translate CRLF to LF — that translation happens on read, not on write, and
    would turn every edit to a Windows-synced vault into a whole-file diff.
    """
    with note_path.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    bom = ""
    if raw.startswith(_BOM):
        bom, raw = _BOM, raw[len(_BOM) :]
    return bom, raw.splitlines(keepends=True), detect_newline(raw)


def set_frontmatter_key_textual(
    note_path: Path,
    key: str,
    value: str,
    *,
    vault_root: Path,
    agent_folder: str = "Agent",
    allow_agent_folder: bool = False,
    grace_minutes: int = 2,
) -> WriteResult:
    """Insert or replace a single scalar ``key: value`` line in the frontmatter.

    Everything outside that one line is preserved byte-for-byte, including
    comments, key order, quoting style, and line endings. A note with no
    frontmatter gains a minimal block. New keys go at the end of the block —
    the least surprising diff.

    ``value`` is written bare, so it must be YAML-safe as-is (a canonical UUID
    is). This is not a general-purpose YAML writer.
    """

    ok, reason = is_writable(
        note_path,
        vault_root=vault_root,
        agent_folder=agent_folder,
        grace_minutes=grace_minutes,
        allow_agent_folder=allow_agent_folder,
    )
    if not ok:
        return WriteResult(path=note_path, changed=False, reason=reason)

    bom, lines, nl = _read_for_edit(note_path)
    new_text = f"{key}: {value}"
    bounds = _frontmatter_bounds(lines)

    if bounds is None:
        lines = [f"---{nl}", f"{new_text}{nl}", f"---{nl}", *lines]
    else:
        first, close = bounds
        pattern = _key_pattern(key)
        for i in range(first, close):
            if not pattern.match(lines[i]):
                continue
            current = lines[i].split(":", 1)[1].strip().strip("'\"")
            if current == value:
                return WriteResult(path=note_path, changed=False, reason="already set")
            body = lines[i].rstrip("\r\n")
            ending = lines[i][len(body) :] or nl
            lines[i] = f"{new_text}{ending}"
            break
        else:
            lines.insert(close, f"{new_text}{nl}")

    _atomic_write_text(note_path, bom + "".join(lines))
    return WriteResult(path=note_path, changed=True, reason="ok")


def remove_frontmatter_key_textual(
    note_path: Path,
    key: str,
    *,
    vault_root: Path,
    agent_folder: str = "Agent",
    allow_agent_folder: bool = False,
    grace_minutes: int = 2,
    drop_empty_block: bool = False,
) -> WriteResult:
    """Delete a single ``key:`` line from the frontmatter. The rollback path.

    Surgical by design: it never restores a body, so it stays safe to run on a
    file the user has edited since the key was written.

    ``drop_empty_block`` also removes the frontmatter fences when the deletion
    empties them — what rollback needs, so a note that had no frontmatter
    before the migration is left with none after it.
    """

    ok, reason = is_writable(
        note_path,
        vault_root=vault_root,
        agent_folder=agent_folder,
        grace_minutes=grace_minutes,
        allow_agent_folder=allow_agent_folder,
    )
    if not ok:
        return WriteResult(path=note_path, changed=False, reason=reason)

    bom, lines, _nl = _read_for_edit(note_path)
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return WriteResult(path=note_path, changed=False, reason="no frontmatter")

    first, close = bounds
    pattern = _key_pattern(key)
    for i in range(first, close):
        if pattern.match(lines[i]):
            del lines[i]
            if drop_empty_block and close - first == 1:
                # That was the only key — remove the fences too. Indices shifted
                # by one when the key line went, so the closing fence is now at
                # `first` and the opening is at 0.
                del lines[first]
                del lines[0]
            _atomic_write_text(note_path, bom + "".join(lines))
            return WriteResult(path=note_path, changed=True, reason="ok")

    return WriteResult(path=note_path, changed=False, reason=f"no {key}: key")


def snapshot(note_path: Path, *, vault_root: Path, agent_folder: str = "Agent") -> Path:
    """Copy the file to ``<vault>/<agent_folder>/backups/<rel>.<ts>.md``. Returns the snapshot path."""
    rel = note_path.resolve().relative_to(vault_root.resolve())
    ts = int(time.time())
    backup_path = vault_root / agent_folder / "backups" / f"{rel}.{ts}.md"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(note_path, backup_path)
    return backup_path


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via tempfile + os.replace — never a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".daemon-write-", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def write_atomic(path: Path, text: str) -> None:
    """Public wrapper around the tempfile/replace dance. Used by the reflector."""
    _atomic_write_text(path, text)


def _split_around_section(body: str) -> tuple[str, str | None]:
    """Return ``(body_before_agent_section, existing_agent_section_or_None)``.

    The agent section is recognised by its H2 heading at the start of a line.
    If the heading appears more than once, raises — refusing to mangle.
    """
    heading_re = re.compile(rf"^{re.escape(AGENT_SECTION_HEADING)}\s*$", re.MULTILINE)
    matches = list(heading_re.finditer(body))
    if len(matches) > 1:
        raise ValueError(
            f"Refusing to write: found {len(matches)} '{AGENT_SECTION_HEADING}' headings; "
            "expected at most one."
        )
    if not matches:
        return body, None
    cut = matches[0].start()
    return body[:cut], body[cut:]


def _build_agent_section(inner_body: str) -> str:
    """Compose the full ``## Agent Notes`` block with sentinel markers around ``inner_body``."""
    inner_body = inner_body.strip("\n")
    return f"{AGENT_SECTION_HEADING}\n{SENTINEL_START}\n{inner_body}\n{SENTINEL_END}\n"


def _serialize(post: frontmatter.Post, *, had_frontmatter: bool) -> str:
    """Round-trip a Post — but don't add an empty frontmatter block if there wasn't one."""
    if had_frontmatter or post.metadata:
        return frontmatter.dumps(post) + "\n"
    return post.content if post.content.endswith("\n") else post.content + "\n"


def write_agent_section(
    note_path: Path,
    inner_body: str,
    *,
    vault_root: Path,
    agent_folder: str = "Agent",
    grace_minutes: int = 30,
) -> WriteResult:
    """Replace (or append) the ``## Agent Notes`` section in ``note_path``.

    The section is delimited by ``<!-- daemon:start --> … <!-- daemon:end -->``,
    making re-runs perfectly idempotent: everything outside the markers is
    preserved byte-for-byte.
    """
    ok, reason = is_writable(
        note_path, vault_root=vault_root, agent_folder=agent_folder, grace_minutes=grace_minutes
    )
    if not ok:
        return WriteResult(path=note_path, changed=False, reason=reason)

    text = note_path.read_text(encoding="utf-8")
    had_frontmatter = text.lstrip().startswith("---")
    post = frontmatter.loads(text)
    body = post.content

    try:
        before, _existing = _split_around_section(body)
    except ValueError as exc:
        # Two ## Agent Notes headings — refuse rather than mangle.
        return WriteResult(path=note_path, changed=False, reason=str(exc))

    new_section = _build_agent_section(inner_body)
    if not before.endswith("\n\n") and before:
        before = before.rstrip("\n") + "\n\n"
    new_body = (before + new_section).rstrip() + "\n"

    if new_body == body:
        return WriteResult(path=note_path, changed=False, reason="content unchanged")

    snap = snapshot(note_path, vault_root=vault_root, agent_folder=agent_folder)
    post.content = new_body
    _atomic_write_text(note_path, _serialize(post, had_frontmatter=had_frontmatter))
    return WriteResult(path=note_path, changed=True, reason="agent section written", snapshot=snap)


def _mask_safe_zones(body: str) -> tuple[str, list[tuple[int, int]]]:
    """Return ``(body, off_limit_spans)`` — ranges where wikilink insertion is forbidden.

    Off-limits: inside an existing ``[[wikilink]]``, inside a fenced code block.
    """
    spans: list[tuple[int, int]] = []
    for m in _CODE_FENCE_RE.finditer(body):
        spans.append((m.start(), m.end()))
    for m in _EXISTING_WIKILINK_RE.finditer(body):
        spans.append((m.start(), m.end()))
    return body, spans


def _in_any_span(idx: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= idx < end for start, end in spans)


def insert_wikilinks(
    note_path: Path,
    links: list[tuple[str, str]],
    *,
    vault_root: Path,
    agent_folder: str = "Agent",
    grace_minutes: int = 30,
) -> WriteResult:
    """Rewrite the *first* case-insensitive word-boundary match of each ``anchor`` to ``[[target|anchor]]``.

    ``links`` is ``[(target_title, anchor_text_in_body), …]``. Skips anchors that
    sit inside an existing wikilink or a fenced code block. Skips anchors that
    don't appear in the body at all (silently — caller decides what to log).
    """
    ok, reason = is_writable(
        note_path, vault_root=vault_root, agent_folder=agent_folder, grace_minutes=grace_minutes
    )
    if not ok:
        return WriteResult(path=note_path, changed=False, reason=reason)

    text = note_path.read_text(encoding="utf-8")
    had_frontmatter = text.lstrip().startswith("---")
    post = frontmatter.loads(text)
    body = post.content
    original_body = body

    for target, anchor in links:
        body, spans = _mask_safe_zones(body)
        pattern = re.compile(rf"(?<!\w){re.escape(anchor)}(?!\w)", re.IGNORECASE)
        for m in pattern.finditer(body):
            if _in_any_span(m.start(), spans):
                continue
            insertion = f"[[{target}|{m.group(0)}]]"
            body = body[: m.start()] + insertion + body[m.end():]
            break  # first match only — strict per plan

    if body == original_body:
        return WriteResult(path=note_path, changed=False, reason="no anchors matched")

    snap = snapshot(note_path, vault_root=vault_root, agent_folder=agent_folder)
    post.content = body
    _atomic_write_text(note_path, _serialize(post, had_frontmatter=had_frontmatter))
    return WriteResult(path=note_path, changed=True, reason=f"inserted {len(links)} wikilink(s)", snapshot=snap)


def add_tags(
    note_path: Path,
    tags: list[str],
    *,
    vault_root: Path,
    agent_folder: str = "Agent",
    grace_minutes: int = 30,
) -> WriteResult:
    """Extend the frontmatter ``tags`` list with ``tags``, preserving original ordering.

    Creates the ``tags`` block if absent. Strips leading ``#`` from each tag.
    Never writes inline ``#tag`` markers — frontmatter is the safer surface.
    """
    ok, reason = is_writable(
        note_path, vault_root=vault_root, agent_folder=agent_folder, grace_minutes=grace_minutes
    )
    if not ok:
        return WriteResult(path=note_path, changed=False, reason=reason)

    text = note_path.read_text(encoding="utf-8")
    post = frontmatter.loads(text)
    existing_raw = post.metadata.get("tags") or []
    if isinstance(existing_raw, str):
        existing_raw = [existing_raw]
    existing = [str(t).lstrip("#") for t in existing_raw]

    additions = [t.lstrip("#") for t in tags if t.lstrip("#") not in set(existing)]
    if not additions:
        return WriteResult(path=note_path, changed=False, reason="no new tags")

    post.metadata["tags"] = existing + additions
    snap = snapshot(note_path, vault_root=vault_root, agent_folder=agent_folder)
    # add_tags always writes frontmatter (it's the whole point) — force serialization.
    _atomic_write_text(note_path, _serialize(post, had_frontmatter=True))
    return WriteResult(path=note_path, changed=True, reason=f"added tags: {additions}", snapshot=snap)
