# SPDX-License-Identifier: Apache-2.0
"""`daemon run` — the foreground supervisor, and the units that start it.

This is the file that turns a manually-invoked tool into an actual daemon.
Everything the user previously had to remember to type — re-ingest after
editing notes, consolidate at night — happens here, in one long-lived process.

**Foreground on purpose.** Nothing here forks, detaches, writes a pidfile or
redirects stdout. That is not an omission: systemd (`Type=simple`) and Windows
Task Scheduler both *want* a process that stays in the foreground and logs to
stdout, and a double-forking daemon fights both of them. :func:`build_schedule_unit`
generates the unit that does the backgrounding.

**Three clocks, deliberately separated.**

* ``monotonic`` drives the debounce and the heartbeat. It cannot jump backwards
  when NTP corrects the clock, so a time sync can never wedge a pending ingest.
* wall-clock ``datetime`` drives the nightly consolidation, because "03:00"
  means the wall clock, not an elapsed interval.
* Both are injected, which is what lets the tests drive a five-second debounce
  and a month of nights without sleeping once.

**DST naivety, stated plainly.** :func:`next_occurrence` does naive local
arithmetic: it asks for the next wall-clock ``HH:MM`` after now, and adds one
calendar day when that has passed. On a DST boundary the gap between two runs
is therefore 23 or 25 hours rather than 24, and a job scheduled inside the hour
that a spring-forward deletes runs at the next occurrence instead. For a
nightly consolidation of a personal vault that is the right trade against
dragging in a timezone database and a cron parser.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import sysconfig
import time as _time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from my_daemon.config import Settings
from my_daemon.doctor import CheckResult, check_vault, check_vector_store

__all__ = [
    "SYSTEMD_UNIT_NAME",
    "WINDOWS_TASK_NAME",
    "Debouncer",
    "PreflightFailed",
    "Resources",
    "ScheduleUnit",
    "Supervisor",
    "UnsupportedPlatform",
    "VaultChangeFilter",
    "build_resources",
    "build_schedule_unit",
    "current_platform",
    "daemon_executable",
    "next_occurrence",
    "preflight",
    "schtasks_command",
    "systemd_unit",
    "systemd_unit_path",
    "task_command",
]

#: Whatever `signal.signal` accepts and hands back — a callback, one of the
#: `SIG_DFL`/`SIG_IGN` constants, or None. Named here so the saved-handler map
#: can be round-tripped without loosening it to `object`.
SignalHandler = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None

#: How often the supervisor wakes to check its own timers. Fine-grained enough
#: that a 5-second debounce is honoured within a second, coarse enough that an
#: idle daemon is invisible in `top`.
TICK_SECONDS = 1.0

SYSTEMD_UNIT_NAME = "my-daemon.service"
WINDOWS_TASK_NAME = "MyDaemonRun"


# ---------------------------------------------------------------------------
# debounce
# ---------------------------------------------------------------------------


class Debouncer:
    """Collapse a burst of file events into a single ingest.

    Saving a note in Obsidian is rarely one filesystem event, and a vault sync
    client can rewrite a hundred files in a second. The rule is *quiet time*,
    not rate limiting: the timer restarts on every new event, so an ingest runs
    once the vault has stopped changing rather than in the middle of a sync.

    Pure by construction — it owns no clock. Every method takes the caller's
    monotonic reading, which is what makes a five-second window testable in
    microseconds.
    """

    def __init__(self, quiet_seconds: float) -> None:
        self.quiet_seconds = quiet_seconds
        self._last_event: float | None = None
        self._count = 0

    @property
    def pending(self) -> bool:
        return self._last_event is not None

    @property
    def pending_count(self) -> int:
        return self._count

    def record(self, now: float, count: int = 1) -> None:
        """Note ``count`` changes seen at monotonic time ``now``."""
        self._last_event = now
        self._count += count

    def ready(self, now: float) -> bool:
        """True once the vault has been quiet for the whole window."""
        if self._last_event is None:
            return False
        return now - self._last_event >= self.quiet_seconds

    def seconds_remaining(self, now: float) -> float | None:
        """How much longer the window has to run; None when nothing is pending."""
        if self._last_event is None:
            return None
        return max(0.0, self.quiet_seconds - (now - self._last_event))

    def take(self) -> int:
        """Claim the pending burst and reset. Returns how many changes it held."""
        count = self._count
        self._last_event = None
        self._count = 0
        return count


# ---------------------------------------------------------------------------
# which changes are worth an ingest
# ---------------------------------------------------------------------------


class VaultChangeFilter:
    """Decide whether a raw watcher path should schedule an ingest.

    Mirrors :class:`my_daemon.vault.reader.VaultReader`'s rules — ``.md`` only,
    case-insensitive directory exclusions — so the watcher can never wake the
    daemon for a file the reader would then refuse to read.

    The agent folder is excluded *unconditionally*, not merely because it is in
    the default ``exclude_dirs``. It is the daemon's own writeback target: a
    nightly `consolidate` writing ``Agent/observer-2026-07-26.md`` must not, at
    03:00, schedule an ingest to discover its own letter. Ingest would skip it
    on the content hash anyway; the point is not to spend the wakeup.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        exclude_dirs: Iterable[str],
        agent_folder: str,
    ) -> None:
        self.vault_root = Path(vault_root)
        self.excluded = {d.casefold() for d in exclude_dirs}
        if agent_folder:
            self.excluded.add(agent_folder.casefold())

    def accepts(self, path: str | Path) -> bool:
        candidate = Path(path)
        if candidate.suffix.casefold() != ".md":
            return False
        try:
            relative = candidate.relative_to(self.vault_root)
        except ValueError:
            return False
        return not any(part.casefold() in self.excluded for part in relative.parts[:-1])

    def select(self, paths: Iterable[str | Path]) -> list[Path]:
        """The accepted paths, deduplicated, in the order first seen."""
        seen: dict[Path, None] = {}
        for raw in paths:
            candidate = Path(raw)
            if candidate not in seen and self.accepts(candidate):
                seen[candidate] = None
        return list(seen)


