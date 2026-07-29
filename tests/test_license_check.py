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
    """Neither table may become a blanket amnesty.

    Deliberately not an `nvidia-*` name: those are listed exceptions now, and
    using one here would make this test pass for the wrong reason. This
    exercises the "self-describing as proprietary" fallback for a package
    nobody has ruled on.
    """

    result = license_check._classify_package("some-vendor-sdk", "1.0", "Other/Proprietary License")

    assert result.status is license_check.Status.INCOMPATIBLE


def test_overrides_are_matched_case_insensitively(license_check):
    result = license_check._classify_package("FastEmbed", "0.8.0", "Other/Proprietary License")

    assert result.status is license_check.Status.COMPATIBLE


# ---------------------------------------------------------------------------
# Accepted exceptions
#
# Distinct from a verified override, and the distinction is the point. An
# override says "the declared metadata is wrong and the licence is actually
# compatible". An exception says "the metadata is right, this really is
# incompatible, and we accept it anyway for a stated reason". Collapsing them
# would let a genuine incompatibility hide behind a word that means the
# opposite.
# ---------------------------------------------------------------------------


def test_an_accepted_exception_is_not_reported_as_incompatible(license_check):
    """The NVIDIA CUDA runtimes: proprietary, real, and not redistributed with
    this project's source."""

    result = license_check._classify_package(
        "nvidia-cublas", "13.1.1.3", "LicenseRef-NVIDIA-Proprietary"
    )

    assert result.status is license_check.Status.EXCEPTED


def test_an_exception_carries_the_reason_it_was_accepted(license_check):
    """An unexplained exception is indistinguishable from ignoring a finding,
    so the rationale travels into the report where it can be challenged."""

    result = license_check._classify_package(
        "nvidia-cublas", "13.1.1.3", "LicenseRef-NVIDIA-Proprietary"
    )

    assert "redistribut" in result.matched.lower()


def test_an_unlisted_copyleft_package_still_fails(license_check):
    """The gate has to keep biting, or wiring it into CI achieves nothing."""

    result = license_check._classify_package("leidenalg", "0.10.2", "GPLv3+")

    assert result.status is license_check.Status.INCOMPATIBLE


def test_py_rust_stemmers_is_recognised_as_mit(license_check):
    """No licence metadata at all, but the wheel ships the MIT text."""

    result = license_check._classify_package("py_rust_stemmers", "0.1.5", "UNKNOWN")

    assert result.status is license_check.Status.COMPATIBLE


# ---------------------------------------------------------------------------
# Exit codes — what CI actually keys on
# ---------------------------------------------------------------------------


def _exit_code(license_check, statuses, *, strict=False):
    results = [
        license_check.PkgResult(f"pkg{i}", "1.0", "raw", [], status, "")
        for i, status in enumerate(statuses)
    ]
    return license_check.exit_code_for(results, strict=strict)


def test_a_clean_tree_exits_zero(license_check):
    assert _exit_code(license_check, [license_check.Status.COMPATIBLE]) == 0


def test_exceptions_alone_do_not_fail_the_build(license_check):
    """Otherwise CI is permanently red and everyone learns to ignore it."""

    assert (
        _exit_code(license_check, [license_check.Status.COMPATIBLE, license_check.Status.EXCEPTED])
        == 0
    )


def test_one_real_incompatibility_fails_the_build(license_check):
    assert (
        _exit_code(
            license_check, [license_check.Status.EXCEPTED, license_check.Status.INCOMPATIBLE]
        )
        == 1
    )


def test_an_unknown_licence_fails_only_under_strict(license_check):
    """A dependency nobody has classified is exactly what CI should stop on,
    but it stays opt-in so a local run is not blocked by it."""

    assert _exit_code(license_check, [license_check.Status.UNKNOWN]) == 0
    assert _exit_code(license_check, [license_check.Status.UNKNOWN], strict=True) == 2


def test_incompatible_outranks_unknown_under_strict(license_check):
    assert (
        _exit_code(
            license_check,
            [license_check.Status.UNKNOWN, license_check.Status.INCOMPATIBLE],
            strict=True,
        )
        == 1
    )


# ---------------------------------------------------------------------------
# The report must not hide what it excepted
# ---------------------------------------------------------------------------


def _results(license_check, spec):
    """spec: [(name, Status), ...]"""
    return [
        license_check.PkgResult(name, "1.0", "raw", [], status, "reason") for name, status in spec
    ]


def test_the_summary_counts_exceptions_separately(license_check, capsys):
    """ "0 incompatible" while silently omitting seventeen proprietary packages
    would be a clean bill of health that is not true."""

    license_check._report(
        _results(
            license_check,
            [("ok", license_check.Status.COMPATIBLE), ("cuda", license_check.Status.EXCEPTED)],
        )
    )

    summary = capsys.readouterr().out

    assert "1 excepted" in summary


def test_excepted_packages_are_listed_by_name(license_check, capsys):
    """A count alone cannot be reviewed; the names and the reason can."""

    license_check._report(
        _results(license_check, [("cuda-bindings", license_check.Status.EXCEPTED)])
    )

    out = capsys.readouterr().out

    assert "cuda-bindings" in out
    assert "reason" in out


def test_every_package_is_accounted_for_in_the_summary(license_check, capsys):
    """The four buckets must sum to the total, or something is being dropped."""

    license_check._report(
        _results(
            license_check,
            [
                ("a", license_check.Status.COMPATIBLE),
                ("b", license_check.Status.INCOMPATIBLE),
                ("c", license_check.Status.UNKNOWN),
                ("d", license_check.Status.EXCEPTED),
            ],
        )
    )

    out = capsys.readouterr().out

    assert "1 compatible" in out
    assert "1 incompatible" in out
    assert "1 unknown" in out
    assert "1 excepted" in out
    assert "(4 total)" in out


def _payload(license_check, spec, *, exit_code=0):
    return license_check._build_payload(_results(license_check, spec), exit_code)


def test_the_markdown_does_not_claim_all_clear_over_an_exception(license_check):
    """The worst possible output: "No action required" printed above seventeen
    proprietary dependencies the reader was never shown."""

    md = license_check._render_markdown(
        _payload(
            license_check,
            [("ok", license_check.Status.COMPATIBLE), ("cuda", license_check.Status.EXCEPTED)],
        )
    )

    assert "No action required" not in md
    assert "cuda" in md


def test_the_markdown_still_says_all_clear_when_it_really_is(license_check):
    md = license_check._render_markdown(
        _payload(license_check, [("ok", license_check.Status.COMPATIBLE)])
    )

    assert "No action required" in md


def test_the_markdown_counts_exceptions(license_check):
    md = license_check._render_markdown(
        _payload(license_check, [("cuda", license_check.Status.EXCEPTED)])
    )

    assert "1 excepted" in md
