"""A compute pool reached over a socket: the wire and its two ends.

The serving process holds one end of a stream socket pair, the rendezvous; a client process holds
the other, inherited or passed. For each job the client makes a socket pair of its own, passes one
end over the rendezvous, writes the packed job on the other end and reads the job's outcome back.
The server runs the job in its pool under the job's timeout. A client that closes its job's end
(its task was cancelled, or it ended) cancels the job, which terminates the job's worker. A job
whose send the server's closed end refused (the server ended, or stopped serving) raises
:class:`JobNotSentError`, as it never reached the server, and the client records it
(:attr:`ComputePoolClient.server_ended`). A job whose socket closes with no reply (the server
ended, or could not take or serve the job) raises
:class:`~tai42_kit.compute.errors.ComputePoolUnavailableError` and records nothing, as a job a
live server could not take or serve gets no reply either. Nothing on the client's side uses
``multiprocessing``, so the client may be daemonic or carry a host library's process identity.
Every end either side makes, receives or closes goes through
:data:`~tai42_kit.compute.wire_sockets.wire_sockets`, so a child forked at any instant holds no
copy of a job's socket, within the one limit that module states: each job's own processes alone
hold it.

Each message is a frame: a 4-byte big-endian length, then a pickle. The client sends
``(payload, job_name, timeout)``; the server replies with the job's
:class:`~tai42_kit.compute.job.JobOutcome`, or with the exception the pool raised for it.
"""

from __future__ import annotations

import asyncio
import logging
import pickle
import socket
import struct
from collections.abc import Callable

from tai42_kit.compute.errors import ComputePoolUnavailableError, ComputeWorkerLostError
from tai42_kit.compute.job import JobOutcome, job_name, outcome_value, pack
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.wire_sockets import EndNotReceivedError, wire_sockets

logger = logging.getLogger(__name__)

_LENGTH = struct.Struct("!I")
# The pool's own failures, which reach the client as themselves.
_POOL_ERRORS = (TimeoutError, ComputeWorkerLostError, ComputePoolUnavailableError)