# ---------------------------------------------------------------------------
# schedule arithmetic
# ---------------------------------------------------------------------------


def next_occurrence(at: time, after: datetime) -> datetime:
    """The next local wall-clock ``at`` strictly after ``after``.

    "Strictly" matters at startup: a daemon started at exactly 03:00:00 has not
    run the 03:00 job, but neither has it missed it by enough to be worth
    guessing, and firing a consolidation on the same second the process came up
    is the more surprising of the two behaviours.

    Naive local arithmetic — see this module's docstring on DST.
    """
    candidate = after.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class PreflightFailed(RuntimeError):
    """Startup checks refused. Carries the failing :class:`CheckResult` rows."""

    def __init__(self, failures: Sequence[CheckResult]) -> None:
        self.failures = list(failures)
        super().__init__("; ".join(f"{f.name}: {f.detail}" for f in self.failures))


def preflight(settings: Settings) -> list[CheckResult]:
    """Doctor-lite: the two checks that make an unattended run pointless.

    Not the full `daemon doctor` sweep — a missing API key or a cold model
    cache are things `run` can recover from on its own, and a supervisor that
    refuses to start over a warning is a supervisor nobody enables. A missing
    vault or an unopenable vector store are different: every subsequent ingest
    would fail identically, forever, in a log nobody is reading.

    Returns only failures, so an empty list means "go".
    """
    results = [check_vault(settings)]
    store_result, store = check_vector_store(settings)
    if store is not None:
        # Doctor's contract is caller-closes, and it matters more here than
        # anywhere: in embedded mode this probe holds the storage lock the
        # supervisor is about to want for the rest of its life.
        store.close()
    results.append(store_result)
    return [r for r in results if r.failed]


def _run_preflight(settings: Settings) -> list[CheckResult]:
    """Indirection so tests can monkeypatch the module-level :func:`preflight`."""
    return preflight(settings)


# ---------------------------------------------------------------------------
# what the supervisor drives
# ---------------------------------------------------------------------------


class IngestSummary(Protocol):
    """The three fields the supervisor's log line reads off `IngestStats`."""

    notes_new_or_updated: int
    notes_deleted: int
    skipped_unchanged: int


@dataclass
class Resources:
    """The long-lived collaborators, behind three callables.

    Built once and reused for the process lifetime — reopening the embedded
    store and reloading the embedding model on every keystroke-triggered ingest
    would cost more than the ingest.
    """

    ingest: Callable[[], IngestSummary]
    consolidate: Callable[[], object]
    close: Callable[[], None]


