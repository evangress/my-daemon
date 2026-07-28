# SPDX-License-Identifier: Apache-2.0
"""What the Windows one-file installer decides to download, and what it unpacks.

Spec: `docs/superpowers/specs/2026-07-28-windows-bootstrap-installer-design.md`.

Two facts verified against GitHub on 2026-07-28 shape every test here:

* `/releases/latest` returns **404** for this repo, because no release exists
  yet. The master fallback is therefore not an edge case — it is the only path
  that runs today, so it is tested first and hardest.
* GitHub publishes a `digest` for **uploaded release assets** but not for
  generated source archives. Checksum verification is consequently possible on
  one of the three paths and impossible on the other two, and the installer has
  to be honest about which case it is in rather than implying it verified
  something.

Everything here is offline and OS-agnostic: the release payloads are plain
dicts (house style — no MagicMock), and the archives are real ZIPs built in
`tmp_path`. The Windows-only surface (shortcuts, winget, the `.bat`) is
deliberately not exercised; see the spec's stated verification gap.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.bootstrap import (
    MASTER_ZIP_URL,
    IntegrityError,
    SourceRef,
    extract_app,
    resolve_source,
    verify_sha256,
)

GOOD_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _release(*, tag: str = "v0.1.0", assets: list[dict] | None = None) -> dict:
    """The subset of GitHub's release payload the installer actually reads."""
    return {
        "tag_name": tag,
        "zipball_url": f"https://api.github.com/repos/evangress/my-daemon/zipball/{tag}",
        "assets": assets or [],
    }


# ---------------------------------------------------------------------------
# what to download
# ---------------------------------------------------------------------------


def test_no_release_yet_falls_back_to_master():
    """The only path that runs today — /releases/latest 404s for this repo."""
    ref = resolve_source(fetch=lambda _url: None)

    assert ref.url == MASTER_ZIP_URL
    assert ref.version == "master"


def test_the_master_fallback_admits_it_cannot_be_verified():
    ref = resolve_source(fetch=lambda _url: None)

    assert ref.sha256 is None
    assert ref.verifiable is False


def test_a_published_asset_is_preferred_and_carries_its_digest():
    release = _release(
        assets=[
            {
                "name": "my-daemon-v0.1.0.zip",
                "browser_download_url": "https://github.com/evangress/my-daemon/releases/download/v0.1.0/my-daemon-v0.1.0.zip",
                "digest": f"sha256:{GOOD_SHA}",
            }
        ]
    )

    ref = resolve_source(fetch=lambda _url: release)

    assert ref.version == "v0.1.0"
    assert ref.url.endswith("my-daemon-v0.1.0.zip")
    assert ref.sha256 == GOOD_SHA
    assert ref.verifiable is True


def test_unrelated_release_assets_are_ignored():
    """A release carrying only, say, a checksums file is not an install source."""
    release = _release(
        assets=[
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://example.invalid/SHA256SUMS",
                "digest": f"sha256:{GOOD_SHA}",
            }
        ]
    )

    ref = resolve_source(fetch=lambda _url: release)

    assert ref.url == release["zipball_url"]


def test_a_release_without_an_asset_uses_the_source_archive_unverified():
    """GitHub publishes no digest for generated source archives."""
    ref = resolve_source(fetch=lambda _url: _release())

    assert ref.url == _release()["zipball_url"]
    assert ref.sha256 is None
    assert ref.verifiable is False


def test_a_digest_in_an_unexpected_algorithm_is_not_treated_as_sha256():
    release = _release(
        assets=[
            {
                "name": "my-daemon-v0.1.0.zip",
                "browser_download_url": "https://example.invalid/a.zip",
                "digest": "sha512:beef",
            }
        ]
    )

    ref = resolve_source(fetch=lambda _url: release)

    assert ref.sha256 is None
    assert ref.verifiable is False


# ---------------------------------------------------------------------------
# integrity
# ---------------------------------------------------------------------------


def test_a_matching_checksum_passes(tmp_path: Path):
    blob = tmp_path / "a.zip"
    blob.write_bytes(b"")  # sha256 of empty input is GOOD_SHA

    verify_sha256(blob, GOOD_SHA)  # must not raise


def test_a_mismatched_checksum_is_refused(tmp_path: Path):
    blob = tmp_path / "a.zip"
    blob.write_bytes(b"tampered")

    with pytest.raises(IntegrityError):
        verify_sha256(blob, GOOD_SHA)


def test_an_unverifiable_download_is_allowed_through(tmp_path: Path):
    """sha256=None means "GitHub gave us nothing", not "the file is bad"."""
    blob = tmp_path / "a.zip"
    blob.write_bytes(b"whatever")

    verify_sha256(blob, None)  # must not raise


# ---------------------------------------------------------------------------
# unpacking
# ---------------------------------------------------------------------------


def _make_zip(path: Path, top: str = "my-daemon-master") -> Path:
    """A GitHub-shaped archive: everything under one generated top-level dir."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{top}/setup.py", "# setup\n")
        zf.writestr(f"{top}/src/my_daemon/__init__.py", "")
        zf.writestr(f"{top}/config.example.yaml", "vault:\n  path: .\n")
    return path


def test_extracting_strips_githubs_generated_top_level_directory(tmp_path: Path):
    """Without this the app lands at app\\my-daemon-master\\setup.py."""
    archive = _make_zip(tmp_path / "src.zip")
    app = tmp_path / "app"

    extract_app(archive, app)

    assert (app / "setup.py").is_file()
    assert (app / "src" / "my_daemon" / "__init__.py").is_file()
    assert not (app / "my-daemon-master").exists()


def test_reinstalling_replaces_the_previous_app_directory(tmp_path: Path):
    """Update is a wholesale replace — stale files must not survive it."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "stale-from-old-version.py").write_text("gone\n", encoding="utf-8")

    extract_app(_make_zip(tmp_path / "src.zip"), app)

    assert (app / "setup.py").is_file()
    assert not (app / "stale-from-old-version.py").exists()


def test_a_corrupt_archive_leaves_the_existing_install_untouched(tmp_path: Path):
    """A failed update must not destroy a working install."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "setup.py").write_text("# the working version\n", encoding="utf-8")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"this is not a zip file")

    with pytest.raises(zipfile.BadZipFile):
        extract_app(corrupt, app)

    assert (app / "setup.py").read_text(encoding="utf-8") == "# the working version\n"


def test_the_source_ref_renders_without_leaking_surprises():
    """The installer prints this before downloading — it must be readable."""
    ref = SourceRef(version="v0.1.0", url="https://example.invalid/a.zip", sha256=None)

    assert "v0.1.0" in ref.describe()
    assert "not verified" in ref.describe().lower()
