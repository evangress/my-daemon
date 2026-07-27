# SPDX-License-Identifier: Apache-2.0
"""`daemon setup` — how the API key gets persisted.

The setup window used to call `dotenv.set_key()` on every platform, so the
secret landed in `.env` in plaintext even on Windows, where `setx` had already
stored it. Two invariants replace that and are pinned here:

* the key goes to the **OS credential store** and nowhere else;
* an existing plaintext `.env` is **migrated and stripped**, not left behind —
  an upgrade that leaves the old copy on disk has not fixed anything.

The window itself is never constructed; `_persist_api_key` is the whole unit
under test, which is why it was worth pulling out of the save handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from my_daemon.gui import setup as setup_window
from my_daemon.secrets import KEYCHAIN_SERVICE, KEYCHAIN_USERNAME

KEY = "sk-ant-api03-secret-value"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the setup window at a throwaway project directory."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(setup_window, "config_path", lambda: root / "config.yaml")
    return root


def test_the_key_goes_into_the_os_credential_store(project: Path, memory_keyring):
    ok, _ = setup_window._persist_api_key(KEY)

    assert ok is True
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) == KEY


def test_saving_never_creates_a_plaintext_dotenv(project: Path, memory_keyring):
    """The regression this whole change exists to prevent."""
    setup_window._persist_api_key(KEY)

    assert not (project / ".env").exists()


def test_an_existing_plaintext_key_is_migrated_out_of_the_dotenv(project: Path, memory_keyring):
    (project / ".env").write_text(
        f"ANTHROPIC_API_KEY={KEY}\nMY_DAEMON_LLM__MODEL=claude-sonnet-4-6\n",
        encoding="utf-8",
    )

    ok, message = setup_window._persist_api_key(KEY)

    remaining = (project / ".env").read_text(encoding="utf-8")
    assert ok is True
    assert KEY not in remaining
    assert "MY_DAEMON_LLM__MODEL=claude-sonnet-4-6" in remaining
    assert "rotate" in message.lower()


def test_a_dotenv_without_the_key_is_left_alone(project: Path, memory_keyring):
    (project / ".env").write_text("MY_DAEMON_LLM__MODEL=claude-sonnet-4-6\n", encoding="utf-8")

    _, message = setup_window._persist_api_key(KEY)

    assert (project / ".env").read_text(encoding="utf-8") == (
        "MY_DAEMON_LLM__MODEL=claude-sonnet-4-6\n"
    )
    assert "rotate" not in message.lower()


def test_the_current_process_sees_the_key_without_a_restart(
    project: Path, memory_keyring, monkeypatch: pytest.MonkeyPatch
):
    """`daemon chat` is launched straight from this window."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    setup_window._persist_api_key(KEY)

    import os

    assert os.environ["ANTHROPIC_API_KEY"] == KEY


def test_a_machine_without_a_credential_store_fails_loudly_and_writes_nothing(
    project: Path, no_keyring
):
    """Failing closed matters: silently falling back to .env is the old bug."""
    ok, message = setup_window._persist_api_key(KEY)

    assert ok is False
    assert not (project / ".env").exists()
    assert KEY not in message


def test_no_status_message_ever_contains_the_key(project: Path, memory_keyring):
    _, message = setup_window._persist_api_key(KEY)

    assert KEY not in message
