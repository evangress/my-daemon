# SPDX-License-Identifier: Apache-2.0
"""Where the Anthropic API key lives, in order of preference.

The key is resolved from three layers, first hit wins:

1. **A real environment variable.** ``ANTHROPIC_API_KEY`` in the process
   environment. Kept on top so CI, containers, and ``export`` in a shell
   profile keep working with no keychain involved.
2. **The OS credential store**, via :mod:`keyring` — Windows Credential
   Manager (DPAPI-encrypted), macOS Keychain, or Linux Secret Service. This is
   where ``daemon setup`` writes, and the only layer that is encrypted at rest.
3. **A legacy ``.env`` file**, read directly. Supported so existing installs
   keep running, but it is plaintext on disk and the caller is expected to
   nag — see :attr:`ResolvedKey.is_plaintext`.

Reading ``.env`` *directly*, rather than through ``load_dotenv``, is the whole
reason provenance works: once dotenv has merged the file into ``os.environ``, a
plaintext key is indistinguishable from a properly-stored one, and the daemon
can no longer tell the user their secret is sitting in a text file.

Nothing here ever logs, prints, or raises the key itself — a traceback from a
scheduled job must not leak the secret into a log file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import keyring
import keyring.errors
from dotenv import dotenv_values

__all__ = [
    "ENV_VAR",
    "KEYCHAIN_SERVICE",
    "KEYCHAIN_USERNAME",
    "KeySource",
    "ResolvedKey",
    "delete_from_keychain",
    "purge_dotenv_key",
    "read_keychain",
    "resolve_api_key",
    "store_in_keychain",
]

ENV_VAR = "ANTHROPIC_API_KEY"

# Identifies the credential in the OS store. Shows up verbatim in the Windows
# Credential Manager UI and in macOS Keychain Access, so it is named for a
# human reading that list, not for the code.
KEYCHAIN_SERVICE = "my-daemon"
KEYCHAIN_USERNAME = "anthropic-api-key"

KeySource = Literal["environment", "keychain", "dotenv", "missing"]


@dataclass(frozen=True)
class ResolvedKey:
    """A key plus where it came from — the provenance is the point."""

    value: str | None
    source: KeySource
    dotenv_path: Path | None = None

    @property
    def is_plaintext(self) -> bool:
        """True when the key was read from a file anyone can `cat`."""
        return self.source == "dotenv"


def _clean(value: str | None) -> str | None:
    """Whitespace-only is absent, not present-but-empty.

    `setx ANTHROPIC_API_KEY ""` and a stray `ANTHROPIC_API_KEY=` line both
    produce a value that is technically set; treating those as present would
    send an empty key to the API and surface as a confusing 401.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def read_keychain() -> str | None:
    """The key from the OS credential store, or None if absent/unavailable.

    A machine with no usable backend (headless Linux, a cron job with no
    session bus) raises out of keyring; that is a normal condition here, not an
    error, so it degrades to None and lets the next layer answer.
    """
    try:
        return _clean(keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME))
    except keyring.errors.KeyringError:
        return None


def store_in_keychain(value: str) -> tuple[bool, str]:
    """Write the key to the OS credential store. Returns (ok, message).

    Returns rather than raises because the only caller is a GUI save button,
    which needs to render the failure. The message never contains the key.
    """
    try:
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, value)
    except keyring.errors.KeyringError as exc:
        return False, f"No usable OS credential store ({type(exc).__name__}: {exc})."
    return True, f"Saved to the OS credential store as {KEYCHAIN_SERVICE}/{KEYCHAIN_USERNAME}."


def delete_from_keychain() -> bool:
    """Remove the stored key. Absent or unavailable both count as 'nothing to do'."""
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME)
    except keyring.errors.KeyringError:
        return False
    return True


def _read_dotenv(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return _clean(dotenv_values(path).get(ENV_VAR))
    except OSError:
        return None


def resolve_api_key(
    *,
    env_value: str | None = None,
    dotenv_paths: list[Path] | tuple[Path, ...] = (),
) -> ResolvedKey:
    """Resolve the key across all layers, reporting which one answered.

    Inputs are passed in rather than read from `os.environ` here so the
    precedence rules stay a pure function — the impure environment and
    filesystem reads live at the `load_settings` boundary.
    """
    from_env = _clean(env_value)
    if from_env:
        return ResolvedKey(from_env, "environment")

    from_keychain = read_keychain()
    if from_keychain:
        return ResolvedKey(from_keychain, "keychain")

    for path in dotenv_paths:
        from_file = _read_dotenv(path)
        if from_file:
            return ResolvedKey(from_file, "dotenv", dotenv_path=path)

    return ResolvedKey(None, "missing")


def purge_dotenv_key(path: Path) -> bool:
    """Strip the API key line from a `.env`, leaving every other line intact.

    Returns True only when something was actually removed, so callers can stay
    quiet on the common no-op. Rewritten line-by-line rather than via
    `dotenv.unset_key` so an untouched file is left byte-identical.
    """
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    kept = [ln for ln in lines if not _is_key_line(ln)]
    if len(kept) == len(lines):
        return False

    try:
        path.write_text("".join(kept), encoding="utf-8")
    except OSError:
        return False
    return True


def _is_key_line(line: str) -> bool:
    """`ANTHROPIC_API_KEY=...`, with or without a leading `export`."""
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    return stripped.startswith(f"{ENV_VAR}=")
