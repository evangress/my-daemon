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
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for whether this file may be modified by the daemon.

    Gates, in order:
      1. Path must resolve inside ``vault_root`` (defends against [[../../etc/passwd]]).
      2. Path must not be inside ``<vault_root>/<agent_folder>``.
      3. Frontmatter must not contain ``daemon: ignore``.
      4. mtime must be older than ``grace_minutes`` (don't collide with a save in progress).
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
    if rel_parts and rel_parts[0].lower() == agent_folder.lower():
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
