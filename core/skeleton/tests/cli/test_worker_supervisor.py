"""The multi-worker supervisor: the worker-death line, the dead-worker step, and the run sequence.

The supervisor is built without uvicorn's ``__init__`` (which installs process-wide signal
handlers) and driven over fake worker processes whose ping replies and core-dump
state are scripted; ``run_workers`` runs with the supervisor's ``__init__``/``run`` and the socket
bind replaced.
"""

from __future__ import annotations

import logging
import sys
import threading
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from uvicorn.config import STARTUP_FAILURE

from tai42_skeleton.cli import worker_supervisor
from tai42_skeleton.cli.worker_supervisor import (
    EXITED_BY_ITSELF,
    ExitReportingMultiprocess,
    describe_exit_status,
    describe_worker_death,
    is_dumping_core,
    killed_while_dumping_core,
    missed_health_check,
    run_workers,
)

_APP = "tai42_skeleton.cli.mcp_app:create_app"


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("exitcode", "expected"),
    [
        (0, "exit code 0"),
        (1, "exit code 1"),
        (-9, "killed by signal SIGKILL (-9)"),
        (-11, "killed by signal SIGSEGV (-11)"),
        (-6, "killed by signal SIGABRT (-6)"),
        (-200, "killed by signal signal 200 (-200)"),
        (None, "no exit status reported"),
    ],
)
def test_describe_exit_status(exitcode: int | None, expected: str) -> None:
    assert describe_exit_status(exitcode) == expected


def test_describe_worker_death_exited_by_itself() -> None:
    line = describe_worker_death(8, EXITED_BY_ITSELF, -11)
    assert line == "Child process [8] died (exited by itself): killed by signal SIGSEGV (-11)"


def test_describe_worker_death_missed_health_check() -> None:
    line = describe_worker_death(8, missed_health_check(5), -9)
    assert line == (
        "Child process [8] died (did not answer the health check within 5s and was killed): "
        "killed by signal SIGKILL (-9)"
    )


def test_describe_worker_death_killed_while_dumping_core() -> None:
    line = describe_worker_death(8, killed_while_dumping_core(), -9)
    assert line == (
        "Child process [8] died (crashed and was dumping core when it missed the health check, so it was killed): "
        "killed by signal SIGKILL (-9)"
    )


# --- the core-dump probe ----------------------------------------------------


