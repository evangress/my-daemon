# SPDX-License-Identifier: Apache-2.0
"""The `daemon run` supervisor: debounce, schedule arithmetic, lifecycle.

Nothing here sleeps for real and nothing here touches a real filesystem
watcher. The three moving parts are separated on purpose so each can be driven
directly:

* :class:`Debouncer` and :func:`next_occurrence` are pure — every clock reading
  is an argument, so a whole five-second burst is exercised in microseconds.
* :class:`VaultChangeFilter` is pure string/path work.
* :class:`Supervisor` takes its resources, its watcher, its preflight and both
  of its clocks by injection, so `tick()` can be stepped by hand.

Real filesystem events are deliberately absent: they are the single flakiest
thing a test suite can depend on, and watchfiles' own tests already cover them.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_daemon.cli import app
from my_daemon.config import RunConfig, Settings
from my_daemon.doctor import FAIL, PASS, CheckResult
from my_daemon.supervisor import (
    Debouncer,
    PreflightFailed,
    Resources,
    Supervisor,
    VaultChangeFilter,
    next_occurrence,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeStats:
    """Just the three fields the supervisor's log line reads."""

    def __init__(self) -> None:
        self.notes_new_or_updated = 1
        self.notes_deleted = 0
        self.skipped_unchanged = 4


class FakeResources:
    """Stands in for the real store wiring; counts every call."""

    def __init__(
        self, *, ingest_error: Exception | None = None, consolidate_error: Exception | None = None
    ) -> None:
        self.ingest_calls = 0
        self.consolidate_calls = 0
        self.closed = 0
        self.ingest_error = ingest_error
        self.consolidate_error = consolidate_error

    def as_resources(self) -> Resources:
        return Resources(ingest=self.ingest, consolidate=self.consolidate, close=self.close)

    def ingest(self) -> FakeStats:
        self.ingest_calls += 1
        if self.ingest_error is not None:
            raise self.ingest_error
        return FakeStats()

    def consolidate(self) -> None:
        self.consolidate_calls += 1
        if self.consolidate_error is not None:
            raise self.consolidate_error

    def close(self) -> None:
        self.closed += 1


