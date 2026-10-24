"""The multi-worker supervisor ``tai serve --workers N`` runs: uvicorn's, with the reason for each worker death logged.

uvicorn's stock supervisor replaces a dead worker with one ``Child process [N] died`` line,
which cannot tell a worker that exited on its own (a crash, an outside kill, a normal exit)
from a live worker the supervisor killed because it missed the health ping.
:class:`ExitReportingMultiprocess` overrides only that dead-worker step to log which of the
two happened and the worker's exit status; every other supervisor behaviour is uvicorn's.
uvicorn's supervisor also returns normally after a worker fails its startup, so
:func:`run_workers` exits with uvicorn's ``STARTUP_FAILURE`` status then, as a single-worker
``uvicorn.run`` does.

A crashed worker whose core is being dumped (a ``core_pattern`` pipe handler such as apport
or systemd-coredump keeps it alive, threads stopped, until the dump is written) misses the
health ping too and is killed like any unanswering worker; its line says it crashed and was
dumping core, so the crash is not mistaken for a hang.

This relies on uvicorn's non-exported supervisor internals — ``Multiprocess.processes`` /
``should_exit`` / ``sockets`` / ``config``, the ``keep_subprocess_alive`` step, and the
``uvicorn.supervisors.multiprocess.Process`` wrapper with its ``process`` attribute (the
underlying :class:`multiprocessing.Process`). The ``uvicorn>=0.52.4,<0.55`` pin in this
package's ``pyproject.toml`` holds that shape stable.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from typing import Any

from uvicorn import Config, Server
from uvicorn.config import STARTUP_FAILURE
from uvicorn.supervisors import ChangeReload, Multiprocess
from uvicorn.supervisors.multiprocess import Process

# The supervisor's own logger, so the death line sits beside uvicorn's supervisor lines
# and follows the same log config.
logger = logging.getLogger("uvicorn.error")

EXITED_BY_ITSELF = "exited by itself"


def missed_health_check(timeout: float) -> str:
    """The death reason of a live worker killed for not answering the health ping."""
    return f"did not answer the health check within {timeout}s and was killed"


def killed_while_dumping_core() -> str:
    """The death reason of a crashed worker killed because it missed the health ping while dumping core."""
    return "crashed and was dumping core when it missed the health check, so it was killed"


def describe_exit_status(exitcode: int | None) -> str:
    """Render a worker's exit status: ``exit code N`` for ``N >= 0``, the signal name for a negative code."""
    if exitcode is None:
        return "no exit status reported"
    if exitcode >= 0:
        return f"exit code {exitcode}"
    try:
        name = signal.Signals(-exitcode).name
    except ValueError:
        name = f"signal {-exitcode}"
    return f"killed by signal {name} ({exitcode})"


def describe_worker_death(pid: int | None, reason: str, exitcode: int | None) -> str:
    """The one log line naming a dead worker, why it is being replaced, and its exit status."""
    return f"Child process [{pid}] died ({reason}): {describe_exit_status(exitcode)}"


def is_dumping_core(pid: int | None) -> bool:
    """Whether the live process ``pid`` is in the middle of writing a core dump.

    Read from the ``CoreDumping:`` field of ``/proc/<pid>/status``, which Linux exposes from
    4.15 on. Without that field (another OS, an older kernel) a dump cannot be seen, so this
    is ``False``; a process that is already gone is not dumping either.
    """
    if pid is None or not sys.platform.startswith("linux"):
        return False
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as status:
            for line in status:
                if line.startswith("CoreDumping:"):
                    return line.split(":", 1)[1].strip() == "1"
    except (FileNotFoundError, ProcessLookupError):
        return False
    return False


class ExitReportingMultiprocess(Multiprocess):
    """uvicorn's ``Multiprocess`` supervisor, logging why each dead worker is replaced.

    ``startup_failed`` turns true when a worker failed its startup and the supervisor
    stopped the parent for it, so :func:`run_workers` can exit with uvicorn's
    startup-failure status.
    """

    startup_failed: bool = False

    def keep_subprocess_alive(self) -> None:
        """Replace every worker that exited or missed its health ping, as uvicorn does, logging which it was.

        A worker that misses the ping is killed and replaced at once. When the supervisor's
        SIGKILL is what ended a worker that was still running, the worker is reported as a
        crash killed during its core dump if it was dumping core just before the kill, and as
        killed for the missed ping otherwise. A worker that ended any other way, including one
        that died while the ping was in flight or finished its dump before the kill landed,
        exited by itself.
        """
        if self.should_exit.is_set():
            return

        timeout = self.config.timeout_worker_healthcheck
        for idx, process in enumerate(self.processes):
            if process.is_alive(timeout=timeout):
                continue
            running = process.process.is_alive()
            dumping = running and is_dumping_core(process.pid)

            process.kill()
            process.join()

            if process.exitcode == STARTUP_FAILURE:
                logger.error(f"Child process [{process.pid}] failed to start, stopping the parent process.")
                self.startup_failed = True
                self.should_exit.set()
                return

            if self.should_exit.is_set():
                return

            killed = running and process.exitcode == -signal.SIGKILL
            if killed and dumping:
                reason = killed_while_dumping_core()
            elif killed:
                reason = missed_health_check(timeout)
            else:
                reason = EXITED_BY_ITSELF
            logger.warning(describe_worker_death(process.pid, reason, process.exitcode))
            process = Process(self.config, self.sockets)
            process.start()
            self.processes[idx] = process


def run_workers(app: str, **kwargs: Any) -> None:
    """Serve ``app`` on several workers as ``uvicorn.run`` does, under :class:`ExitReportingMultiprocess`.

    Takes ``uvicorn.run``'s keyword arguments and follows its multi-worker sequence:
    ``app_dir`` onto ``sys.path``, one :class:`uvicorn.Config`, a :class:`uvicorn.Server`,
    then the reload supervisor when reload is on (it takes precedence over workers, as in
    ``uvicorn.run``), else the worker supervisor on the bound socket; the Unix socket is
    removed on the way out. A worker that failed its startup stops the supervisor, and the
    run then exits with uvicorn's ``STARTUP_FAILURE`` status, as ``uvicorn.run`` does for a
    single worker. Fewer than two workers is refused — a single worker has no supervisor and
    runs through ``uvicorn.run`` itself.
    """
    app_dir = kwargs.pop("app_dir", None)
    if app_dir is not None:
        sys.path.insert(0, app_dir)

    config = Config(app, **kwargs)
    if config.workers < 2:
        raise ValueError(f"run_workers serves more than one worker; got workers={config.workers}.")
    server = Server(config=config)

    try:
        sock = config.bind_socket()
        if config.should_reload:
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        else:
            supervisor = ExitReportingMultiprocess(config, sockets=[sock])
            supervisor.run()
            if supervisor.startup_failed:
                sys.exit(STARTUP_FAILURE)
    except KeyboardInterrupt:
        # A Ctrl-C that reaches here (before the supervisor's own signal handling is
        # installed) ends the run normally, as in ``uvicorn.run``.
        pass
    finally:
        if config.uds and os.path.exists(config.uds):
            os.remove(config.uds)
