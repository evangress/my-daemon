# SPDX-License-Identifier: Apache-2.0
"""Walk an Obsidian vault and yield parsed Notes with resolved wikilinks."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from my_daemon.models import Note
from my_daemon.vault.identity import effective_uuid
from my_daemon.vault.parser import parse_note


class VaultReader:
    """Two-pass reader: first builds a title index, then resolves wikilinks against it.

    Obsidian wikilinks are case-insensitive on the target side. We index by
    lowercased title and lowercased relative_path so ``[[Foo]]`` resolves whether
    the target is ``Foo.md`` or ``notes/foo.md``.
    """

    def __init__(self, vault_root: Path, exclude_dirs: list[str] | None = None) -> None:
        self.vault_root = vault_root.expanduser().resolve()
        self.exclude_dirs = {d.lower() for d in (exclude_dirs or [])}

    def discover(self) -> list[Path]:
        """Return all markdown files in the vault, excluding configured directories."""

        if not self.vault_root.is_dir():
            raise FileNotFoundError(f"Vault path does not exist: {self.vault_root}")

        files: list[Path] = []
        for p in self.vault_root.rglob("*.md"):
            if any(part.startswith(".") for part in p.relative_to(self.vault_root).parts[:-1]):
                continue
            if any(
                part.lower() in self.exclude_dirs for part in p.relative_to(self.vault_root).parts
            ):
                continue
            files.append(p)
        return sorted(files)

    def _build_title_index(self, notes: list[Note]) -> dict[str, tuple[str, str]]:
        """Lookup key -> (relative_path, note_uuid) for every resolvable alias."""

        index: dict[str, tuple[str, str]] = {}
        for n in notes:
            entry = (n.relative_path, effective_uuid(n))
            index.setdefault(n.title.lower(), entry)
            index.setdefault(n.relative_path.lower(), entry)
            # also index by stem so [[Foo]] matches Foo.md regardless of dir
            stem = n.relative_path.rsplit("/", 1)[-1].removesuffix(".md").lower()
            index.setdefault(stem, entry)
        return index

    def _resolve_wikilinks(self, note: Note, index: dict[str, tuple[str, str]]) -> Note:
        """Split each link into a display target plus either an identity or a
        dangling marker. A target with no note has no uuid to borrow."""

        display: list[str] = []
        uuids: list[str] = []
        dangling: list[str] = []
        for target in note.wikilinks:
            key = target.lower().removesuffix(".md")
            hit = index.get(key)
            if hit is None:
                display.append(target)
                dangling.append(target)
            else:
                rel_path, note_uuid = hit
                display.append(rel_path)
                uuids.append(note_uuid)
        return note.model_copy(
            update={
                "wikilinks": display,
                "wikilink_uuids": uuids,
                "dangling_wikilinks": dangling,
            }
        )

    def read_all(self) -> Iterator[Note]:
        """Two-pass: parse every file, build the title index, yield resolved notes."""

        raw_notes = [parse_note(p, self.vault_root) for p in self.discover()]
        index = self._build_title_index(raw_notes)
        for n in raw_notes:
            yield self._resolve_wikilinks(n, index)
