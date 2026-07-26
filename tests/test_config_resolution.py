# SPDX-License-Identifier: Apache-2.0
"""Where the config comes from, and what its relative paths mean.

Two bugs live here, and they are the same bug seen from two ends:

* `_default_config_path()` was `Path.cwd()/config.yaml` and a miss silently
  became defaults — `daemon migrate status` from an empty directory exited 0
  against a vault that did not exist.
* every state path (`./data/...`) resolved against the CWD, so the two
  documented schedulers (cron, which starts in `$HOME`; Windows Task Scheduler,
  which starts in `system32`) each read a different, empty daemon and reported
  success.

The fix is a search order plus an anchoring rule, so both tests below are
written to fail loudly if either half regresses. Every test runs with the CWD
somewhere that deliberately has no config, so nothing can pass by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.config import ConfigNotFoundError, load_settings
from my_daemon.paths import config_search_paths, find_config, user_config_dir

runner = CliRunner()

CONFIG_TEXT = """\
vault:
  path: /srv/notes
graph:
  path: ./data/graph.gpickle
  manifest_path: ./data/manifest.json
feedback:
  db_path: ./data/feedback.db
snapshot:
  dir: ./data/snapshots
consolidation:
  out_dir: ./data/consolidation
embeddings:
  cache_folder: ./data/models