class PoolServer:
    """The serving end: accepts the job sockets clients pass over the rendezvous and runs their jobs."""

    def __init__(self, serving_end: socket.socket, own_pool: Callable[[], ComputePool]) -> None:
        """Serve on ``serving_end``; ``own_pool`` returns the pool a job runs in."""
        self._serving_end = serving_end
        self._serving_end.setblocking(False)
        self._own_pool = own_pool
        # The sockets of the jobs being served, closed when serving ended (``close``).
        self._connections: set[socket.socket] = set()

    async def serve(self, *, on_serving: Callable[[], None] | None = None) -> None:
        """Serve on the running loop until every client's end of the rendezvous is closed, or until cancelled.

        ``on_serving`` is called once the server accepts jobs. On the way out the jobs being served
        are cancelled and run to their end: a client's job in flight then reads its end closed. The
        loop and the served tasks are held by this call alone, never by the server, so a forked
        child that keeps the server never finalizes them.
        """
        loop = asyncio.get_running_loop()
        jobs: set[asyncio.Task[None]] = set()
        closed: asyncio.Future[None] = loop.create_future()
        loop.add_reader(self._serving_end, self._accept, loop, jobs, closed)
        try:
            if on_serving is not None:
                on_serving()
            await closed
        finally:
            loop.remove_reader(self._serving_end)
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.wait(set(jobs))

    def close(self) -> None:
        """Close the serving end and the job sockets being served; called once serving ended."""
        for connection in self._connections:
            wire_sockets.close(connection)
        self._connections.clear()
        wire_sockets.close(self._serving_end)

    def _accept(
        self, loop: asyncio.AbstractEventLoop, jobs: set[asyncio.Task[None]], closed: asyncio.Future[None]
    ) -> None:
        try:
            data, connections = wire_sockets.received(self._serving_end)
        except BlockingIOError:
            return  # another client's descriptor was taken first; the reader is called again for the next
        except EndNotReceivedError:
            # The job's socket no longer exists, so its client reads it closed and raises; the
            # message is consumed, so serving goes on with the next one.
            logger.error(  # noqa: TRY400 - the line names the cause; its traceback adds nothing
                "a compute job was not taken: this process has no file descriptor free for its socket; "
                "its caller raises"
            )
            return
        if not data and not connections:  # end of file: every client's end is closed
            loop.remove_reader(self._serving_end)
            if not closed.done():
                closed.set_result(None)
            return
        for connection in connections:
            connection.setblocking(False)
            self._connections.add(connection)
            task = loop.create_task(self._serve_job(connection))
            jobs.add(task)
            task.add_done_callback(jobs.discard)

    async def _serve_job(self, connection: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            try:
                request = await _read_frame(loop, connection)
            except EOFError:
                return  # the client ended before it sent its job: there is nothing to run
            job = asyncio.ensure_future(self._outcome(request))
            gone = asyncio.ensure_future(_until_closed(loop, connection))
            await asyncio.wait({job, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not job.done():
                job.cancel()
                await asyncio.wait({job})
                return
            gone.cancel()
            try:
                await loop.sock_sendall(connection, _frame(pickle.dumps(job.result())))
            except (BrokenPipeError, ConnectionResetError):
                return  # the client ended as its outcome was sent: nobody is left to read it
        except Exception:
            # The job's socket is closed below, so its client raises that it got no result.
            logger.exception("serving a compute job failed")
        finally:
            self._connections.discard(connection)
            wire_sockets.close(connection)

    async def _outcome(self, request: bytes) -> JobOutcome | Exception:
        name = "a compute job"
        try:
            payload, name, timeout = pickle.loads(request)  # noqa: S301 - sent by a client holding the rendezvous
            return await self._own_pool().run_packed(payload, name, timeout=timeout)
        except _POOL_ERRORS as exc:
            return exc
        except Exception as exc:  # any other failure to run the job reaches the client as the pool's
            return ComputePoolUnavailableError(f"the compute pool could not run {name}: {type(exc).__name__}: {exc}")


class JobNotSentError(ComputePoolUnavailableError):
    """The serving process's end of the rendezvous was closed when a job was sent: the job never reached it."""


class ComputePoolClient:
    """The client end: runs jobs in the pool of the process serving the rendezvous."""

    def __init__(self, rendezvous: socket.socket) -> None:
        """Send jobs to the serving process over ``rendezvous``."""
        self._rendezvous = rendezvous
        self._server_ended = False

    @property
    def server_ended(self) -> bool:
        """Whether a job's send was refused: the serving process's end of the rendezvous is closed."""
        return self._server_ended

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], /, *args: *Ts, timeout: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker of the serving process's pool and return its result.

        The same contract as :meth:`ComputePool.run`; the timeout and the termination of the
        worker happen in the serving process's pool, and a pool error raised there arrives here
        without its cause. A job the serving process's closed end refused raises
        :class:`JobNotSentError` and is recorded (:attr:`server_ended`); a job whose socket this
        process cannot make, or whose socket closes with no reply, raises
        :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError` and is not.
        """
        name = job_name(fn)
        request = _frame(pickle.dumps((pack(fn, args), name, timeout)))
        try:
            mine, theirs = wire_sockets.socketpair()
        except OSError as exc:  # as at this process's descriptor limit
            raise ComputePoolUnavailableError(
                f"this process could not make the socket for {name}; {name} cannot run"
            ) from exc
        try:
            gone = f"the process holding the compute pool is gone; {name} cannot run"
            try:
                socket.send_fds(self._rendezvous, [b"\0"], [theirs.fileno()])
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._server_ended = True
                raise JobNotSentError(gone) from exc
            except OSError as exc:  # this end closed, or a refusal of the kernel: nothing seen of the server
                raise ComputePoolUnavailableError(gone) from exc
            finally:
                wire_sockets.close(theirs)
            mine.setblocking(False)
            loop = asyncio.get_running_loop()
            try:
                await loop.sock_sendall(mine, request)
                reply = pickle.loads(await _read_frame(loop, mine))  # noqa: S301 - sent by the serving process
            except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
                raise ComputePoolUnavailableError(
                    f"the process holding the compute pool gave no result for {name}: "
                    "it ended, or it could not take or serve the job"
                ) from exc
        finally:
            # Closing the socket is the cancel: the server stops the job when it reads the end.
            wire_sockets.close(mine)
        if isinstance(reply, BaseException):
            raise reply
        return outcome_value(reply, name)

    def close(self) -> None:
        """Close the rendezvous: a later job raises that the pool is gone."""
        wire_sockets.close(self._rendezvous)


def _frame(data: bytes) -> bytes:
    return _LENGTH.pack(len(data)) + data


async def _read_frame(loop: asyncio.AbstractEventLoop, connection: socket.socket) -> bytes:
    """Read one frame; ``EOFError`` when the peer closes before the frame is whole."""
    (size,) = _LENGTH.unpack(await _read_exactly(loop, connection, _LENGTH.size))
    return await _read_exactly(loop, connection, size)


async def _read_exactly(loop: asyncio.AbstractEventLoop, connection: socket.socket, size: int) -> bytes:
    data = bytearray(size)
    view = memoryview(data)
    received = 0
    while received < size:
        count = await loop.sock_recv_into(connection, view[received:])
        if count == 0:
            raise EOFError(f"the peer closed after {received} of {size} bytes")
        received += count
    return bytes(data)


async def _until_closed(loop: asyncio.AbstractEventLoop, connection: socket.socket) -> None:
    """Return when the client closes its end; the client sends nothing after its job, so any readiness is that."""
    try:
        await loop.sock_recv(connection, 1)
    except ConnectionResetError:
        return  # a reset is the client's end closing too
