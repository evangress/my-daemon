# SPDX-License-Identifier: Apache-2.0
"""`daemon key set|clear|status` — reaching the credential store without a GUI.

`daemon setup` has been the only supported way to store the API key, and it is
a Tkinter window. A headless box — a server, an SSH session, the machine most
likely to be running `daemon run` on a schedule — therefore had no route to the
encrypted store at all, and the honest workaround was an environment variable,
which is the layer this project moved *away* from.

The security property under test throughout: the key never appears in argv.
A command-line argument is visible in `ps`, in shell history, and on Windows in
Event 4688 process-creation logs — which is exactly why `setx` was removed in
the 2026-07-27 rewrite. Re-introducing it as a CLI flag would undo that.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.secrets import KEYCHAIN_SERVICE, KEYCHAIN_USERNAME

runner = CliRunner()

SECRET = "sk-ant-api03-not-a-real-key-0123456789"


@pytest.fixture
def env_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# The key must never travel through argv
# ---------------------------------------------------------------------------


def test_the_key_cannot_be_passed_as_an_argument(memory_keyring, env_without_key):
    """argv is visible in `ps`, in shell history, and in Windows Event 4688."""

    result = runner.invoke(app, ["key", "set", SECRET])

    assert result.exit_code != 0
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) is None


def test_no_option_offers_to_take_the_key_inline(memory_keyring):
    help_text = runner.invoke(app, ["key", "set", "--help"]).output

    assert "--key" not in help_text


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def test_a_prompted_key_reaches_the_credential_store(memory_keyring, env_without_key):
    result = runner.invoke(app, ["key", "set"], input=f"{SECRET}\n")

    assert result.exit_code == 0, result.output
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) == SECRET


def test_the_key_is_never_echoed_back(memory_keyring, env_without_key):
    """Terminal scrollback and CI logs both outlive the command."""

    result = runner.invoke(app, ["key", "set"], input=f"{SECRET}\n")

    assert SECRET not in result.output


def test_stdin_mode_works_for_a_pipe(memory_keyring, env_without_key):
    """`pass show anthropic | daemon key set --stdin` over SSH."""

    result = runner.invoke(app, ["key", "set", "--stdin"], input=f"{SECRET}\n")

    assert result.exit_code == 0, result.output
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) == SECRET


def test_an_empty_key_is_refused(memory_keyring, env_without_key):
    result = runner.invoke(app, ["key", "set", "--stdin"], input="   \n")

    assert result.exit_code != 0
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) is None


def test_an_unexpected_key_shape_warns_but_is_accepted(memory_keyring, env_without_key):
    """A hard format check would be a liability — key prefixes are Anthropic's
    to change, and refusing a valid key is worse than accepting an invalid one
    the API will reject in a second."""

    result = runner.invoke(app, ["key", "set", "--stdin"], input="probably-wrong\n")

    assert result.exit_code == 0, result.output
    assert "doesn't look like" in result.output
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) == "probably-wrong"


def test_an_environment_variable_that_would_shadow_the_store_is_called_out(
    memory_keyring, monkeypatch
):
    """The footgun this command could otherwise create: the write succeeds, and
    the daemon goes on using a *different* key, because the environment wins."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-something-else")

    result = runner.invoke(app, ["key", "set", "--stdin"], input=f"{SECRET}\n")

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "shadow" in result.output.lower() or "takes precedence" in result.output.lower()


def test_a_store_failure_is_reported_rather_than_silently_swallowed(no_keyring, env_without_key):
    """No usable backend must fail loudly — believing a key was saved when it
    was not is how someone ends up with a broken scheduled job."""

    result = runner.invoke(app, ["key", "set", "--stdin"], input=f"{SECRET}\n")

    assert result.exit_code != 0
    assert "credential store" in result.output.lower()


# ---------------------------------------------------------------------------
# Migrating off plaintext
# ---------------------------------------------------------------------------


def test_a_legacy_plaintext_key_is_stripped_after_a_successful_store(
    memory_keyring, env_without_key, tmp_path: Path, monkeypatch
):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"ANTHROPIC_API_KEY={SECRET}\nOTHER=keep-me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["key", "set", "--stdin"], input=f"{SECRET}\n")

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" not in dotenv.read_text(encoding="utf-8")
    assert "OTHER=keep-me" in dotenv.read_text(encoding="utf-8")


def test_the_plaintext_file_is_left_alone_when_the_store_fails(
    no_keyring, env_without_key, tmp_path: Path, monkeypatch
):
    """Purging the only surviving copy of the key after failing to save it
    elsewhere would lose the user their key outright."""

    dotenv = tmp_path / ".env"
    dotenv.write_text(f"ANTHROPIC_API_KEY={SECRET}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["key", "set", "--stdin"], input=f"{SECRET}\n")

    assert SECRET in dotenv.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_removes_the_stored_key(memory_keyring, env_without_key):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, SECRET)

    result = runner.invoke(app, ["key", "clear"], input="y\n")

    assert result.exit_code == 0, result.output
    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) is None


def test_clear_is_a_no_op_when_nothing_is_stored(memory_keyring, env_without_key):
    result = runner.invoke(app, ["key", "clear"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "nothing" in result.output.lower()


def test_clear_asks_before_deleting(memory_keyring, env_without_key):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, SECRET)

    runner.invoke(app, ["key", "clear"], input="n\n")

    assert memory_keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME) == SECRET


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_names_the_layer_that_answers(memory_keyring, env_without_key):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, SECRET)

    result = runner.invoke(app, ["key", "status"])

    assert result.exit_code == 0, result.output
    assert "credential store" in result.output.lower()


def test_status_never_prints_the_key(memory_keyring, env_without_key):
    memory_keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, SECRET)

    result = runner.invoke(app, ["key", "status"])

    assert SECRET not in result.output


def test_status_says_so_when_there_is_no_key_anywhere(memory_keyring, env_without_key):
    result = runner.invoke(app, ["key", "status"])

    assert result.exit_code == 0, result.output
    assert "no key" in result.output.lower()
