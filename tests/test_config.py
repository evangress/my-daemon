# SPDX-License-Identifier: Apache-2.0
"""Tests for configuration layer."""

from datetime import date

from my_daemon.config import Settings


def test_date_config_defaults_are_conservative():
    s = Settings()
    assert s.vault.date_frontmatter_keys == ["occurred_at", "date", "created", "created_at"]
    assert s.vault.mtime_trusted_before is None, "mtime fallback must be off by default"


def test_mtime_trusted_before_parses_an_iso_date():
    s = Settings.model_validate({"vault": {"mtime_trusted_before": "2026-06-21"}})
    assert s.vault.mtime_trusted_before == date(2026, 6, 21)