def build_resources(settings: Settings) -> Resources:
    """Wire the real stores. Imports are local: the heavy dependencies belong
    to a process that is actually going to run, not to `daemon --help`."""
    from my_daemon.integration.wiring import build_stores
    from my_daemon.pipeline import ingest_vault
    from my_daemon.pipeline.agent_observe import run_observe
    from my_daemon.stores import AgentStateStore

    stores = build_stores(settings)
    state = AgentStateStore(db_path=settings.feedback.db_path)

    def _ingest() -> IngestSummary:
        return ingest_vault(
            settings,
            stores.embedder,
            stores.vector_store,
            stores.graph_store,
            sparse_embedder=stores.sparse_embedder,
        )

    def _consolidate() -> object:
        return run_observe(
            settings, state, stores.feedback_store, stores.graph_store, stores.llm
        )

    return Resources(ingest=_ingest, consolidate=_consolidate, close=stores.vector_store.close)


# ---------------------------------------------------------------------------
# the supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """One foreground process: watch, debounce, ingest, consolidate, heartbeat.

    The loop is deliberately single-threaded and non-overlapping. Ingest is a
    blocking call made straight from the tick, so a change arriving mid-ingest
    is queued rather than racing it — which is also why SIGTERM "finishes
    in-flight work" for free: the signal sets a flag, and the flag is only ever
    read between whole units of work.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resources_factory: Callable[[], Resources] | None = None,
        preflight: Callable[[Settings], list[CheckResult]] | None = None,
        watcher: Callable[[], AsyncIterator[Iterable[object]]] | None = None,
        emit: Callable[[str], None] = print,
        monotonic: Callable[[], float] = _time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.settings = settings
        # Both defaults resolve their module global lazily, so a test that
        # patches `supervisor.build_resources` after import still wins.
        self._resources_factory = resources_factory or (lambda: build_resources(settings))
        self.preflight = preflight if preflight is not None else _run_preflight
        self._watcher = watcher or self._default_watcher
        self._emit = emit
        self._monotonic = monotonic
        self._now = now
        self.tick_seconds = tick_seconds

        self.resources: Resources | None = None
        self.debouncer = Debouncer(settings.run.debounce_seconds)
        self.changes = VaultChangeFilter(
            settings.vault.path,
            exclude_dirs=settings.vault.exclude_dirs,
            agent_folder=settings.agent.folder_name,
        )

        self.next_consolidate_at: datetime | None = None
        self.ingest_runs = 0
        self.consolidate_runs = 0
        self.heartbeats = 0
        self._heartbeat_seconds = max(0.0, settings.run.heartbeat_minutes * 60.0)
        self._next_heartbeat: float | None = None
        self._skip_reason_logged: str | None = None
        self._previous_handlers: dict[int, SignalHandler] = {}
        self._stopping = False
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stopping

    def start(self, *, monotonic: float | None = None, wall: datetime | None = None) -> None:
        """Preflight, then build resources, then announce.

        Order is load-bearing: building resources loads the embedding model and
        takes the embedded store's exclusive lock. Doing that ahead of a doomed
        preflight would make the refusal slow *and* hold the very lock the user
        is being told to free.
        """
        failures = self.preflight(self.settings)
        if failures:
            raise PreflightFailed(failures)

        self.resources = self._resources_factory()

        started_at = self._monotonic() if monotonic is None else monotonic
        clock = self._now() if wall is None else wall
        if self._heartbeat_seconds > 0:
            self._next_heartbeat = started_at + self._heartbeat_seconds
        at = self.settings.run.consolidate_time
        self.next_consolidate_at = next_occurrence(at, clock) if at is not None else None

        self._banner()

    def _banner(self) -> None:
        s = self.settings
        self._emit(f"watching {s.vault.path}")
        self._emit(
            f"debounce {s.run.debounce_seconds:g}s of quiet before each incremental ingest"
        )
        if self.next_consolidate_at is not None:
            self._emit(
                f"nightly consolidate at {s.run.consolidate_at} — "
                f"next {self.next_consolidate_at:%Y-%m-%d %H:%M}"
            )
        else:
            self._emit("nightly consolidate disabled (run.consolidate_at is null)")

        qdrant = s.vector_store.qdrant
        if qdrant.is_embedded:
            # The honest version of the concurrency story. The GUI is a
            # *separate process*, so it hits this wall exactly like the CLI
            # does — saying otherwise would send the user to a surface that is
            # about to refuse them too.
            self._emit(
                f"embedded vector store at {qdrant.path} — this process holds it "
                "exclusively. Every other my-daemon process (daemon query, daemon "
                "chat, the GUI, Hermes) will refuse to open it until `daemon run` "
                "stops. Set vector_store.qdrant.url (server mode) if you need "
                "concurrent access."
            )
        else:
            self._emit(f"vector store: server at {qdrant.url}")

    def shutdown(self) -> None:
        """Release everything. Idempotent — the loop and the CLI both call it."""
        if self._closed or self.resources is None:
            return
        self._closed = True
        self.resources.close()
        # The graph lock is per-write (`GraphStore.lock()` wraps `save()`), so
        # there is nothing long-held to release here — but nothing may still be
        # mid-save either, which the non-overlapping loop guarantees.
        self._emit("stopped — vector store closed, graph lock released.")

    def request_stop(self, signum: int | None = None, frame: object = None) -> None:
        """The SIGINT/SIGTERM handler. Sets a flag; never does work itself."""
        if self._stopping:
            return
        self._stopping = True
        name = "stop requested"
        if signum is not None:
            with contextlib.suppress(ValueError):
                name = signal.Signals(signum).name
        self._emit(f"{name} — finishing in-flight work, then shutting down.")

    def install_signal_handlers(self) -> list[int]:
        """Best-effort. `signal.signal` only works on the main thread, and
        SIGTERM is not deliverable everywhere; neither is a reason to refuse to
        run, only a reason to lose the graceful path."""
        installed: list[int] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError, AttributeError, RuntimeError):
                self._previous_handlers[int(sig)] = signal.signal(sig, self.request_stop)
                installed.append(int(sig))
        return installed

    def restore_signal_handlers(self) -> None:
        """Put back whatever was there before.

        A supervisor is a library object as much as a process: leaving SIGINT
        pointing at a dead instance would swallow the next Ctrl-C of whatever
        embedded it (the test suite, most immediately).
        """
        while self._previous_handlers:
            signum, handler = self._previous_handlers.popitem()
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signum, handler)

    # -- work --------------------------------------------------------------

    def initial_ingest(self) -> None:
        """The one ingest that is allowed to kill the process.

        Fail fast at startup — a supervisor that cannot complete a single
        ingest is not something to leave running unattended for a week. Once
        the loop is up the rule inverts (see :meth:`_ingest`): a transient
        failure must not take the daemon down.
        """
        assert self.resources is not None, "call start() first"
        self._emit("initial ingest…")
        stats = self.resources.ingest()
        self.ingest_runs += 1
        self._emit(f"initial ingest: {_describe(stats)}")

    def _ingest(self, reason: str) -> None:
        assert self.resources is not None, "call start() first"
        self._emit(f"ingest ({reason})…")
        try:
            stats = self.resources.ingest()
        except Exception as exc:  # noqa: BLE001 — a daemon outlives one bad run
            self._emit(f"ingest failed: {exc}")
            return
        self.ingest_runs += 1
        self._emit(f"ingest done: {_describe(stats)}")

    def note_changes(self, batch: Iterable[object], *, now: float) -> int:
        """Feed one watcher batch into the debouncer. Returns accepted count.

        Accepts watchfiles' ``(Change, path)`` tuples and bare paths alike.
        """
        accepted = self.changes.select(_batch_paths(batch))
        if accepted:
            self.debouncer.record(now, len(accepted))
        return len(accepted)

    def tick(self, *, monotonic: float, wall: datetime) -> None:
        """One pass over the timers. Everything the loop does, it does here."""
        if self.debouncer.ready(monotonic):
            count = self.debouncer.take()
            self._ingest(f"{count} changed file(s)")
        self._maybe_consolidate(wall)
        self._maybe_heartbeat(monotonic)

    def _maybe_consolidate(self, wall: datetime) -> None:
        if self.next_consolidate_at is None or wall < self.next_consolidate_at:
            return
        at = self.settings.run.consolidate_time
        assert at is not None  # next_consolidate_at is only set when it is
        # Reschedule first, unconditionally: a skip, a crash and a success must
        # all cost exactly one night.
        self.next_consolidate_at = next_occurrence(at, wall)

        reason = self._consolidate_blocked_reason()
        if reason is not None:
            # Once, not every night. A user who has deliberately left the
            # observer off does not need a nightly reminder in their journal.
            if self._skip_reason_logged != reason:
                self._skip_reason_logged = reason
                self._emit(f"nightly consolidate skipped — {reason}")
            return

        self._skip_reason_logged = None
        assert self.resources is not None, "call start() first"
        self._emit("nightly consolidate starting…")
        try:
            self.resources.consolidate()
        except Exception as exc:  # noqa: BLE001 — same rule as ingest
            self._emit(f"consolidate failed: {exc}")
            return
        self.consolidate_runs += 1
        self._emit(f"consolidate done — next {self.next_consolidate_at:%Y-%m-%d %H:%M}")

    def _consolidate_blocked_reason(self) -> str | None:
        """The same two gates `daemon consolidate` enforces, worded the same."""
        agent = self.settings.agent
        if not agent.enabled:
            return (
                "agent.enabled is false in config.yaml (the master writeback gate). "
                "Flip it when you're ready for the daemon to write."
            )
        if not agent.observer_enabled:
            return (
                "agent.observer_enabled is false in config.yaml (the observer has "
                "its own gate, separate from agent.enabled)."
            )
        return None

    def _maybe_heartbeat(self, monotonic: float) -> None:
        if self._next_heartbeat is None or monotonic < self._next_heartbeat:
            return
        self._next_heartbeat = monotonic + self._heartbeat_seconds
        self.heartbeats += 1
        pending = self.debouncer.pending_count
        nxt = (
            f"{self.next_consolidate_at:%Y-%m-%d %H:%M}"
            if self.next_consolidate_at is not None
            else "off"
        )
        self._emit(
            f"alive — {self.ingest_runs} ingest run(s), {self.consolidate_runs} "
            f"consolidation(s), {pending} change(s) pending, next consolidate {nxt}"
        )

    # -- the loop ----------------------------------------------------------

    def _default_watcher(self) -> AsyncIterator[Iterable[object]]:
        """The real thing. Imported here so the module stays importable — and
        the pure components stay testable — without watchfiles present."""
        from watchfiles import awatch

        return awatch(self.settings.vault.path, recursive=True)

    async def _watch_loop(self) -> None:
        try:
            async for batch in self._watcher():
                self.note_changes(batch, now=self._monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced, then fatal
            # A dead watcher is not survivable the way a failed ingest is: the
            # daemon would keep heartbeating "alive" while silently no longer
            # watching anything. Better to stop and let the unit restart us.
            self._emit(f"watcher stopped: {exc}")
            self._stopping = True

    async def run(self, *, once: bool = False) -> None:
        """Start up, then either exit (`--once`) or loop until signalled."""
        self.start()
        try:
            self.initial_ingest()
            if once:
                return
            self.install_signal_handlers()
            watch_task = asyncio.create_task(self._watch_loop())
            try:
                while True:
                    self.tick(monotonic=self._monotonic(), wall=self._now())
                    if self._stopping:
                        break
                    await asyncio.sleep(self.tick_seconds)
            finally:
                watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch_task
                self.restore_signal_handlers()
        finally:
            self.shutdown()


def _describe(stats: IngestSummary) -> str:
    return (
        f"{stats.notes_new_or_updated} new/updated, "
        f"{stats.notes_deleted} deleted, {stats.skipped_unchanged} unchanged"
    )


def _batch_paths(batch: Iterable[object]) -> list[str]:
    """Normalise one watcher batch to plain paths.

    watchfiles yields ``set[tuple[Change, str]]``; accepting bare paths too
    keeps the injected fakes honest without a shim on their side.
    """
    paths: list[str] = []
    for item in batch:
        paths.append(str(item[1]) if isinstance(item, tuple) else str(item))
    return paths


# ---------------------------------------------------------------------------
# generated scheduler units
# ---------------------------------------------------------------------------


class UnsupportedPlatform(RuntimeError):
    """No unit shape is generated for this OS."""


@dataclass(frozen=True)
class ScheduleUnit:
    """A generated unit, plus what the user has to type next.

    ``target`` is None for shapes that are a *command* rather than a file
    (Windows), which is also how `install` knows there is nothing to write.
    """

    kind: str
    text: str
    instructions: list[str]
    executable: Path
    target: Path | None = None

    @property
    def executable_missing(self) -> bool:
        """A unit naming a binary that isn't there installs fine and fails at
        boot, in a log nobody is watching. Worth one warning up front."""
        return not self.executable.is_file()


def current_platform() -> str:
    """``sys.platform``, behind a seam a test can replace without patching
    `sys` itself out from under rich and typer."""
    return sys.platform


def daemon_executable() -> Path:
    """The `daemon` console script for the environment currently running.

    ``sysconfig.get_path("scripts")`` rather than anything derived from
    ``sys.executable`` by hand, for two reasons a smoke test found the hard way:

    * A venv's ``bin/python`` is a **symlink to the system interpreter**, so
      ``Path(sys.executable).resolve()`` yields ``/usr/bin/python3.14`` and its
      sibling is ``/usr/bin/daemon`` — a path that does not exist. The unit
      would install cleanly and fail forever at boot.
    * It is the same call pip and the installer use to place the script, and it
      already knows ``bin`` vs ``Scripts``.

    Never ``shutil.which``: a scheduler starts with a PATH nobody chose, and
    the daemon that should run is the one in *this* environment.
    """
    scripts = Path(sysconfig.get_path("scripts"))
    return scripts / ("daemon.exe" if sys.platform == "win32" else "daemon")


def systemd_unit_path() -> Path:
    """``$XDG_CONFIG_HOME/systemd/user/my-daemon.service``, or the ~/.config default."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user" / SYSTEMD_UNIT_NAME


def systemd_unit(exe: Path, config_path: Path) -> str:
    """A `systemd --user` service that runs the supervisor in the foreground.

    Deliberately *no* companion ``.timer``: the supervisor owns its own
    schedule, and a timer would either double-fire the consolidation or restart
    a process that never stopped. Deliberately no ``WorkingDirectory=`` either
    — the config is named absolutely and every relative path inside it anchors
    to *its* directory, which is the same rule the schtasks path follows.
    """
    return (
        "[Unit]\n"
        "Description=My Daemon — vault watcher and nightly consolidation\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'ExecStart="{exe}" --config "{config_path}" run\n'
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def task_command(exe: Path, config_path: Path, subcommand: str) -> str:
    """The `/TR` payload for a Windows scheduled task.

    One shape, shared with :func:`my_daemon.gui.setup.reflect_task_command` —
    ``test_schedule.py`` pins the two together so a change to either quoting
    rule breaks a test instead of a user's scheduled job.
    """
    return f'"{exe}" --config "{config_path}" {subcommand}'


def schtasks_command(exe: Path, config_path: Path) -> str:
    """The full `schtasks /Create` line, ready to paste into a Command Prompt.

    The inner quotes are backslash-escaped because ``/TR`` takes one quoted
    argument that itself contains quoted paths; this is the form `schtasks`
    documents, and the reason the GUI (which passes an argv list to
    `subprocess`) does not need it.
    """
    inner = task_command(exe, config_path, "run").replace('"', '\\"')
    return f'schtasks /Create /SC ONLOGON /TN "{WINDOWS_TASK_NAME}" /TR "{inner}" /F'


def build_schedule_unit(
    settings: Settings,
    *,
    platform: str | None = None,
    exe: Path | None = None,
) -> ScheduleUnit:
    """The unit for this OS, with every path already substituted."""
    target_platform = platform if platform is not None else current_platform()
    executable = exe if exe is not None else daemon_executable()
    config_path = settings.config_path
    if config_path is None:
        raise ValueError(
            "these settings came from no config file, so there is no --config path to "
            "put in the unit. Run `daemon schedule` with a config on disk (see "
            "`daemon init`)."
        )

    if target_platform.startswith("linux"):
        return ScheduleUnit(
            kind="systemd-user",
            text=systemd_unit(executable, config_path),
            executable=executable,
            target=systemd_unit_path(),
            instructions=[
                "systemctl --user daemon-reload",
                f"systemctl --user enable --now {SYSTEMD_UNIT_NAME}",
                f"journalctl --user -u {SYSTEMD_UNIT_NAME} -f    # follow the log",
                "loginctl enable-linger $USER    # keep it running after you log out",
            ],
        )

    if target_platform == "win32":
        return ScheduleUnit(
            kind="schtasks",
            text=schtasks_command(executable, config_path),
            executable=executable,
            target=None,
            instructions=[
                "Paste the command above into a Command Prompt to register the task.",
                f'schtasks /Query /TN "{WINDOWS_TASK_NAME}"    # confirm it exists',
                f'schtasks /Delete /TN "{WINDOWS_TASK_NAME}" /F    # remove it again',
            ],
        )

    raise UnsupportedPlatform(
        f"no scheduler unit is generated for {target_platform!r} yet. Run the "
        "supervisor from a launchd plist or your init system of choice with "
        f'`"{executable}" --config "{config_path}" run`, or drive a periodic '
        "ingest from cron with `daemon run --once`."
    )
