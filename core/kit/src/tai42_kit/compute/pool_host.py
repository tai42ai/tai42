"""The pool host: a process of OS threads that runs the compute pool of a process a prelude reshaped.

The owner, a process started through a process prelude or forked at any remove from one, that
reaches no host, starts its host on its first compute job (:class:`PoolHost`) and is a
:class:`~tai42_kit.compute.pool_wire.ComputePoolClient` of it over one end of a socket pair; the
host is a fresh interpreter, started with the owner's interpreter flags, its environment less the
prelude variable and its module search path, that calls the serving function the owner names,
which serves the other end (:func:`serve_pool`) and runs each job in a pool of its own. The
search path reaches the host on its standard input, a socket pair of its own made for the start;
the serving function, on its command line by its ``module:qualname``. The owner runs its jobs
through a :class:`HostedPool`, which sends each job to the pool host the process reaches when the
job is sent: the one it started, or the one it kept an end of at its fork. The owner replaces a
host whose closed end refused a job, and that job is sent once to the fresh one; a host a process
reaches and did not start is never replaced.

The host ends when the owner stops it (``SIGTERM``), and when the owner, and every forked
descendant of it that holds its end, ended, whatever ended them: the host then reads its end of the socket pair
closed. Either way it stops its pool, whose workers end with it. A terminal interrupt is the
owner's to handle; the host ignores it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable

from tai42_contract.backend import PROCESS_PRELUDE_ENV

from tai42_kit.compute.errors import ComputePoolUnavailableError
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.pool_wire import ComputePoolClient, JobNotSentError, PoolServer
from tai42_kit.compute.wire_sockets import wire_sockets

# Run with ``-c``: run as ``-m``, a module would execute a second time as ``__main__``. The
# owner's module search path, read whole from the standard input, is set before the serving
# function is resolved, so the host imports what the owner does.
_HOST_COMMAND = (
    "import json, pkgutil, sys; sys.path[:] = json.loads(sys.stdin.buffer.read()); "
    "pkgutil.resolve_name(sys.argv[1])(int(sys.argv[2]))"
)


class PoolHost:
    """The owner's side: the host process it started, and the client that reaches its pool."""

    def __init__(self, serve: Callable[[int], None]) -> None:
        """Start the host process, which calls ``serve`` with the number of its end of the socket pair.

        The host resolves ``serve`` by its module and qualified name; it serves that end
        through :func:`serve_pool`.

        Raises :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError` when it cannot start
        (its sockets cannot be made, as at the process's descriptor limit, or its process cannot
        start). Anything else that interrupts the start propagates as itself. Either way the
        owner's end is closed, and a host process whose start returned is ended and reaped.
        """
        mine: socket.socket | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            mine, hosts = wire_sockets.socketpair(second_for_a_spawn=True)
            try:
                process = _started_host_process(f"{serve.__module__}:{serve.__qualname__}", hosts.fileno())
            finally:
                wire_sockets.close(hosts)
            self.client = ComputePoolClient(mine)
            # From here a child the process forks reaches this host.
            wire_sockets.keep_in_children(mine)
            self._process = process
        except BaseException as exc:
            if mine is not None:
                wire_sockets.close(mine)
            if process is not None:
                _ended(process)
            if isinstance(exc, OSError):
                raise ComputePoolUnavailableError("the compute pool host did not start") from exc
            raise

    def stop(self) -> None:
        """End the host and the pool it runs, and wait for it; a job in flight raises in its caller."""
        self.client.close()
        self._process.terminate()
        self._process.wait()


def _started_host_process(serve: str, rendezvous: int) -> subprocess.Popen[bytes]:
    """Start the host, which calls ``serve`` with the socket end ``rendezvous``, and send it the search path.

    Raises :class:`OSError` when the host cannot start or its end of the socket refused the path.
    Whatever is raised once the host process's start returned, the host is ended and reaped first.
    """
    path_end, hosts_input = wire_sockets.socketpair(second_for_a_spawn=True)
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            try:
                # The host reads its input as a plain interpreter; the owner's prelude may have made
                # the process's sockets non-blocking.
                os.set_blocking(hosts_input.fileno(), True)
                process = subprocess.Popen(  # noqa: S603 fixed, trusted argv; no shell and no user input
                    [
                        sys.executable,
                        # The interpreter flags multiprocessing passes its own children.
                        *subprocess._args_from_interpreter_flags(),  # pyright: ignore[reportAttributeAccessIssue]
                        "-c",
                        _HOST_COMMAND,
                        serve,
                        str(rendezvous),
                    ],
                    pass_fds=[rendezvous],
                    env={name: value for name, value in os.environ.items() if name != PROCESS_PRELUDE_ENV},
                    stdin=hosts_input.fileno(),
                )
            finally:
                wire_sockets.close(hosts_input)
            # The import system reads only the ``str`` entries of the search path.
            path_end.sendall(json.dumps([entry for entry in sys.path if isinstance(entry, str)]).encode())
            return process
        finally:
            wire_sockets.close(path_end)
    except BaseException:
        if process is not None:
            _ended(process)
        raise


def _ended(process: subprocess.Popen[bytes]) -> None:
    """End the host ``process`` and reap it."""
    process.kill()
    process.wait()


class HostedPool:
    """The owner's side of its jobs: each runs in the pool host the process reaches when the job is sent."""

    def __init__(self, current_client: Callable[[], ComputePoolClient]) -> None:
        """Send jobs to the client ``current_client`` returns.

        It returns the client of the pool host the process reaches: the one it started, which it
        starts or replaces as needed, or the one it kept an end of at its fork.
        """
        self._current_client = current_client

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], /, *args: *Ts, timeout: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker of the pool host the process reaches and return its result.

        The contract of :meth:`~tai42_kit.compute.pool_wire.ComputePoolClient.run`. A job the
        host's closed end refused never reached it, so it is sent once more, to the host the
        process reaches then; a second refusal raises.
        """
        client = self._current_client()
        try:
            return await client.run(fn, *args, timeout=timeout)
        except JobNotSentError:
            client = self._current_client()
        return await client.run(fn, *args, timeout=timeout)


def serve_pool(fd: int, own_pool: Callable[[], ComputePool], stop_pool: Callable[[], None]) -> None:
    """The host's side: serve the socket end ``fd`` until ``SIGTERM`` or its closing, then stop the pool.

    ``own_pool`` returns the pool a job runs in; ``stop_pool`` stops it. Anything else that ends
    the serving propagates: the host exits non-zero, its traceback on the stderr it shares with its
    owner, and the owner's jobs raise :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError`.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    asyncio.run(_serve_until_terminated(PoolServer(wire_sockets.adopted(fd), own_pool), stop_pool))


async def _serve_until_terminated(server: PoolServer, stop_pool: Callable[[], None]) -> None:
    # The pool is stopped while the loop runs: until the loop closes, its ``SIGTERM`` handler
    # stands in for the signal's default action, which would end the host before its workers.
    try:
        serving = asyncio.ensure_future(server.serve())
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, serving.cancel)
        await asyncio.wait({serving})
        if not serving.cancelled():  # cancelled is the SIGTERM's stop
            serving.result()
    finally:
        try:
            server.close()
        finally:
            stop_pool()
