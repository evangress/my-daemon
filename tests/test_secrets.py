# SPDX-License-Identifier: Apache-2.0
"""Where the Anthropic API key comes from, and which layers are plaintext.

The daemon used to read the key from exactly one place — `os.environ` — and
`daemon setup` wrote it to `.env` in plaintext on every platform, including
Windows where `setx` had already stored it properly. Two things follow from
that and are pinned here:

* **Precedence.** A real environment variable beats the OS keychain, which
  beats a legacy `.env`. The env var stays on top so CI and `MY_DAEMON_*`-style
  overrides keep working without touching a keychain.
* **Provenance.** The resolver reports *which* layer answered, because only
  `.env` is plaintext-on-disk and only that case should nag the user. That is
  why `.env` is read directly rather than through `load_dotenv` — once it has
  been merged into `os.environ`, a plaintext key is indistinguishable from a
  properly-stored one.

The keychain is exercised through a real in-memory `KeyringBackend`, not a
mock, so these tests run keyring's actual dispatch without ever touching the
developer's Secret Service / Keychain / Credential Manager.
"""

from __future__ import annotations

from pathlib import Path

from my_daemon.secrets import (
    KEYCHAIN_SERVICE,
    KEYCHAIN_USERNAME,
    delete_from_keychain,
    purge_dotenv_key,
    read_keychain,
    resolve_api_key,
    store_in_keychain,
)


def _write_dotenv(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_a_real_environment_variable_is_used_and_reported_as_such(memory_keyring):
    resolved = resolve_api_key(env_value="sk-ant-from-env")

    assert resolved.value == "sk-ant-from-env"
    assert resolved.source == "environment"


def test_the_keychain_supplies_the_key_when_the_environment_has_none(memory_keyring):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, "sk-ant-from-keychain")

    resolved = resolve_api_key(env_value=None)

    assert resolved.value == "sk-ant-from-keychain"
    assert resolved.source == "keychain"


def test_the_environment_beats_the_keychain(memory_keyring):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, "sk-ant-from-keychain")

    resolved = resolve_api_key(env_value="sk-ant-from-env")

    assert resolved.value == "sk-ant-from-env"
    assert resolved.source == "environment"


def test_a_legacy_dotenv_still_supplies_the_key(tmp_path: Path, memory_keyring):
    env_file = _write_dotenv(tmp_path, "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")

    resolved = resolve_api_key(env_value=None, dotenv_paths=[env_file])

    assert resolved.value == "sk-ant-from-dotenv"
    assert resolved.source == "dotenv"


def test_the_keychain_beats_a_legacy_dotenv(tmp_path: Path, memory_keyring):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, "sk-ant-from-keychain")
    env_file = _write_dotenv(tmp_path, "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")

    resolved = resolve_api_key(env_value=None, dotenv_paths=[env_file])

    assert resolved.value == "sk-ant-from-keychain"
    assert resolved.source == "keychain"


def test_the_first_dotenv_that_has_the_key_wins(tmp_path: Path, memory_keyring):
    first = _write_dotenv(tmp_path / "beside-config", "ANTHROPIC_API_KEY=sk-ant-first\n")
    second = _write_dotenv(tmp_path / "cwd", "ANTHROPIC_API_KEY=sk-ant-second\n")

    resolved = resolve_api_key(env_value=None, dotenv_paths=[first, second])

    assert resolved.value == "sk-ant-first"
    assert resolved.dotenv_path == first


# ---------------------------------------------------------------------------
# absence and degradation
# ---------------------------------------------------------------------------


def test_no_key_anywhere_reports_missing_rather_than_raising(memory_keyring):
    resolved = resolve_api_key(env_value=None)

    assert resolved.value is None
    assert resolved.source == "missing"


def test_a_blank_environment_variable_counts_as_absent(memory_keyring):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, "sk-ant-from-keychain")

    resolved = resolve_api_key(env_value="   ")

    assert resolved.value == "sk-ant-from-keychain"
    assert resolved.source == "keychain"


def test_a_machine_without_a_credential_store_falls_through_to_dotenv(tmp_path: Path, no_keyring):
    """Headless Linux and cron have no Secret Service — that must not crash."""
    env_file = _write_dotenv(tmp_path, "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")

    resolved = resolve_api_key(env_value=None, dotenv_paths=[env_file])

    assert resolved.value == "sk-ant-from-dotenv"
    assert resolved.source == "dotenv"


def test_a_missing_dotenv_file_is_simply_skipped(tmp_path: Path, memory_keyring):
    resolved = resolve_api_key(env_value=None, dotenv_paths=[tmp_path / "absent" / ".env"])

    assert resolved.source == "missing"


def test_only_the_dotenv_layer_is_flagged_as_plaintext(tmp_path: Path, memory_keyring):
    env_file = _write_dotenv(tmp_path, "ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    from_dotenv = resolve_api_key(env_value=None, dotenv_paths=[env_file])

    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, "sk-ant-safe")
    from_keychain = resolve_api_key(env_value=None)

    assert from_dotenv.is_plaintext is True
    assert from_keychain.is_plaintext is False


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def test_a_stored_key_round_trips_through_the_keychain(memory_keyring):
    store_in_keychain("sk-ant-stored")

    assert read_keychain() == "sk-ant-stored"


def test_storing_reports_failure_instead_of_raising_when_there_is_no_backend(no_keyring):
    ok, message = store_in_keychain("sk-ant-stored")

    assert ok is False
    assert "sk-ant-stored" not in message


def test_deleting_a_key_that_was_never_stored_is_not_an_error(memory_keyring):
    delete_from_keychain()

    assert read_keychain() is None


# ---------------------------------------------------------------------------
# purging the plaintext copy
# ---------------------------------------------------------------------------


def test_purging_removes_the_key_line_but_keeps_other_variables(tmp_path: Path):
    env_file = _write_dotenv(
        tmp_path,
        "MY_DAEMON_LLM__MODEL=claude-sonnet-4-6\nANTHROPIC_API_KEY=sk-ant-secret\nOTHER=keepme\n",
    )

    purged = purge_dotenv_key(env_file)

    remaining = env_file.read_text(encoding="utf-8")
    assert purged is True
    assert "sk-ant-secret" not in remaining
    assert "MY_DAEMON_LLM__MODEL=claude-sonnet-4-6" in remaining
    assert "OTHER=keepme" in remaining


def test_purging_a_dotenv_without_the_key_changes_nothing(tmp_path: Path):
    env_file = _write_dotenv(tmp_path, "OTHER=keepme\n")

    purged = purge_dotenv_key(env_file)

    assert purged is False
    assert env_file.read_text(encoding="utf-8") == "OTHER=keepme\n"


def test_purging_a_missing_file_is_not_an_error(tmp_path: Path):
    assert purge_dotenv_key(tmp_path / "absent" / ".env") is False