"""


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited config, no inherited home, no inherited CWD.

    Without this the developer's real `~/.config/my-daemon` (or the repo's own
    `config.yaml`) would satisfy the search and every assertion below would be
    meaningless.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("MY_DAEMON_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)


def _write_config(directory: Path, text: str = CONFIG_TEXT) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# search order
# ---------------------------------------------------------------------------


def test_search_order_is_cwd_then_user_config_dir(tmp_path: Path):
    assert config_search_paths() == [
        tmp_path / "elsewhere" / "config.yaml",
        tmp_path / "home" / ".config" / "my-daemon" / "config.yaml",
    ]


def test_an_explicit_path_is_the_only_candidate(tmp_path: Path):
    """A typo in --config must fail, not silently resolve to some other file."""
    assert config_search_paths(tmp_path / "typo.yaml") == [tmp_path / "typo.yaml"]


def test_env_var_is_the_only_candidate_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MY_DAEMON_CONFIG", str(tmp_path / "chosen.yaml"))

    assert config_search_paths() == [tmp_path / "chosen.yaml"]


def test_cwd_config_wins_over_the_user_config_dir(tmp_path: Path):
    cwd_config = _write_config(tmp_path / "elsewhere")
    _write_config(user_config_dir())

    assert find_config() == cwd_config
    assert load_settings().config_path == cwd_config


def test_user_config_dir_is_used_when_cwd_has_none(tmp_path: Path):
    user_config = _write_config(user_config_dir())

    assert find_config() == user_config
    assert load_settings().config_path == user_config


def test_user_config_dir_follows_xdg_config_home(tmp_path: Path):
    assert user_config_dir() == tmp_path / "home" / ".config" / "my-daemon"


def test_explicit_path_beats_both_locations(tmp_path: Path):
    _write_config(tmp_path / "elsewhere")
    _write_config(user_config_dir())
    chosen = _write_config(tmp_path / "chosen", "vault:\n  path: /srv/chosen\n")

    settings = load_settings(chosen)

    assert settings.config_path == chosen
    assert settings.vault.path == Path("/srv/chosen")


def test_env_var_selects_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    chosen = _write_config(tmp_path / "viaenv", "vault:\n  path: /srv/viaenv\n")
    _write_config(tmp_path / "elsewhere")
    monkeypatch.setenv("MY_DAEMON_CONFIG", str(chosen))

    settings = load_settings()

    assert settings.config_path == chosen
    assert settings.vault.path == Path("/srv/viaenv")


# ---------------------------------------------------------------------------
# loud failure
# ---------------------------------------------------------------------------


def test_load_settings_raises_when_nothing_is_found(tmp_path: Path):
    with pytest.raises(ConfigNotFoundError) as excinfo:
        load_settings()

    assert excinfo.value.searched == [
        tmp_path / "elsewhere" / "config.yaml",
        tmp_path / "home" / ".config" / "my-daemon" / "config.yaml",
    ]


def test_load_settings_raises_for_a_named_file_that_is_absent(tmp_path: Path):
    with pytest.raises(ConfigNotFoundError) as excinfo:
        load_settings(tmp_path / "typo.yaml")

    assert excinfo.value.searched == [tmp_path / "typo.yaml"]


def test_config_not_found_is_still_a_file_not_found_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_settings()


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_state_touching_command_exits_1_with_the_searched_locations(tmp_path: Path):
    """`daemon migrate status` used to exit 0 here, against a phantom vault."""
    result = runner.invoke(app, ["migrate", "status"])

    assert result.exit_code == 1
    output = _squash(result.output)
    assert "no config.yaml found" in output
    assert "daemon init" in output
    assert str(tmp_path / "elsewhere" / "config.yaml") in output
    assert str(tmp_path / "home" / ".config" / "my-daemon" / "config.yaml") in output


def test_status_exits_1_without_a_config():
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert "no config.yaml found" in _squash(result.output)


def test_version_works_without_a_config():
    """Pure commands must not need state."""
    from my_daemon import __version__

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_the_config_option_selects_a_config_for_a_command(tmp_path: Path):
    _write_config(tmp_path / "anchored")

    result = runner.invoke(
        app, ["--config", str(tmp_path / "anchored" / "config.yaml"), "migrate", "status"]
    )

    assert result.exit_code == 0, result.output


def test_the_config_option_reports_a_missing_file(tmp_path: Path):
    result = runner.invoke(app, ["--config", str(tmp_path / "typo.yaml"), "status"])

    assert result.exit_code == 1
    assert str(tmp_path / "typo.yaml") in _squash(result.output)


# ---------------------------------------------------------------------------
# anchoring
# ---------------------------------------------------------------------------


def test_relative_state_paths_anchor_to_the_config_dir_not_the_cwd(tmp_path: Path):
    """Config in directory A, process in directory B — state belongs to A."""
    a = tmp_path / "project"
    config = _write_config(a)

    settings = load_settings(config)

    assert settings.graph.path == a / "data" / "graph.gpickle"
    assert settings.graph.manifest_path == a / "data" / "manifest.json"
    assert settings.feedback.db_path == a / "data" / "feedback.db"
    assert settings.snapshot.dir == a / "data" / "snapshots"
    assert settings.consolidation.out_dir == a / "data" / "consolidation"
    assert settings.embeddings.cache_folder == a / "data" / "models"


def test_anchoring_holds_for_the_user_config_dir(tmp_path: Path):
    _write_config(user_config_dir())

    settings = load_settings()

    assert settings.graph.path == user_config_dir() / "data" / "graph.gpickle"


def test_absolute_paths_pass_through_untouched(tmp_path: Path):
    config = _write_config(
        tmp_path / "project",
        "vault:\n  path: /srv/notes\ngraph:\n  path: /var/lib/my-daemon/graph.gpickle\n",
    )

    settings = load_settings(config)

    assert settings.graph.path == Path("/var/lib/my-daemon/graph.gpickle")
    assert settings.vault.path == Path("/srv/notes")


def test_tilde_paths_expand_to_home_rather_than_anchoring(tmp_path: Path):
    config = _write_config(
        tmp_path / "project",
        "vault:\n  path: ~/Vault\ngraph:\n  path: ~/state/graph.gpickle\n",
    )

    settings = load_settings(config)

    assert settings.vault.path == tmp_path / "home" / "Vault"
    assert settings.graph.path == tmp_path / "home" / "state" / "graph.gpickle"


def test_the_embedded_qdrant_directory_is_anchored(tmp_path: Path):
    config = _write_config(
        tmp_path / "project",
        "vault:\n  path: /srv/notes\nvector_store:\n  qdrant:\n    path: ./data/qdrant\n",
    )

    settings = load_settings(config)

    assert settings.vector_store.qdrant.path == str(tmp_path / "project" / "data" / "qdrant")
    assert settings.vector_store.qdrant.is_embedded is True


def test_the_memory_sentinel_is_not_treated_as_a_path(tmp_path: Path):
    config = _write_config(
        tmp_path / "project",
        "vault:\n  path: /srv/notes\nvector_store:\n  qdrant:\n    path: ':memory:'\n",
    )

    settings = load_settings(config)

    assert settings.vector_store.qdrant.path == ":memory:"


def test_env_overrides_still_apply_and_are_anchored_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Anchoring runs on the constructed Settings, so an env-supplied relative
    path is resolved exactly like a yaml one. (Keys the yaml states explicitly
    still win over the environment — that is pydantic-settings' init-kwargs
    precedence and predates this change.)"""
    config = _write_config(tmp_path / "project", "vault:\n  path: /srv/notes\n")
    monkeypatch.setenv("MY_DAEMON_GRAPH__EXPANSION_DEPTH", "5")
    monkeypatch.setenv("MY_DAEMON_FEEDBACK__DB_PATH", "./other/state.db")

    settings = load_settings(config)

    assert settings.graph.expansion_depth == 5
    assert settings.feedback.db_path == tmp_path / "project" / "other" / "state.db"