class _StatusFile:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def __enter__(self) -> list[str]:
        return self._lines

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Name:\tpython\nState:\tD (disk sleep)\nCoreDumping:\t1\n", True),
        ("Name:\tpython\nState:\tS (sleeping)\nCoreDumping:\t0\n", False),
        ("Name:\tpython\nState:\tS (sleeping)\n", False),
    ],
)
def test_is_dumping_core_reads_the_status_field(status: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def fake_open(path: str, **kwargs: Any) -> _StatusFile:
        opened.append(path)
        return _StatusFile(status)

    monkeypatch.setattr(worker_supervisor.sys, "platform", "linux")
    monkeypatch.setattr(worker_supervisor, "open", fake_open, raising=False)

    assert is_dumping_core(4242) is expected
    assert opened == ["/proc/4242/status"]


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_is_dumping_core_is_false_for_a_gone_process(error: type[OSError], monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(path: str, **kwargs: Any) -> _StatusFile:
        raise error

    monkeypatch.setattr(worker_supervisor.sys, "platform", "linux")
    monkeypatch.setattr(worker_supervisor, "open", fake_open, raising=False)

    assert is_dumping_core(4242) is False


def test_is_dumping_core_is_false_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(worker_supervisor, "open", lambda *a, **k: pytest.fail("no /proc read"), raising=False)

    assert is_dumping_core(4242) is False
    assert is_dumping_core(None) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the Linux /proc status file")
def test_is_dumping_core_on_this_live_process() -> None:
    import os

    assert is_dumping_core(os.getpid()) is False


# --- the dead-worker step ---------------------------------------------------


class _FakeOsProcess:
    def __init__(self, owner: _FakeProcess) -> None:
        self._owner = owner

    def is_alive(self) -> bool:
        return self._owner.running


class _FakeProcess:
    """A uvicorn ``Process`` stand-in.

    ``running`` is the OS process state, ``answers`` the ping reply, ``dumping`` whether it
    is writing a core dump, and ``exiting`` whether it is already ending with its own status
    (so the supervisor's kill does not change that status).
    """

    next_pid = 100
    started: ClassVar[list[_FakeProcess]] = []

    def __init__(
        self,
        config: Any = None,
        sockets: Any = None,
        *,
        running: bool = True,
        answers: bool = True,
        own_exitcode: int | None = None,
        dumping: bool = False,
        exiting: bool = False,
    ) -> None:
        self.config = config
        self.sockets = sockets
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.running = running
        self.answers = answers
        self._own_exitcode = own_exitcode
        self.exitcode: int | None = None if running else own_exitcode
        self.dumping = dumping
        self.exiting = exiting
        self.killed = False
        self.joined = False

    @property
    def process(self) -> _FakeOsProcess:
        return _FakeOsProcess(self)

    def is_alive(self, timeout: float = 5) -> bool:
        if not self.running:
            return False
        return self.answers and not self.dumping

    def finish(self) -> None:
        """End the process with its own status, as a finished core dump or a completed exit does."""
        self.running = False
        self.dumping = False
        self.exitcode = self._own_exitcode

    def kill(self) -> None:
        # Like a SIGKILL: no effect on a process that already exited or is already ending.
        self.killed = True
        if self.exiting:
            self.finish()
        elif self.running:
            self.running = False
            self.exitcode = -9

    def join(self) -> None:
        self.joined = True

    def start(self) -> None:
        _FakeProcess.started.append(self)


@pytest.fixture
def supervisor(monkeypatch: pytest.MonkeyPatch) -> ExitReportingMultiprocess:
    _FakeProcess.started = []
    monkeypatch.setattr(worker_supervisor, "Process", _FakeProcess)
    sup = object.__new__(ExitReportingMultiprocess)
    sup.config = SimpleNamespace(timeout_worker_healthcheck=5)  # type: ignore[assignment]
    sup.sockets = ["sock"]  # type: ignore[list-item]
    sup.processes = []
    sup.should_exit = threading.Event()

    def is_dumping_core(pid: int | None) -> bool:
        return any(p.pid == pid and cast("_FakeProcess", p).dumping for p in sup.processes)

    monkeypatch.setattr(worker_supervisor, "is_dumping_core", is_dumping_core)
    return sup


@pytest.fixture
def records() -> Any:
    """Capture the supervisor logger's records directly (uvicorn's log config can stop its propagation)."""
    captured: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Handler(level=logging.DEBUG)
    log = logging.getLogger("uvicorn.error")
    saved_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        log.removeHandler(handler)
        log.setLevel(saved_level)


def test_healthy_workers_are_kept(supervisor: ExitReportingMultiprocess, records: list) -> None:
    workers = [_FakeProcess(), _FakeProcess()]
    supervisor.processes = list(workers)  # type: ignore[arg-type]

    supervisor.keep_subprocess_alive()

    assert supervisor.processes == workers
    assert not any(w.killed for w in workers)
    assert _FakeProcess.started == []
    assert records == []


@pytest.mark.parametrize(
    ("own_exitcode", "status"),
    [(-11, "killed by signal SIGSEGV (-11)"), (-9, "killed by signal SIGKILL (-9)"), (0, "exit code 0")],
)
def test_exited_worker_is_reported_with_its_own_status(
    supervisor: ExitReportingMultiprocess, records: list, own_exitcode: int, status: str
) -> None:
    dead = _FakeProcess(running=False, own_exitcode=own_exitcode)
    healthy = _FakeProcess()
    supervisor.processes = [dead, healthy]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    assert dead.joined
    [replacement] = _FakeProcess.started
    assert supervisor.processes == [replacement, healthy]
    assert replacement.config is supervisor.config
    assert replacement.sockets is supervisor.sockets
    [record] = records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"Child process [{dead.pid}] died (exited by itself): {status}"


def test_unanswered_worker_is_killed_and_reported(supervisor: ExitReportingMultiprocess, records: list) -> None:
    hung = _FakeProcess(answers=False)
    supervisor.processes = [hung]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    assert hung.killed
    assert hung.joined
    [replacement] = _FakeProcess.started
    assert supervisor.processes == [replacement]
    [record] = records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"Child process [{hung.pid}] died (did not answer the health check within 5s and was killed): "
        "killed by signal SIGKILL (-9)"
    )


def test_startup_failure_stops_the_parent(supervisor: ExitReportingMultiprocess, records: list) -> None:
    failed = _FakeProcess(running=False, own_exitcode=STARTUP_FAILURE)
    second = _FakeProcess(running=False, own_exitcode=-11)
    supervisor.processes = [failed, second]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    assert supervisor.should_exit.is_set()
    assert _FakeProcess.started == []
    assert supervisor.processes == [failed, second]
    assert not second.joined
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"Child process [{failed.pid}] failed to start, stopping the parent process."
    assert supervisor.startup_failed


def test_no_replacement_while_exiting(supervisor: ExitReportingMultiprocess, records: list) -> None:
    supervisor.should_exit.set()
    dead = _FakeProcess(running=False, own_exitcode=-11)
    supervisor.processes = [dead]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    assert not dead.joined
    assert _FakeProcess.started == []
    assert records == []


def test_exit_set_during_the_join_skips_the_replacement(supervisor: ExitReportingMultiprocess, records: list) -> None:
    dead = _FakeProcess(running=False, own_exitcode=-11)
    supervisor.processes = [dead]  # type: ignore[list-item]

    def join() -> None:
        supervisor.should_exit.set()

    dead.join = join  # type: ignore[method-assign]

    supervisor.keep_subprocess_alive()

    assert _FakeProcess.started == []
    assert supervisor.processes == [dead]
    assert records == []


def test_worker_that_ended_during_the_ping_exited_by_itself(
    supervisor: ExitReportingMultiprocess, records: list
) -> None:
    # Still reported running when the ping failed, but already ending with its own signal:
    # the supervisor's kill is not what ended it.
    crashing = _FakeProcess(answers=False, exiting=True, own_exitcode=-11)
    supervisor.processes = [crashing]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    [record] = records
    assert (
        record.getMessage() == f"Child process [{crashing.pid}] died (exited by itself): killed by signal SIGSEGV (-11)"
    )


def test_worker_dumping_core_is_killed_and_reported_as_a_crash(
    supervisor: ExitReportingMultiprocess, records: list
) -> None:
    dumping = _FakeProcess(dumping=True, own_exitcode=-11)
    healthy = _FakeProcess()
    supervisor.processes = [dumping, healthy]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    assert dumping.killed
    assert dumping.joined
    [replacement] = _FakeProcess.started
    assert supervisor.processes == [replacement, healthy]
    [record] = records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"Child process [{dumping.pid}] died (crashed and was dumping core when it missed the health "
        "check, so it was killed): killed by signal SIGKILL (-9)"
    )


