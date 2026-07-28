# SPDX-License-Identifier: Apache-2.0
"""One-file Windows installer for My Daemon.

Downloaded and run by ``install-my-daemon.bat``; see
``docs/superpowers/specs/2026-07-28-windows-bootstrap-installer-design.md``.

What it produces::

    %LOCALAPPDATA%\\my-daemon\\app\\   source + .venv — replaced on update
    %APPDATA%\\my-daemon\\             config.yaml + data\\ — never touched
    Start Menu\\Programs\\My Daemon\\   two shortcuts

The split is the point: updating deletes and re-extracts ``app\\``, so anything
the user owns has to live outside it or an update would destroy their vault
index. ``daemon init --user`` already implements exactly that layout.

**Stdlib only, on purpose.** This file is fetched to ``%TEMP%`` and executed
before the project exists on disk, so it cannot import ``my_daemon`` — or
anything else that isn't in a bare CPython.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

OWNER = "evangress"
REPO = "my-daemon"

API_LATEST_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
MASTER_ZIP_URL = f"https://github.com/{OWNER}/{REPO}/archive/refs/heads/master.zip"

# A release asset named like this is the only source we can checksum-verify.
ASSET_PREFIX = "my-daemon-"
ASSET_SUFFIX = ".zip"

MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (3, 15)

APP_DIR_NAME = "my-daemon"
START_MENU_FOLDER = "My Daemon"


class InstallError(RuntimeError):
    """Anything the user needs to read as one actionable line."""


class IntegrityError(InstallError):
    """The download did not match the checksum GitHub published for it."""


@dataclass(frozen=True)
class SourceRef:
    """What we are about to install, and whether we can prove it is intact."""

    version: str
    url: str
    sha256: str | None

    @property
    def verifiable(self) -> bool:
        return self.sha256 is not None

    def describe(self) -> str:
        integrity = (
            "SHA-256 verified" if self.verifiable else "not verified (no checksum published)"
        )
        return f"{self.version} — {self.url}\n  integrity: {integrity}"


# ---------------------------------------------------------------------------
# what to download
# ---------------------------------------------------------------------------


def _fetch_release(url: str) -> dict | None:
    """The latest release payload, or None when there is no release yet."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — fixed https
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise InstallError(f"GitHub returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"Could not reach {url} ({exc.reason})") from exc


def _sha256_from_digest(digest: str | None) -> str | None:
    """GitHub spells asset digests ``sha256:<hex>``. Anything else is unusable."""
    if not digest:
        return None
    algorithm, _, hexdigest = digest.partition(":")
    if algorithm.strip().lower() != "sha256" or not hexdigest.strip():
        return None
    return hexdigest.strip()


def resolve_source(fetch: Callable[[str], dict | None] = _fetch_release) -> SourceRef:
    """Pick what to install, preferring the source we can verify.

    Three outcomes, in descending order of trustworthiness:

    1. A published release **asset** — the only one carrying a checksum.
    2. The release's generated **source archive**. GitHub publishes no digest
       for these, verified against the API on 2026-07-28.
    3. **master.zip**, when no release exists — which is the case for this repo
       today, so this is the branch that actually runs.
    """
    release = fetch(API_LATEST_URL)
    if release is None:
        return SourceRef(version="master", url=MASTER_ZIP_URL, sha256=None)

    version = release.get("tag_name") or "unknown"
    for asset in release.get("assets") or []:
        name = asset.get("name") or ""
        if name.startswith(ASSET_PREFIX) and name.endswith(ASSET_SUFFIX):
            return SourceRef(
                version=version,
                url=asset["browser_download_url"],
                sha256=_sha256_from_digest(asset.get("digest")),
            )

    return SourceRef(version=version, url=release["zipball_url"], sha256=None)


# ---------------------------------------------------------------------------
# fetching and unpacking
# ---------------------------------------------------------------------------


def download(url: str, dest: Path) -> Path:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 — fixed https
            dest.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise InstallError(f"Could not download {url} ({exc.reason})") from exc
    return dest


def verify_sha256(path: Path, expected: str | None) -> None:
    """Check the archive when a checksum exists; say nothing when none does.

    ``expected is None`` means GitHub published no digest for this source, not
    that the file is suspect — refusing to install would make the only working
    path (master, today) impossible.
    """
    if expected is None:
        return
    actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise IntegrityError(
            "Downloaded archive does not match its published checksum.\n"
            f"  expected {expected.lower()}\n  got      {actual}\n"
            "Nothing was installed. Try again; if it repeats, do not proceed."
        )


def _sole_top_level(staging: Path) -> Path:
    """GitHub archives wrap everything in one generated directory; strip it."""
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


def extract_app(zip_path: Path, app_dir: Path) -> Path:
    """Replace ``app_dir`` with the archive's contents.

    Extraction happens into a sibling staging directory first, so a corrupt or
    truncated download fails *before* the working install is removed. A failed
    update that leaves the user with nothing is worse than no update.
    """
    app_dir = Path(app_dir)
    app_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = app_dir.parent / f"{app_dir.name}.incoming"
    shutil.rmtree(staging, ignore_errors=True)

    # Raises BadZipFile here, before anything existing has been touched.
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(staging)

    try:
        source_root = _sole_top_level(staging)
        shutil.rmtree(app_dir, ignore_errors=True)
        shutil.move(str(source_root), str(app_dir))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return app_dir


# ---------------------------------------------------------------------------
# Windows-only steps
#
# Not covered by tests — they need a real Windows session. See the spec's
# stated verification gap. Each is written to degrade rather than abort:
# a missing shortcut is not worth failing an otherwise good install.
# ---------------------------------------------------------------------------


def install_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def check_python_version() -> None:
    current = sys.version_info[:2]
    if not (MIN_PYTHON <= current < MAX_PYTHON_EXCLUSIVE):
        raise InstallError(
            f"My Daemon needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} to "
            f"{MAX_PYTHON_EXCLUSIVE[0]}.{MAX_PYTHON_EXCLUSIVE[1] - 1}; "
            f"this is {current[0]}.{current[1]}."
        )


def run_setup(app_dir: Path) -> None:
    """Delegate to the project's own setup.py — it owns venv + deps."""
    result = subprocess.run([sys.executable, str(app_dir / "setup.py")], cwd=str(app_dir))
    if result.returncode != 0:
        raise InstallError("Dependency install failed. Scroll up for the reason.")


def daemon_exe(app_dir: Path) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return app_dir / ".venv" / scripts / f"daemon{suffix}"


def init_user_config(app_dir: Path) -> None:
    """Create the user-level config once. Never overwrites an existing one."""
    result = subprocess.run(
        [str(daemon_exe(app_dir)), "init", "--user"], cwd=str(app_dir), capture_output=True
    )
    # `daemon init` exits 1 when a config is already there; that is success here.
    if result.returncode not in (0, 1):
        print("  ! could not create the user config; run `daemon init --user` yourself")


def create_shortcuts(app_dir: Path) -> None:
    """Start Menu entries via WScript.Shell.

    The chat shortcut targets ``launch-gui.vbs``, not ``daemon.exe``: a .lnk to
    the exe leaves a console window open for the whole session, which reads as
    "a script ran" rather than "an app opened".
    """
    if os.name != "nt":
        return
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / START_MENU_FOLDER
    folder.mkdir(parents=True, exist_ok=True)

    targets = [
        (folder / "My Daemon.lnk", "wscript.exe", f'"{app_dir / "launch-gui.vbs"}"'),
        (folder / "My Daemon Setup.lnk", str(daemon_exe(app_dir)), "setup"),
    ]
    for link, target, arguments in targets:
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
            f"'{link}'); $s.TargetPath = '{target}'; $s.Arguments = '{arguments}'; "
            f"$s.WorkingDirectory = '{app_dir}'; $s.Save()"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"  ! could not create {link.name} (the app still works)")