class Recorder:
    """Collects emitted lines so assertions can be exact."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    def joined(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.vault.path = tmp_path / "vault"
    s.vault.path.mkdir()
    s.graph.path = tmp_path / "graph.gpickle"
    s.feedback.db_path = tmp_path / "state.db"
    s.config_path = tmp_path / "config.yaml"
    s.run.debounce_seconds = 5.0
    s.run.heartbeat_minutes = 10.0
    return s


def _supervisor(
    settings: Settings,
    resources: FakeResources,
    emit: Recorder,
    *,
    failures: list[CheckResult] | None = None,
    **kwargs,  # noqa: ANN003
) -> Supervisor:
    return Supervisor(
        settings,
        resources_factory=resources.as_resources,
        preflight=lambda _s: list(failures or []),
        emit=emit,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Debouncer — a pure component, driven by a fake clock
# ---------------------------------------------------------------------------


def test_debouncer_is_not_ready_before_the_quiet_window_elapses():
    d = Debouncer(5.0)
    d.record(100.0)

    assert d.pending is True
    assert d.ready(104.9) is False
    assert d.seconds_remaining(104.9) == pytest.approx(0.1)


def test_debouncer_is_ready_once_the_window_elapses():
    d = Debouncer(5.0)
    d.record(100.0)

    assert d.ready(105.0) is True


def test_a_burst_of_changes_restarts_the_quiet_window():
    d = Debouncer(5.0)
    d.record(100.0)
    d.record(104.0)
    d.record(108.0)

    # Ready at 105 under the *first* event, but the 108 event pushed it out.
    assert d.ready(105.0) is False
    assert d.ready(112.9) is False
    assert d.ready(113.0) is True


def test_taking_the_burst_reports_its_size_and_clears_the_timer():
    d = Debouncer(5.0)
    d.record(100.0, count=3)
    d.record(101.0, count=2)

    assert d.pending_count == 5
    assert d.take() == 5
    assert d.pending is False
    assert d.ready(1_000.0) is False
    assert d.seconds_remaining(1_000.0) is None


def test_an_empty_debouncer_is_never_ready():
    d = Debouncer(5.0)

    assert d.pending is False
    assert d.ready(1_000.0) is False


def test_a_zero_second_debounce_is_ready_immediately():
    d = Debouncer(0.0)
    d.record(100.0)

    assert d.ready(100.0) is True


# ---------------------------------------------------------------------------
# VaultChangeFilter
# ---------------------------------------------------------------------------


@pytest.fixture
def change_filter(tmp_path: Path) -> VaultChangeFilter:
    return VaultChangeFilter(
        tmp_path / "vault",
        exclude_dirs=[".obsidian", ".trash", "templates"],
        agent_folder="Agent",
    )


def test_a_markdown_note_in_the_vault_is_accepted(change_filter, tmp_path: Path):
    assert change_filter.accepts(tmp_path / "vault" / "Inbox" / "today.md") is True


def test_a_non_markdown_file_is_ignored(change_filter, tmp_path: Path):
    assert change_filter.accepts(tmp_path / "vault" / "image.png") is False


def test_the_markdown_suffix_check_is_case_insensitive(change_filter, tmp_path: Path):
    assert change_filter.accepts(tmp_path / "vault" / "SHOUTING.MD") is True


def test_an_excluded_directory_is_ignored(change_filter, tmp_path: Path):
    assert change_filter.accepts(tmp_path / "vault" / ".obsidian" / "workspace.md") is False
    assert change_filter.accepts(tmp_path / "vault" / "Templates" / "daily.md") is False


def test_the_daemons_own_agent_folder_never_triggers_a_reingest(change_filter, tmp_path: Path):
    """The writeback target must not feed its own writes back into the watcher.

    Ingest would skip them on hash anyway, but a nightly `consolidate` writing
    a letter should not schedule an ingest at 03:00 to discover that.
    """
    assert change_filter.accepts(tmp_path / "vault" / "Agent" / "observer-2026-07-26.md") is False
    assert change_filter.accepts(tmp_path / "vault" / "Agent" / "backups" / "note.123.md") is False


def test_the_agent_folder_is_excluded_even_when_dropped_from_exclude_dirs(tmp_path: Path):
    f = VaultChangeFilter(tmp_path / "vault", exclude_dirs=[], agent_folder="Agent")

    assert f.accepts(tmp_path / "vault" / "Agent" / "memory-projects.md") is False


def test_a_path_outside_the_vault_is_ignored(change_filter, tmp_path: Path):
    assert change_filter.accepts(tmp_path / "elsewhere" / "note.md") is False


def test_select_dedupes_and_keeps_order(change_filter, tmp_path: Path):
    vault = tmp_path / "vault"
    selected = change_filter.select(
        [
            str(vault / "b.md"),
            str(vault / "a.md"),
            str(vault / "b.md"),
            str(vault / "cover.png"),
            str(vault / "Agent" / "letter.md"),
        ]
    )

    assert selected == [vault / "b.md", vault / "a.md"]


# ---------------------------------------------------------------------------
# schedule arithmetic
# ---------------------------------------------------------------------------


def test_next_occurrence_is_later_the_same_day_when_the_time_has_not_passed():
    at = RunConfig(consolidate_at="03:00").consolidate_time
    assert at is not None

    assert next_occurrence(at, datetime(2026, 7, 26, 1, 30)) == datetime(2026, 7, 26, 3, 0)


def test_next_occurrence_rolls_over_midnight_when_the_time_has_passed():
    at = RunConfig(consolidate_at="03:00").consolidate_time
    assert at is not None

    assert next_occurrence(at, datetime(2026, 7, 26, 3, 0, 1)) == datetime(2026, 7, 27, 3, 0)
    assert next_occurrence(at, datetime(2026, 7, 26, 23, 59)) == datetime(2026, 7, 27, 3, 0)


def test_next_occurrence_treats_the_exact_moment_as_already_run():
    at = RunConfig(consolidate_at="03:00").consolidate_time
    assert at is not None

    assert next_occurrence(at, datetime(2026, 7, 26, 3, 0)) == datetime(2026, 7, 27, 3, 0)


def test_next_occurrence_crosses_a_month_boundary():
    at = RunConfig(consolidate_at="03:00").consolidate_time
    assert at is not None

    assert next_occurrence(at, datetime(2026, 7, 31, 5, 0)) == datetime(2026, 8, 1, 3, 0)


def test_run_config_parses_the_configured_time():
    assert RunConfig(consolidate_at="23:45").consolidate_time == datetime(1, 1, 1, 23, 45).time()


def test_run_config_rejects_a_malformed_time():
    with pytest.raises(ValueError, match="24-hour 'HH:MM'"):
        RunConfig(consolidate_at="3am")


def test_run_config_rejects_a_negative_debounce():
    with pytest.raises(ValueError, match="debounce_seconds"):
        RunConfig(debounce_seconds=-1.0)


def test_a_null_consolidate_at_turns_the_nightly_job_off():
    assert RunConfig(consolidate_at=None).consolidate_time is None
    assert RunConfig(consolidate_at="  ").consolidate_time is None


def test_run_config_defaults_match_the_documented_ones():
    cfg = RunConfig()

    assert cfg.debounce_seconds == 5.0
    assert cfg.consolidate_at == "03:00"
    assert cfg.heartbeat_minutes == 15.0


# ---------------------------------------------------------------------------
# startup: preflight
# ---------------------------------------------------------------------------


def test_start_refuses_when_the_preflight_fails(settings: Settings):
    resources = FakeResources()
    emit = Recorder()
    failure = CheckResult("vault", FAIL, "/nope does not exist", hint="fix vault.path")
    sup = _supervisor(settings, resources, emit, failures=[failure])

    with pytest.raises(PreflightFailed) as exc:
        sup.start()

    assert exc.value.failures == [failure]
    # Nothing was built, so there is nothing to leak.
    assert resources.ingest_calls == 0
    assert resources.closed == 0


def test_start_ignores_passing_checks(settings: Settings):
    """`preflight` returns only failures — a PASS row must never refuse."""
    sup = _supervisor(
        settings,
        FakeResources(),
        Recorder(),
        failures=[],
    )
    sup.preflight = lambda _s: [r for r in [CheckResult("vault", PASS, "fine")] if r.failed]

    sup.start()  # does not raise


def test_start_builds_resources_only_after_the_preflight_passes(settings: Settings):
    """Order matters: building resources loads the embedding model and grabs
    the embedded store's lock. Doing that before a doomed preflight would make
    the refusal slow *and* hold a lock the user is being told to free."""
    order: list[str] = []
    resources = FakeResources()

    def _factory() -> Resources:
        order.append("build")
        return resources.as_resources()

    def _preflight(_s: Settings) -> list[CheckResult]:
        order.append("preflight")
        return []

    sup = Supervisor(settings, resources_factory=_factory, preflight=_preflight, emit=Recorder())
    sup.start()

    assert order == ["preflight", "build"]


# ---------------------------------------------------------------------------
# startup: banner
# ---------------------------------------------------------------------------


def test_embedded_mode_warns_that_it_holds_the_store_exclusively(settings: Settings):
    settings.vector_store.qdrant.path = Path("/srv/qdrant-local")
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)

    sup.start()

    banner = emit.joined()
    assert "/srv/qdrant-local" in banner
    assert "refuse to open it" in banner
    assert "vector_store.qdrant.url" in banner


def test_server_mode_says_nothing_about_exclusivity(settings: Settings):
    settings.vector_store.qdrant.path = None
    settings.vector_store.qdrant.url = "http://localhost:6333"
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)

    sup.start()

    assert "refuse to open it" not in emit.joined()
    assert "http://localhost:6333" in emit.joined()


def test_the_banner_names_the_vault_and_the_debounce(settings: Settings):
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)

    sup.start()

    assert str(settings.vault.path) in emit.joined()
    assert "5s" in emit.joined()


# ---------------------------------------------------------------------------
# initial ingest
# ---------------------------------------------------------------------------


def test_the_initial_ingest_runs_once(settings: Settings):
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())
    sup.start()

    sup.initial_ingest()

    assert resources.ingest_calls == 1


def test_a_failing_initial_ingest_propagates(settings: Settings):
    """Fail fast at startup: a supervisor that cannot ingest even once is not
    something to leave running unattended."""
    resources = FakeResources(ingest_error=RuntimeError("manifest is gibberish"))
    sup = _supervisor(settings, resources, Recorder())
    sup.start()

    with pytest.raises(RuntimeError, match="manifest is gibberish"):
        sup.initial_ingest()


def test_a_failing_later_ingest_is_logged_and_the_loop_survives(settings: Settings):
    """The opposite rule once running: one bad save must not kill the daemon."""
    resources = FakeResources()
    emit = Recorder()
    settings.run.debounce_seconds = 0.0
    sup = _supervisor(settings, resources, emit)
    sup.start()
    resources.ingest_error = OSError("disk full")

    sup.note_changes([str(settings.vault.path / "a.md")], now=10.0)
    sup.tick(monotonic=10.0, wall=datetime(2026, 7, 26, 12, 0))

    assert resources.ingest_calls == 1
    assert "ingest failed: disk full" in emit.joined()
    assert sup.stopping is False


# ---------------------------------------------------------------------------
# debounced ingest through tick()
# ---------------------------------------------------------------------------


def test_changes_inside_the_quiet_window_do_not_trigger_an_ingest(settings: Settings):
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())
    sup.start()

    assert sup.note_changes([str(settings.vault.path / "a.md")], now=100.0) == 1
    sup.tick(monotonic=104.9, wall=datetime(2026, 7, 26, 12, 0))

    assert resources.ingest_calls == 0


def test_a_quiet_window_triggers_exactly_one_ingest_for_the_whole_burst(settings: Settings):
    resources = FakeResources()
    emit = Recorder()
    sup = _supervisor(settings, resources, emit)
    sup.start()

    vault = settings.vault.path
    sup.note_changes([str(vault / "a.md"), str(vault / "b.md")], now=100.0)
    sup.note_changes([str(vault / "c.md")], now=102.0)
    sup.tick(monotonic=106.0, wall=datetime(2026, 7, 26, 12, 0))
    sup.tick(monotonic=200.0, wall=datetime(2026, 7, 26, 12, 1))

    assert resources.ingest_calls == 1
    assert "ingest (3 changed file(s))" in emit.joined()


def test_ignored_files_never_start_the_clock(settings: Settings):
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())
    sup.start()

    accepted = sup.note_changes(
        [
            str(settings.vault.path / "Agent" / "observer.md"),
            str(settings.vault.path / "cover.png"),
        ],
        now=100.0,
    )
    sup.tick(monotonic=999.0, wall=datetime(2026, 7, 26, 12, 0))

    assert accepted == 0
    assert resources.ingest_calls == 0


def test_watchfiles_style_change_tuples_are_understood(settings: Settings):
    """watchfiles yields `{(Change, path)}`; the fakes may yield bare paths."""
    sup = _supervisor(settings, FakeResources(), Recorder())
    sup.start()

    assert sup.note_changes([(2, str(settings.vault.path / "a.md"))], now=1.0) == 1


# ---------------------------------------------------------------------------
# nightly consolidate
# ---------------------------------------------------------------------------


def _gated_on(settings: Settings) -> None:
    settings.agent.enabled = True
    settings.agent.observer_enabled = True


def test_consolidate_runs_when_the_scheduled_time_arrives(settings: Settings):
    _gated_on(settings)
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    assert sup.next_consolidate_at == datetime(2026, 7, 27, 3, 0)

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 2, 59))
    assert resources.consolidate_calls == 0

    sup.tick(monotonic=2.0, wall=datetime(2026, 7, 27, 3, 0, 1))
    assert resources.consolidate_calls == 1
    assert sup.next_consolidate_at == datetime(2026, 7, 28, 3, 0)


def test_consolidate_does_not_run_twice_in_one_night(settings: Settings):
    _gated_on(settings)
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 3, 0, 1))
    sup.tick(monotonic=2.0, wall=datetime(2026, 7, 27, 3, 5))
    sup.tick(monotonic=3.0, wall=datetime(2026, 7, 27, 8, 0))

    assert resources.consolidate_calls == 1


def test_consolidate_is_skipped_when_the_agent_gate_is_closed(settings: Settings):
    settings.agent.enabled = False
    settings.agent.observer_enabled = True
    resources = FakeResources()
    emit = Recorder()
    sup = _supervisor(settings, resources, emit)
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 3, 0, 1))

    assert resources.consolidate_calls == 0
    assert "agent.enabled is false" in emit.joined()


def test_consolidate_is_skipped_when_only_the_observer_gate_is_closed(settings: Settings):
    settings.agent.enabled = True
    settings.agent.observer_enabled = False
    resources = FakeResources()
    emit = Recorder()
    sup = _supervisor(settings, resources, emit)
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 3, 0, 1))

    assert resources.consolidate_calls == 0
    assert "agent.observer_enabled is false" in emit.joined()


def test_the_skip_reason_is_logged_once_not_every_night(settings: Settings):
    settings.agent.enabled = False
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 3, 0, 1))
    sup.tick(monotonic=2.0, wall=datetime(2026, 7, 28, 3, 0, 1))
    sup.tick(monotonic=3.0, wall=datetime(2026, 7, 29, 3, 0, 1))

    assert sum("agent.enabled is false" in line for line in emit.lines) == 1


def test_a_failing_consolidate_is_logged_and_rescheduled(settings: Settings):
    _gated_on(settings)
    resources = FakeResources(consolidate_error=RuntimeError("anthropic said no"))
    emit = Recorder()
    sup = _supervisor(settings, resources, emit)
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 3, 0, 1))

    assert "consolidate failed: anthropic said no" in emit.joined()
    assert sup.next_consolidate_at == datetime(2026, 7, 28, 3, 0)
    assert sup.stopping is False


def test_a_null_consolidate_at_schedules_nothing(settings: Settings):
    _gated_on(settings)
    settings.run.consolidate_at = None
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())
    sup.start(wall=datetime(2026, 7, 26, 23, 0))

    sup.tick(monotonic=1.0, wall=datetime(2026, 7, 27, 3, 0, 1))

    assert sup.next_consolidate_at is None
    assert resources.consolidate_calls == 0


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_the_heartbeat_fires_on_its_cadence_and_not_before(settings: Settings):
    settings.run.heartbeat_minutes = 10.0
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)
    sup.start(monotonic=0.0, wall=datetime(2026, 7, 26, 12, 0))

    sup.tick(monotonic=599.0, wall=datetime(2026, 7, 26, 12, 9))
    assert sup.heartbeats == 0

    sup.tick(monotonic=600.0, wall=datetime(2026, 7, 26, 12, 10))
    assert sup.heartbeats == 1
    assert "alive" in emit.joined()

    sup.tick(monotonic=1_199.0, wall=datetime(2026, 7, 26, 12, 19))
    assert sup.heartbeats == 1

    sup.tick(monotonic=1_200.0, wall=datetime(2026, 7, 26, 12, 20))
    assert sup.heartbeats == 2


def test_a_zero_heartbeat_disables_it(settings: Settings):
    settings.run.heartbeat_minutes = 0.0
    sup = _supervisor(settings, FakeResources(), Recorder())
    sup.start(monotonic=0.0, wall=datetime(2026, 7, 26, 12, 0))

    sup.tick(monotonic=100_000.0, wall=datetime(2026, 7, 27, 12, 0))

    assert sup.heartbeats == 0


# ---------------------------------------------------------------------------
# signals and shutdown
# ---------------------------------------------------------------------------


def test_the_signal_handler_requests_a_stop_and_names_the_signal(settings: Settings):
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)
    sup.start()

    sup.request_stop(signal.SIGTERM, None)

    assert sup.stopping is True
    assert "SIGTERM" in emit.joined()


def test_a_second_signal_does_not_log_twice(settings: Settings):
    emit = Recorder()
    sup = _supervisor(settings, FakeResources(), emit)
    sup.start()

    sup.request_stop(signal.SIGINT, None)
    sup.request_stop(signal.SIGINT, None)

    assert sum("SIGINT" in line for line in emit.lines) == 1


def test_signal_handlers_are_installed_and_then_put_back(settings: Settings):
    """A supervisor must not leave SIGINT pointing at a dead instance — the
    next Ctrl-C belongs to whatever embedded it."""
    before = signal.getsignal(signal.SIGINT)
    sup = _supervisor(settings, FakeResources(), Recorder())

    installed = sup.install_signal_handlers()
    assert int(signal.SIGINT) in installed
    assert signal.getsignal(signal.SIGINT) == sup.request_stop

    sup.restore_signal_handlers()
    assert signal.getsignal(signal.SIGINT) == before


def test_the_loop_restores_signal_handlers_on_the_way_out(settings: Settings):
    before = signal.getsignal(signal.SIGTERM)
    resources = FakeResources()

    async def _watcher():
        sup.request_stop(signal.SIGTERM, None)
        yield ()

    sup = _supervisor(settings, resources, Recorder(), watcher=_watcher, tick_seconds=0.0)

    asyncio.run(sup.run())

    assert signal.getsignal(signal.SIGTERM) == before


def test_shutdown_closes_the_vector_store_exactly_once(settings: Settings):
    resources = FakeResources()
    emit = Recorder()
    sup = _supervisor(settings, resources, emit)
    sup.start()

    sup.shutdown()
    sup.shutdown()

    assert resources.closed == 1
    assert "stopped" in emit.joined()


def test_shutdown_before_start_closes_nothing(settings: Settings):
    resources = FakeResources()
    sup = _supervisor(settings, resources, Recorder())

    sup.shutdown()

    assert resources.closed == 0


# ---------------------------------------------------------------------------
# the async loop
# ---------------------------------------------------------------------------


def test_run_once_ingests_and_shuts_down_without_watching(settings: Settings):
    resources = FakeResources()

    def _watcher():  # pragma: no cover — proving it is never called
        raise AssertionError("--once must not start the watcher")

    sup = _supervisor(settings, resources, Recorder(), watcher=_watcher)

    asyncio.run(sup.run(once=True))

    assert resources.ingest_calls == 1
    assert resources.closed == 1


def test_the_loop_ingests_a_watched_change_and_stops_on_a_signal(settings: Settings):
    settings.run.debounce_seconds = 0.0
    resources = FakeResources()
    emit = Recorder()

    async def _watcher():
        yield {(2, str(settings.vault.path / "note.md"))}
        sup.request_stop(signal.SIGTERM, None)

    sup = _supervisor(
        settings,
        resources,
        emit,
        watcher=_watcher,
        tick_seconds=0.0,
    )

    asyncio.run(sup.run())

    # One initial ingest plus one for the debounced change, then a clean close.
    assert resources.ingest_calls == 2
    assert resources.closed == 1
    assert sup.stopping is True


def test_a_dead_watcher_stops_the_loop_rather_than_spinning_blind(settings: Settings):
    resources = FakeResources()
    emit = Recorder()

    async def _watcher():
        raise OSError("inotify watch limit reached")
        yield  # pragma: no cover — makes this an async generator

    sup = _supervisor(settings, resources, emit, watcher=_watcher, tick_seconds=0.0)

    asyncio.run(sup.run())

    assert "watcher stopped: inotify watch limit reached" in emit.joined()
    assert resources.closed == 1


def test_the_loop_refuses_to_start_when_the_preflight_fails(settings: Settings):
    resources = FakeResources()
    failure = CheckResult("vector store", FAIL, "no answer from http://localhost:6333")
    sup = _supervisor(settings, resources, Recorder(), failures=[failure])

    with pytest.raises(PreflightFailed):
        asyncio.run(sup.run(once=True))

    assert resources.ingest_calls == 0
    assert resources.closed == 0


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_daemon_run_once_reports_the_ingest(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    resources = FakeResources()
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    monkeypatch.setattr("my_daemon.supervisor.preflight", lambda _s: [])
    monkeypatch.setattr("my_daemon.supervisor.build_resources", lambda _s: resources.as_resources())

    result = runner.invoke(app, ["run", "--once"])

    assert result.exit_code == 0, result.output
    assert resources.ingest_calls == 1
    assert resources.closed == 1


def test_daemon_run_refuses_with_the_doctor_hint_when_the_preflight_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    resources = FakeResources()
    failure = CheckResult(
        "vector store",
        FAIL,
        "no answer from http://localhost:6333",
        hint="Is `docker compose up -d` running?",
    )
    monkeypatch.setattr("my_daemon.cli._load", lambda: settings)
    monkeypatch.setattr("my_daemon.supervisor.preflight", lambda _s: [failure])
    monkeypatch.setattr("my_daemon.supervisor.build_resources", lambda _s: resources.as_resources())

    result = runner.invoke(app, ["run", "--once"])

    assert result.exit_code == 1
    assert "no answer from http://localhost:6333" in result.output
    assert "docker compose up -d" in result.output
    assert "daemon doctor" in result.output
    assert resources.ingest_calls == 0