def test_dump_that_finished_before_the_kill_exited_by_itself(
    supervisor: ExitReportingMultiprocess, records: list
) -> None:
    # Dumping core when the ping failed, but the dump ended and the worker died of its own
    # signal before the supervisor's kill landed.
    dumping = _FakeProcess(dumping=True, exiting=True, own_exitcode=-11)
    supervisor.processes = [dumping]  # type: ignore[list-item]

    supervisor.keep_subprocess_alive()

    [record] = records
    assert (
        record.getMessage() == f"Child process [{dumping.pid}] died (exited by itself): killed by signal SIGSEGV (-11)"
    )


# --- run_workers ------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    sock = object()

    def init(self: Any, config: Any, sockets: Any) -> None:
        self.config = config
        self.sockets = sockets
        rec.calls.append(("init", type(self), config))

    def run(self: Any) -> None:
        rec.calls.append(("run", self.sockets, None))

    class _FakeReload:
        def __init__(self, config: Any, target: Any, sockets: Any) -> None:
            rec.calls.append(("reload-init", config, (target, sockets)))

        def run(self) -> None:
            rec.calls.append(("reload-run", None, None))

    monkeypatch.setattr(ExitReportingMultiprocess, "__init__", init)
    monkeypatch.setattr(ExitReportingMultiprocess, "run", run)
    monkeypatch.setattr(worker_supervisor, "ChangeReload", _FakeReload)
    monkeypatch.setattr(worker_supervisor.Config, "bind_socket", lambda self: sock)
    rec.sock = sock  # type: ignore[attr-defined]
    return rec


