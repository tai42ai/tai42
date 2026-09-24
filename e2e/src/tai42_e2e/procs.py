"""Process spawning and lifecycle for the real ``tai`` entrypoints.

Each spawned process is its own session leader (``start_new_session=True``) so a
``tai serve`` master and the uvicorn workers it forks share one process group
the harness can signal as a unit. stdout/stderr go straight to log FILES — no
pipes, so there is no pump thread and no PIPE-full deadlock — and failure
diagnostics read the file tail. The child env is built from scratch by the
caller; this module never consults ``os.environ``."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tai42_e2e.waiting import WaitTimeoutError, wait_for


@dataclass
class ProcessHandle:
    """A spawned OS process (a ``tai serve`` / ``tai backend worker`` /
    ``tai metrics`` invocation) plus its log file and process group."""

    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path
    _proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    def start(self) -> None:
        """Fork the process into its own session, logging to ``log_path``."""
        if self._proc is not None:
            raise RuntimeError(f"process {self.name!r} already started")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = self.log_path.open("wb")
        # The file handle is owned by the child's stdout/stderr; closing it here
        # is safe because the OS dup'd it into the child. Keep a reference on the
        # handle object so the fd stays open for the child's lifetime.
        self._log_file = log_file
        self._proc = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            env=self.env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        # Capture the process-group id while the master is alive: a new session
        # makes pgid == pid. Teardown signals this group even if the master has
        # since exited, so orphaned uvicorn workers cannot linger holding a port.
        self._pgid = os.getpgid(self._proc.pid)

    @property
    def pid(self) -> int:
        if self._proc is None:
            raise RuntimeError(f"process {self.name!r} not started")
        return self._proc.pid

    def poll(self) -> int | None:
        """Return the exit code if the process has exited, else ``None``."""
        if self._proc is None:
            raise RuntimeError(f"process {self.name!r} not started")
        return self._proc.poll()

    def is_running(self) -> bool:
        return self.poll() is None

    def log_tail(self, lines: int = 80) -> str:
        """The last ``lines`` lines of the process log, for failure reports."""
        if not self.log_path.exists():
            return "<no log file>"
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def terminate(self, *, sigterm_grace: float = 10.0) -> None:
        """Stop the whole process group: SIGTERM, wait up to ``sigterm_grace``,
        then SIGKILL. Idempotent; a process already gone is not an error."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._signal_group(signal.SIGTERM)
            try:
                self._proc.wait(timeout=sigterm_grace)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                self._proc.wait(timeout=sigterm_grace)
        else:
            # The master already exited on its own; SIGKILL the group anyway so any
            # workers it forked are reaped rather than left orphaned on a port.
            self._signal_group(signal.SIGKILL)
        # A child that put ITSELF in a new process group (rq's ``os.setpgrp``
        # work-horse) is outside the group SIGKILL above but still inside this
        # master's SESSION. Reap those survivors too, so no SUT child outlives
        # teardown to write into a Redis logical DB / PG database the harness has
        # since returned to the pool — the exact cross-stack contamination the
        # isolation contract forbids. Done BEFORE the final ``wait`` reaps the master,
        # so the master's pid (== the session id) stays reserved and cannot be reused
        # by an unrelated process that would then match the session filter.
        self._reap_session_survivors()
        # Reap and release the log fd.
        self._proc.wait()
        log_file = getattr(self, "_log_file", None)
        if log_file is not None and not log_file.closed:
            log_file.close()

    def kill_now(self) -> None:
        """SIGKILL the group immediately (for the dead-worker tests)."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._signal_group(signal.SIGKILL)
        self._proc.wait()
        log_file = getattr(self, "_log_file", None)
        if log_file is not None and not log_file.closed:
            log_file.close()

    def _signal_group(self, sig: int) -> None:
        # Signal by the pgid captured at start (== the master pid), so the whole
        # group is reachable even after the master itself has been reaped. A gone
        # group raises ProcessLookupError, which means there is nothing to signal.
        pgid = getattr(self, "_pgid", None)
        if pgid is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)

    def _reap_session_survivors(self, *, deadline: float = 10.0) -> None:
        """SIGKILL every process still in this master's SESSION after the group kill,
        then BLOCK until they are actually gone.

        ``start_new_session=True`` makes the master a session leader (sid == pid ==
        pgid). A child that re-groups itself (``setpgrp``) escapes the group SIGKILL
        but stays in the session, so it is found here by session id. SIGKILL is
        asynchronous and the harness does not parent these survivors (uvicorn worker
        children, an rq work-horse) — init reaps them a beat after the kill — so this
        must WAIT for the session to drain before returning: otherwise teardown releases
        the stack's Redis DB while a worker still heartbeats its ``bus:presence`` key on
        it, and the next stack to lease that index trips the live-orphan guard. A
        survivor still alive at ``deadline`` raises loudly naming its pid rather than
        leaving a silent leak.
        """
        sid = getattr(self, "_pgid", None)
        if sid is None:
            return
        master_pid = self._proc.pid if self._proc is not None else None

        def survivors() -> list[int]:
            found: list[int] = []
            for pid in _all_pids():
                if pid == master_pid:
                    continue  # the master itself is reaped by wait(); leave its pid reserved
                try:
                    if os.getsid(pid) != sid:
                        continue
                except (ProcessLookupError, PermissionError):
                    continue
                found.append(pid)
            return found

        for pid in survivors():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        try:
            wait_for(
                lambda: not survivors(),
                deadline=deadline,
                interval=0.05,
                message=f"process {self.name!r}: session worker(s) still alive after SIGKILL",
            )
        except WaitTimeoutError as exc:
            raise RuntimeError(
                f"process {self.name!r}: session worker(s) still alive {deadline:.0f}s after SIGKILL "
                f"(pid(s) {survivors()}) — teardown cannot release the stack's infra under a live worker"
            ) from exc


def _all_pids() -> list[int]:
    """Every live pid on the host — the census the session-survivor reap filters
    by ``os.getsid``. Read from ``/proc`` where it exists (Linux); on hosts
    without it (macOS) from ``ps -axo pid=``, raising loudly if ``ps`` fails
    (a silent empty census would silently skip the leak reap)."""
    if os.path.isdir("/proc"):
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    listing = subprocess.run(["ps", "-axo", "pid="], capture_output=True, text=True, check=True)
    return [int(token) for token in listing.stdout.split()]