def launch_setup(app_dir: Path) -> None:
    try:
        subprocess.Popen([str(daemon_exe(app_dir)), "setup"], cwd=str(app_dir))
    except OSError:
        print("  ! could not open the Setup window; run `daemon setup` yourself")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== My Daemon — installer ===\n")
    try:
        check_python_version()

        root = install_root()
        app_dir = root / "app"
        if app_dir.exists():
            print(f"An install already exists at {app_dir}.")
            print("Updating replaces the program; your vault, config and index are untouched.")
            if input("Update it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Cancelled — nothing was changed.")
                return 0

        ref = resolve_source()
        print(f"Installing {ref.describe()}\n")
        if not ref.verifiable:
            print("  note: GitHub publishes no checksum for this source; relying on HTTPS.\n")

        with tempfile.TemporaryDirectory() as tmp:
            archive = download(ref.url, Path(tmp) / "my-daemon.zip")
            verify_sha256(archive, ref.sha256)
            print(f"Unpacking into {app_dir}")
            extract_app(archive, app_dir)

        print("Installing dependencies (this takes a few minutes the first time)\n")
        run_setup(app_dir)
        init_user_config(app_dir)
        create_shortcuts(app_dir)

        print("\nInstalled. Start Menu > My Daemon.")
        print("Opening Setup so you can pick your vault and enter your API key...")
        launch_setup(app_dir)
        return 0
    except InstallError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
