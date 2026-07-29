# SPDX-License-Identifier: Apache-2.0
"""Locating `pip-licenses` — the reason CLAUDE.md rule 10 was unenforceable.

The tool was resolved with `shutil.which`, which only searches `PATH`. Running
the checker the way everything else in this repo is run — `.venv/bin/python
scripts/license_check.py`, no activated shell — therefore reported "not
installed" even with `pip-licenses` sitting in that very venv's `bin/`. The
audit was skipped for two months on the strength of a false negative, which is
worse than having no checker at all: a checker that cannot run is indisputable,
while one that lies looks like a clean bill of health.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "license_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("license_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: the script defines dataclasses, and
    # `dataclasses.fields` resolves annotations through `sys.modules`.
    sys.modules["license_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def license_check():
    return _load()


def test_the_interpreters_own_bin_is_searched_first(license_check, tmp_path: Path, monkeypatch):
    """The case that was broken: installed beside the running python, absent
    from PATH."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = fake_bin / "pip-licenses"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("PATH", "")

    assert license_check.find_pip_licenses() == str(tool)


def test_path_is_still_honoured_for_a_global_install(license_check, tmp_path: Path, monkeypatch):
    """A system-wide install must keep working — not everyone uses a venv."""

    elsewhere = tmp_path / "usr-bin"
    elsewhere.mkdir()
    tool = elsewhere / "pip-licenses"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setenv("PATH", str(elsewhere))

    assert license_check.find_pip_licenses() == str(tool)


def test_a_genuinely_missing_tool_still_reports_missing(license_check, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setenv("PATH", "")

    assert license_check.find_pip_licenses() is None


# ---------------------------------------------------------------------------
# Verified overrides
#
# A checker that flags a core dependency as proprietary on bad metadata is not
# an enforcement mechanism, it is noise you learn to scroll past — which is how
# a real finding would get missed.
# ---------------------------------------------------------------------------


def test_a_verified_override_beats_a_wrong_classifier(license_check):
    """fastembed's PyPI Trove classifier says Other/Proprietary License. Its
    `License` field says "Apache License" and it ships the full Apache-2.0 text.
    The classifier is simply stale."""

    result = license_check._classify_package("fastembed", "0.8.0", "Other/Proprietary License")

    assert result.status is license_check.Status.COMPATIBLE


def test_an_override_records_why_it_was_applied(license_check):
    """An unexplained override is indistinguishable from suppressing a real
    finding, so the evidence travels with the verdict into the report."""

    result = license_check._classify_package("fastembed", "0.8.0", "Other/Proprietary License")

    assert "apache" in result.matched.lower()


def test_an_unlisted_proprietary_package_is_still_incompatible(license_check):
    """The override table must not become a blanket amnesty."""

    result = license_check._classify_package(
        "nvidia-cublas", "13.1.1.3", "LicenseRef-NVIDIA-Proprietary"
    )

    assert result.status is license_check.Status.INCOMPATIBLE


def test_overrides_are_matched_case_insensitively(license_check):
    result = license_check._classify_package("FastEmbed", "0.8.0", "Other/Proprietary License")

    assert result.status is license_check.Status.COMPATIBLE
