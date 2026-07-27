# SPDX-License-Identifier: Apache-2.0
"""Note identity: reading a stable UUID off disk, and deriving one when we can't.

**Frontmatter is the source of truth. The registry is derived state.** If the
two disagree, frontmatter wins and the registry is corrected. That invariant is
what makes vault sync across machines work, makes ``daemon reset`` safe, and
makes a hand-edited UUID a recoverable situation rather than a corruption.
"""

from __future__ import annotations

import uuid as uuidlib

# A fixed namespace for every uuid5 this project derives. Never change it —
# doing so would silently re-identify every note that relies on derivation.
NAMESPACE = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "https://github.com/evangress/my-daemon")

# Frontmatter keys that may carry an identity, in priority order. `uuid` is
# ours (and what obsidian-front-matter-uuid writes); `id` is what the core
# "Unique note ID" workflow and several Zettelkasten plugins use.
ADOPT_KEYS: tuple[str, ...] = ("uuid", "uid", "id", "guid", "note-id", "permanent_id")

# The key we write. It has a defined type, so a non-UUID value in it means
# corruption or a hand-edit — never something to derive a *new* identity from.
OUR_KEY = "uuid"

# Below this length an `id:` is a field, not an identity — `id: 3` is a
# numbering scheme, not something to hang a note's whole history on.
_MIN_OPAQUE_ID_LEN = 8


def canonicalize(value: object) -> str | None:
    """Return the canonical lowercase-hyphenated form, or None if not a UUID.

    Accepts the forms people and plugins actually write: uppercase, braced,
    and ``urn:uuid:`` prefixed.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return str(uuidlib.UUID(text))
    except (ValueError, AttributeError):
        return None


def derive_path_uuid(rel_path: str) -> str:
    """The fallback identity for a note we may not write to.

    Deterministic, so two machines agree without sharing a registry — but
    **not rename-stable**, which is exactly today's behaviour. Notes carrying
    one are recorded with ``uuid_source='derived_path'`` so ``daemon status``
    can report how much of the vault is still fragile.
    """

    return str(uuidlib.uuid5(NAMESPACE, f"my-daemon/note-path/{rel_path}"))


def derive_adopted_uuid(key: str, value: str) -> str:
    """A stable UUID for a foreign, non-UUID identity (e.g. a Zettelkasten id).

    Deterministic in the same way as :func:`derive_path_uuid`, so a re-run on a
    machine with no registry converges on the same answer.
    """

    return str(uuidlib.uuid5(NAMESPACE, f"my-daemon/adopted/{key}/{value}"))


def effective_uuid(note) -> str:  # noqa: ANN001 — avoids a models import cycle
    """A note's identity, always. Stamped if it has one, path-derived if not.

    Nothing downstream needs to care which: everything joins on a UUID string.
    Only the registry records whether the identity is rename-stable.
    """

    return note.uuid or derive_path_uuid(note.relative_path)


def read_note_uuid(fm: dict) -> tuple[str | None, str | None]:
    """Resolve a note's identity from its frontmatter.

    Returns ``(uuid, source)`` where source is ``adopted:<key>`` for a value
    that already was a UUID, ``derived:<key>`` for one derived from a foreign
    opaque id, or ``(None, None)`` when the note carries no usable identity.
    """

    for key in ADOPT_KEYS:
        if key not in fm:
            continue
        raw = fm[key]

        found = canonicalize(raw)
        if found is not None:
            return found, f"adopted:{key}"

        # Garbage in *our* key reads as absent, so ingest restores the
        # registry's value rather than minting a new one and orphaning the
        # note's history. Deriving is only ever right for a foreign key.
        if key == OUR_KEY:
            continue

        # Not a UUID, but possibly a real identity under another scheme.
        if isinstance(raw, str):
            text = raw.strip()
            if len(text) >= _MIN_OPAQUE_ID_LEN:
                return derive_adopted_uuid(key, text), f"derived:{key}"

    return None, None
