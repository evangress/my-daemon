# SPDX-License-Identifier: Apache-2.0
"""`daemon schedule` — the generated systemd / Task Scheduler units.

These are exact-match tests on purpose. A scheduler unit is write-once,
read-never: nobody re-reads `my-daemon.service` after `systemctl --user enable`,
so a silently wrong `ExecStart` is a daemon that has "been running" for a month
against the wrong config. The whole point of generating the unit is that the
paths are not typed by hand, which is only worth anything if the substitution
is pinned down.
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.config import Settings
from my_daemon.supervisor import (
    SYSTEMD_UNIT_NAME,
    WINDOWS_TASK_NAME,
    UnsupportedPlatform,
    build_schedule_unit,
    daemon_executable,
    schtasks_command,
    systemd_unit,
    systemd_unit_path,
    task_command,
)

runner = CliRunner()

EXE = Path("/srv/my-daemon/.venv/bin/daemon")
CFG = Path("/srv/my-daemon/config.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = tmp_path / "vault"
    s.config_path = CFG
    return s


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------


def test_the_systemd_unit_is_exactly_this():
    assert systemd_unit(EXE, CFG) == (
        "[Unit]\n"
        "Description=My Daemon — vault watcher and nightly consolidation\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        'ExecStart="/srv/my-daemon/.venv/bin/daemon" '
        '--config "/srv/my-daemon/config.yaml" run\n'
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def test_the_unit_never_depends_on_a_working_directory():
    """No WorkingDirectory=, and the config named absolutely — the same rule
    the schtasks helper follows, for the same reason."""
    text = systemd_unit(EXE, CFG)

    assert "WorkingDirectory" not in text
    assert f'--config "{CFG}"' in text


def test_the_unit_has_no_companion_timer():
    """`daemon run` owns its own schedule, so a .timer would double-fire."""
    assert "OnCalendar" not in systemd_unit(EXE, CFG)


def test_the_unit_path_follows_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert systemd_unit_path() == tmp_path / "cfg" / "systemd" / "user" / "my-daemon.service"


def test_the_unit_path_defaults_to_dot_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert systemd_unit_path() == tmp_path / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def test_the_schtasks_command_is_exactly_this():
    exe = Path(r"C:\my-daemon\.venv\Scripts\daemon.exe")
    cfg = Path(r"C:\my-daemon\config.yaml")

    assert schtasks_command(exe, cfg) == (
        'schtasks /Create /SC ONLOGON /TN "MyDaemonRun" '
        '/TR "\\"C:\\my-daemon\\.venv\\Scripts\\daemon.exe\\" '
        '--config \\"C:\\my-daemon\\config.yaml\\" run" /F'
    )


def test_the_task_command_shape_matches_the_setup_guis_helper():
    """One shape for Windows scheduled tasks, not two.

    `gui/setup.py` already registers `daemon reflect` this way; feeding it the
    same subcommand must produce a byte-identical `/TR` string, so the two
    surfaces cannot drift into disagreeing about quoting.
    """
    from my_daemon.gui.setup import reflect_task_command

    assert task_command(EXE, CFG, "reflect") == reflect_task_command(EXE, CFG)


def test_the_windows_task_name_is_distinct_from_the_reflect_task():
    from my_daemon.gui.setup import REFLECT_TASK_NAME

    assert WINDOWS_TASK_NAME != REFLECT_TASK_NAME


# ---------------------------------------------------------------------------
# the executable
# ---------------------------------------------------------------------------


def test_the_daemon_executable_is_the_one_in_this_environment():
    """Never `shutil.which`: a scheduler has no PATH worth trusting, and the
    daemon that should run is the one in *this* venv."""
    expected = "daemon.exe" if sys.platform == "win32" else "daemon"

    assert daemon_executable() == Path(sysconfig.get_path("scripts")) / expected


def test_the_daemon_executable_is_not_derived_from_a_resolved_interpreter():
    """Regression: a venv's `bin/python` is a *symlink to the system
    interpreter*, so `Path(sys.executable).resolve().with_name("daemon")`
    yields `/usr/bin/daemon` — a path that does not exist. The generated unit
    installed cleanly and would have failed at every boot."""
    resolved_sibling = (
        Path(sys.executable)
        .resolve()
        .with_name("daemon.exe" if sys.platform == "win32" else "daemon")
    )

    assert daemon_executable().is_file()
    if resolved_sibling != daemon_executable():
        assert not resolved_sibling.exists()


def test_a_unit_naming_a_missing_binary_reports_itself(settings: Settings):
    unit = build_schedule_unit(settings, platform="linux", exe=Path("/nowhere/daemon"))

    assert unit.executable == Path("/nowhere/daemon")
    assert unit.executable_missing is True


def test_a_unit_naming_a_real_binary_does_not(settings: Settings, tmp_path: Path):
    real = tmp_path / "daemon"
    real.write_text("#!/bin/sh\n", encoding="utf-8")

    unit = build_schedule_unit(settings, platform="linux", exe=real)

    assert unit.executable_missing is False


# ---------------------------------------------------------------------------
# build_schedule_unit
# ---------------------------------------------------------------------------


def test_linux_produces_a_systemd_unit(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    unit = build_schedule_unit(settings, platform="linux", exe=EXE)

    assert unit.kind == "systemd-user"
    assert unit.target == tmp_path / "cfg" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    assert unit.text == systemd_unit(EXE, CFG)
    assert "systemctl --user enable --now my-daemon.service" in unit.instructions


def test_windows_produces_a_schtasks_command_and_writes_no_file(settings: Settings):
    unit = build_schedule_unit(settings, platform="win32", exe=EXE)

    assert unit.kind == "schtasks"
    assert unit.target is None
    assert unit.text == schtasks_command(EXE, CFG)


def test_an_unsupported_platform_says_so_and_points_at_run_once(settings: Settings):
    with pytest.raises(UnsupportedPlatform, match="daemon run --once"):
        build_schedule_unit(settings, platform="darwin", exe=EXE)


def test_a_config_that_was_never_resolved_from_a_file_is_refused(settings: Settings):
    """Every generated unit names `--config <path>`. There is no honest unit to
    generate for a Settings that came from nowhere."""
    settings.config_path = None

    with pytest.raises(ValueError, match="no config file"):
        build_schedule_unit(settings, platform="linux", exe=EXE)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def linux_cli(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    monkeypatch.setattr("my_daemon.supervisor.current_platform", lambda: "linux")
    monkeypatch.setattr("my_daemon.supervisor.daemon_executable", lambda: EXE)
    return tmp_path / "cfg" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def test_schedule_show_prints_the_unit_and_writes_nothing(linux_cli: Path):
    result = runner.invoke(app, ["schedule", "show"])

    assert result.exit_code == 0, result.output
    assert 'ExecStart="/srv/my-daemon/.venv/bin/daemon"' in result.output
    assert not linux_cli.exists()


def test_schedule_install_writes_the_unit_and_prints_the_next_commands(linux_cli: Path):
    result = runner.invoke(app, ["schedule", "install"])

    assert result.exit_code == 0, result.output
    assert linux_cli.read_text(encoding="utf-8") == systemd_unit(EXE, CFG)
    assert "systemctl --user daemon-reload" in result.output


def test_schedule_install_dry_run_writes_nothing(linux_cli: Path):
    result = runner.invoke(app, ["schedule", "install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not linux_cli.exists()
    assert "dry-run" in result.output.lower()


def test_schedule_install_never_runs_systemctl_itself(
    linux_cli: Path, monkeypatch: pytest.MonkeyPatch
):
    """Enabling a unit is the user's decision, made with their eyes open."""
    import subprocess

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("daemon schedule must not shell out")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    result = runner.invoke(app, ["schedule", "install"])

    assert result.exit_code == 0, result.output


def test_schedule_show_on_windows_prints_the_schtasks_command(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    monkeypatch.setattr("my_daemon.supervisor.current_platform", lambda: "win32")
    monkeypatch.setattr("my_daemon.supervisor.daemon_executable", lambda: EXE)

    result = runner.invoke(app, ["schedule", "show"])

    assert result.exit_code == 0, result.output
    assert "schtasks /Create /SC ONLOGON" in _squash(result.output)


def test_schedule_on_an_unsupported_platform_exits_one(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    monkeypatch.setattr("my_daemon.supervisor.current_platform", lambda: "darwin")
    monkeypatch.setattr("my_daemon.supervisor.daemon_executable", lambda: EXE)

    result = runner.invoke(app, ["schedule", "show"])

    assert result.exit_code == 1
    assert "daemon run --once" in _squash(result.output)


_BOX_CHARS = "┏┓┗┛━─│┃┡┩┠┨╇┼├┤┬┴╭╮╰╯╺╸"


def _squash(text: str) -> str:
    """Rich pads with runs of spaces and draws boxes; normalise for matching."""
    stripped = text.translate({ord(c): " " for c in _BOX_CHARS})
    return " ".join(stripped.split())