def test_dotenv_beside_the_config_supplies_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The scheduler case: cwd has no .env, the config's directory does."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    project = tmp_path / "project"
    config = _write_config(project)
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-from-config-dir\n", encoding="utf-8")

    settings = load_settings(config)

    assert settings.anthropic_api_key == "sk-from-config-dir"


# ---------------------------------------------------------------------------
# daemon init --user
# ---------------------------------------------------------------------------


def test_init_user_writes_into_the_user_config_dir(tmp_path: Path):
    (tmp_path / "elsewhere" / "config.example.yaml").write_text(
        "vault:\n  path: ~/Documents/Obsidian/MyVault\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["init", "--user", "--vault", "/srv/notes"])

    assert result.exit_code == 0, result.output
    assert (user_config_dir() / "config.yaml").read_text(encoding="utf-8") == (
        "vault:\n  path: /srv/notes\n"
    )
    # ...and the CWD stays clean, which is the whole point of the flag.
    assert not (tmp_path / "elsewhere" / "config.yaml").exists()


def test_init_user_config_is_then_what_load_settings_finds(tmp_path: Path):
    (tmp_path / "elsewhere" / "config.example.yaml").write_text(
        "vault:\n  path: ~/Documents/Obsidian/MyVault\ngraph:\n  path: ./data/graph.gpickle\n",
        encoding="utf-8",
    )

    runner.invoke(app, ["init", "--user", "--vault", "/srv/notes"])
    settings = load_settings()

    assert settings.config_path == user_config_dir() / "config.yaml"
    assert settings.graph.path == user_config_dir() / "data" / "graph.gpickle"


def test_init_user_refuses_to_clobber(tmp_path: Path):
    (tmp_path / "elsewhere" / "config.example.yaml").write_text("vault:\n", encoding="utf-8")
    _write_config(user_config_dir(), "mine\n")

    result = runner.invoke(app, ["init", "--user"])

    assert result.exit_code == 1
    assert (user_config_dir() / "config.yaml").read_text(encoding="utf-8") == "mine\n"


def test_init_without_user_still_writes_to_the_cwd(tmp_path: Path):
    (tmp_path / "elsewhere" / "config.example.yaml").write_text(
        "vault:\n  path: ~/Documents/Obsidian/MyVault\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["init", "--vault", "/srv/notes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "elsewhere" / "config.yaml").exists()
    assert not (user_config_dir() / "config.yaml").exists()


# ---------------------------------------------------------------------------
# the scheduler fix
# ---------------------------------------------------------------------------


def test_the_windows_reflect_task_names_its_config_absolutely(tmp_path: Path):
    """A scheduled task starts in system32; the command must not depend on CWD."""
    from my_daemon.gui.setup import reflect_task_command

    exe = tmp_path / "project" / ".venv" / "Scripts" / "daemon.exe"
    cfg = tmp_path / "project" / "config.yaml"

    assert reflect_task_command(exe, cfg) == f'"{exe}" --config "{cfg}" reflect'


def test_the_setup_window_edits_the_config_the_daemon_reads(tmp_path: Path):
    from my_daemon.gui import setup as setup_gui

    user_config = _write_config(user_config_dir())

    assert setup_gui.config_path() == user_config
    assert setup_gui.project_root() == user_config_dir()


def test_the_setup_window_falls_back_to_the_cwd_when_nothing_exists(tmp_path: Path):
    from my_daemon.gui import setup as setup_gui

    assert setup_gui.config_path() == tmp_path / "elsewhere" / "config.yaml"
