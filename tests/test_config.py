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


def test_config_default_matches_the_vault_layers_key_list():
    """config.py deliberately does not import from the vault layer, so the
    agreement between the two default lists is enforced here instead. Without
    this, changing DEFAULT_DATE_KEYS would silently leave config.py behind."""
    from my_daemon.vault.dates import DEFAULT_DATE_KEYS

    assert Settings().vault.date_frontmatter_keys == list(DEFAULT_DATE_KEYS)