# ``log_config=None`` keeps uvicorn from reconfiguring this test process's loggers.
_KWARGS: dict[str, Any] = {
    "factory": True,
    "host": "127.0.0.1",
    "port": 8123,
    "ws": "wsproto",
    "timeout_graceful_shutdown": 7,
    "timeout_worker_healthcheck": 3,
    "log_config": None,
}


def test_run_workers_hands_the_config_to_the_reporting_supervisor(recorder: _Recorder) -> None:
    run_workers(_APP, workers=3, **_KWARGS)

    (kind, cls, config), (run_kind, sockets, _) = recorder.calls
    assert kind == "init"
    assert run_kind == "run"
    assert cls is ExitReportingMultiprocess
    assert sockets == [recorder.sock]  # type: ignore[attr-defined]
    assert config.app == _APP
    assert config.workers == 3
    assert config.factory is True
    assert (config.host, config.port) == ("127.0.0.1", 8123)
    assert config.ws == "wsproto"
    assert config.timeout_graceful_shutdown == 7
    assert config.timeout_worker_healthcheck == 3


def test_run_workers_reload_takes_precedence(recorder: _Recorder) -> None:
    run_workers(_APP, workers=2, reload=True, **_KWARGS)

    kinds = [call[0] for call in recorder.calls]
    assert kinds == ["reload-init", "reload-run"]
    _, config, (target, sockets) = recorder.calls[0]
    assert config.should_reload
    assert sockets == [recorder.sock]  # type: ignore[attr-defined]
    assert target.__self__.config is config


def test_run_workers_refuses_a_single_worker(recorder: _Recorder) -> None:
    with pytest.raises(ValueError, match="more than one worker; got workers=1"):
        run_workers(_APP, workers=1, **_KWARGS)
    assert recorder.calls == []


def test_run_workers_puts_app_dir_on_sys_path(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    run_workers(_APP, workers=2, app_dir=str(tmp_path), **_KWARGS)
    assert sys.path[0] == str(tmp_path)


def test_run_workers_removes_the_unix_socket(recorder: _Recorder, tmp_path) -> None:
    uds = tmp_path / "serve.sock"
    uds.touch()
    kwargs = {k: v for k, v in _KWARGS.items() if k not in {"host", "port"}}

    run_workers(_APP, workers=2, uds=str(uds), **kwargs)

    assert not uds.exists()


def test_run_workers_exits_with_the_startup_failure_status_when_a_worker_failed_to_start(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A worker that fails its startup stops the parent, and the parent exits with uvicorn's
    # startup-failure status, as a single-worker ``uvicorn.run`` does, so a refused boot is
    # never a clean exit. The Unix socket is still removed on the way out.
    uds = tmp_path / "serve.sock"
    uds.touch()
    kwargs = {k: v for k, v in _KWARGS.items() if k not in {"host", "port"}}

    def failed_startup(self: Any) -> None:
        self.startup_failed = True

    monkeypatch.setattr(ExitReportingMultiprocess, "run", failed_startup)
    with pytest.raises(SystemExit) as exc:
        run_workers(_APP, workers=2, uds=str(uds), **kwargs)

    assert exc.value.code == STARTUP_FAILURE
    assert not uds.exists()


def test_run_workers_returns_on_keyboard_interrupt(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(self: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ExitReportingMultiprocess, "run", interrupted)
    run_workers(_APP, workers=2, **_KWARGS)
