"""The Docker session and its interactive exec handle — the provider's runtime I/O.

A :class:`DockerSandboxSession` runs ``exec`` / ``exec_start`` and file transfers
against ONE engine container; the kit :class:`~tai42_kit.sandbox.ManagedSandboxSession`
base it extends owns ``id`` / ``info()`` / ``touch()`` / ``destroy()`` (all routed
through the owning sandbox's ledger), so this module carries only the container I/O.

Every ``timeout_seconds`` is enforced HOST-SIDE (an ``asyncio`` deadline around the
attach/drain), never by trusting the engine: on expiry the exec's process is killed
and a :class:`SandboxExecTimeoutError` is raised carrying the partial-output LENGTHS
only. A ``spec.env`` value is a :class:`~pydantic.SecretStr` unwrapped ONLY at the
engine call and never placed in a repr, log, or error message.
"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import socket
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox import ManagedSandboxSession

if TYPE_CHECKING:
    from tai42_kit.sandbox import ManagedSandbox

# The absolute container path the session workspace mounts at. A workspace-RELATIVE
# ``cwd`` / ``path`` resolves under here; an absolute value is passed to the engine
# as given (the caller's own responsibility, per the contract path rule).
WORKSPACE_PATH = "/workspace"

# The Docker exec multiplex frame stream ids (see the Docker Engine attach "Stream
# Format"): 1 is stdout, 2 is stderr.
_STREAM_STDERR = 2

# Bound on the post-EOF exit-code poll: the attach stream has ended, so the exec is
# finishing; a few short reads cover the brief window before the engine records the
# exit code, without an unbounded wait.
_EXIT_CODE_POLLS = 50
_EXIT_CODE_POLL_INTERVAL = 0.02


def engine_error(exc: DockerError) -> SandboxError:
    """Wrap an engine :class:`DockerError` as a typed :class:`SandboxError`.

    Carries the engine's own status and message (never any ``spec.env`` value — the
    engine message describes the API failure, not the credential channel)."""
    return SandboxError(f"docker engine error [{exc.status}]: {exc.message}")


def resolve_workspace_path(path: str) -> str:
    """Resolve a ``cwd`` / file ``path`` against the workspace root.

    A relative value resolves under :data:`WORKSPACE_PATH` with containment enforced
    (an escaping ``..`` is a loud :class:`SandboxError`); an absolute value is returned
    unchanged as the caller's own responsibility (the contract path rule)."""
    if path.startswith("/"):
        return path
    resolved = posixpath.normpath(posixpath.join(WORKSPACE_PATH, path))
    if resolved != WORKSPACE_PATH and not resolved.startswith(WORKSPACE_PATH + "/"):
        raise SandboxError(f"path {path!r} escapes the workspace root {WORKSPACE_PATH!r}")
    return resolved


def merge_env(base: Mapping[str, SecretStr], overlay: Mapping[str, SecretStr] | None) -> dict[str, str]:
    """Merge the session's base ``spec.env`` with a per-exec overlay, unwrapping each
    secret ONLY here at the engine call. Per-exec keys override the base on collision."""
    merged = {key: value.get_secret_value() for key, value in base.items()}
    if overlay:
        merged.update({key: value.get_secret_value() for key, value in overlay.items()})
    return merged


async def read_exit_code(exec_obj: Any) -> int:
    """Read the finished exec's exit code, polling briefly past the attach EOF."""
    for _ in range(_EXIT_CODE_POLLS):
        info = await exec_obj.inspect()
        if not info.get("Running", False) and info.get("ExitCode") is not None:
            return int(info["ExitCode"])
        await asyncio.sleep(_EXIT_CODE_POLL_INTERVAL)
    raise SandboxError("sandbox exec ended without reporting an exit code")


async def half_close_stdin(stream: Any) -> None:
    """Half-close the write (stdin) side of a live attach stream, leaving the read
    side open so remaining output still drains — this is what delivers EOF to the
    in-session process, so a ``cat``-style reader exits instead of hanging.

    The aiodocker ``Stream`` exposes only a full ``close()`` (which tears down the
    read side too), so the half-close is issued on the underlying transport directly
    (the ``aiodocker~=0.27`` pin fixes this shape):

    - Plain TCP: ``write_eof()`` sends a FIN on the write half — the clean half-close.
    - mTLS (``tcp://`` normalized to ``https://``): an asyncio SSL transport reports
      ``can_write_eof() == False`` (TLS has no half-close), so ``write_eof()`` never
      fires and the process's stdin never sees EOF. Signal it by shutting the WRITE
      half of the UNDERLYING socket: the daemon reads a FIN on the hijacked stream
      and closes the process's stdin, while the socket's read half stays open so the
      remaining stdout/stderr still drains. This is how the docker CLI half-closes an
      interactive exec over a TLS control API."""
    resp = stream._resp
    if resp is None or resp.connection is None:
        return
    transport = resp.connection.transport
    if transport is None:
        return
    if transport.can_write_eof():
        transport.write_eof()
        return
    sock = transport.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)


class DockerSandboxExecHandle(SandboxExecHandle):
    """A live interactive exec attached over one demultiplexed engine stream.

    Reads and writes ride ONE attach stream: a write lock keeps a ``write_stdin``
    from interleaving with another and never splits a call's bytes, while reads drain
    concurrently. The ``timeout_seconds`` deadline is enforced host-side around the
    output drain; on expiry the exec is killed and the iterator raises
    :class:`SandboxExecTimeoutError`.
    """

    def __init__(self, *, container: Any, exec_obj: Any, stream: Any, timeout_seconds: float) -> None:
        self._container = container
        self._exec = exec_obj
        self._stream = stream
        self._timeout_seconds = timeout_seconds
        self._write_lock = asyncio.Lock()
        self._finished = False
        self._output_iter: AsyncIterator[SandboxStreamChunk | SandboxStreamExit] | None = None
        self._stdout_len = 0
        self._stderr_len = 0

    async def write_stdin(self, data: bytes) -> None:
        if self._finished:
            raise SandboxError("cannot write stdin: the sandbox exec has exited")
        async with self._write_lock:
            try:
                await self._stream.write_in(data)
            except RuntimeError as exc:
                # aiodocker raises RuntimeError once the transport is closed.
                self._finished = True
                raise SandboxError("cannot write stdin: the sandbox exec stream is closed") from exc
            except DockerError as exc:
                raise engine_error(exc) from exc

    async def close_stdin(self) -> None:
        if self._finished:
            return
        async with self._write_lock:
            await self._stream._init()
            await half_close_stdin(self._stream)

    @property
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        if self._output_iter is None:
            self._output_iter = self._iter_output()
        return self._output_iter

    async def _iter_output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        async with self._stream:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self.kill()
                    raise self._timeout_error()
                try:
                    message = await asyncio.wait_for(self._stream.read_out(), remaining)
                except TimeoutError as exc:
                    await self.kill()
                    raise self._timeout_error() from exc
                if message is None:
                    break
                if message.stream == _STREAM_STDERR:
                    self._stderr_len += len(message.data)
                    yield SandboxStreamChunk(stream="stderr", data=message.data)
                else:
                    self._stdout_len += len(message.data)
                    yield SandboxStreamChunk(stream="stdout", data=message.data)
        self._finished = True
        yield SandboxStreamExit(exit_code=await read_exit_code(self._exec))

    def _timeout_error(self) -> SandboxExecTimeoutError:
        return SandboxExecTimeoutError(
            timeout_seconds=self._timeout_seconds,
            stdout_len=self._stdout_len,
            stderr_len=self._stderr_len,
        )

    async def kill(self) -> None:
        if self._finished:
            return
        info = await _inspect_exec(self._exec)
        if info is not None and not info.get("Running", True):
            # The exec has already exited: kill is an idempotent no-op.
            self._finished = True
            return
        pid = info.get("Pid") if info else None
        await kill_exec_process(self._container, int(pid) if pid else None)
        self._finished = True


async def _inspect_exec(exec_obj: Any) -> dict[str, Any] | None:
    try:
        return await exec_obj.inspect()
    except DockerError:
        return None


async def _signal_pid(container: Any, pid: int) -> bool:
    """Kill the exec's process via a sibling ``kill`` exec. Returns whether it ran."""
    try:
        killer = await container.exec(
            cmd=["kill", "-KILL", str(pid)],
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
        )
        async with killer.start(detach=False) as stream:
            while await stream.read_out() is not None:
                pass
    except DockerError:
        return False
    return True


async def kill_exec_process(container: Any, pid: int | None) -> None:
    """Terminate an exec's process, falling back to killing the container's main
    process (the documented last resort) only when the process cannot be signalled."""
    if pid is not None and await _signal_pid(container, pid):
        return
    try:
        await container.kill(signal="SIGKILL")
    except DockerError as exc:
        if exc.status not in (404, 409):
            raise engine_error(exc) from exc


class DockerSandboxSession(ManagedSandboxSession):
    """One live session bound to a single engine container."""

    def __init__(
        self,
        *,
        sandbox: ManagedSandbox,
        session_id: str,
        container: Any,
        workspace_key: str,
        durability: str,
        base_env: Mapping[str, SecretStr],
    ) -> None:
        super().__init__(sandbox=sandbox, session_id=session_id)
        self._container = container
        self._workspace_key = workspace_key
        self._durability = durability
        self._base_env = dict(base_env)

    @property
    def workspace_path(self) -> str:
        return WORKSPACE_PATH

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        exec_obj = await self._make_exec(argv, cwd=cwd, env=env, stdin=stdin is not None)
        stdout = bytearray()
        stderr = bytearray()
        try:
            exit_code = await asyncio.wait_for(self._drain(exec_obj, stdin, stdout, stderr), timeout_seconds)
        except TimeoutError as exc:
            await self._kill_exec(exec_obj)
            raise SandboxExecTimeoutError(
                timeout_seconds=timeout_seconds,
                stdout_len=len(stdout),
                stderr_len=len(stderr),
            ) from exc
        return ExecResult(
            exit_code=exit_code,
            stdout=bytes(stdout).decode("utf-8", "replace"),
            stderr=bytes(stderr).decode("utf-8", "replace"),
        )

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> DockerSandboxExecHandle:
        exec_obj = await self._make_exec(argv, cwd=cwd, env=env, stdin=True)
        stream = exec_obj.start(detach=False)
        return DockerSandboxExecHandle(
            container=self._container,
            exec_obj=exec_obj,
            stream=stream,
            timeout_seconds=timeout_seconds,
        )

    async def put_file(self, path: str, data: bytes) -> None:
        target = resolve_workspace_path(path)
        # docker's put_archive extracts a tar at ``base`` and requires that dir to
        # exist. For a workspace path we extract at the workspace ROOT with the member
        # path RELATIVE to it, and the tar carries its parent-dir entries, so a nested
        # ``sub/nested.txt`` creates ``sub/`` instead of 404-ing on the missing dir.
        if target == WORKSPACE_PATH or target.startswith(WORKSPACE_PATH + "/"):
            base = WORKSPACE_PATH
            member = target[len(WORKSPACE_PATH) :].lstrip("/")
        else:
            # An absolute path outside the workspace: its parent is the caller's own
            # responsibility (the contract path rule), matching prior behavior.
            base, member = posixpath.split(target)
            base = base or "/"
        archive = _tar_single_member(member, data)
        try:
            await self._container.put_archive(base, archive)
        except DockerError as exc:
            raise engine_error(exc) from exc

    async def get_file(self, path: str) -> bytes:
        target = resolve_workspace_path(path)
        try:
            tar = await self._container.get_archive(target)
        except DockerError as exc:
            if exc.status == 404:
                raise SandboxError(f"sandbox file {path!r} not found") from exc
            raise engine_error(exc) from exc
        member = posixpath.basename(target)
        extracted = tar.extractfile(member)
        if extracted is None:
            raise SandboxError(f"sandbox path {path!r} is not a readable file")
        return extracted.read()

    async def _make_exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: dict[str, SecretStr] | None,
        stdin: bool,
    ) -> Any:
        workdir = resolve_workspace_path(cwd) if cwd is not None else WORKSPACE_PATH
        environment = merge_env(self._base_env, env)
        try:
            return await self._container.exec(
                cmd=list(argv),
                stdin=stdin,
                stdout=True,
                stderr=True,
                tty=False,
                workdir=workdir,
                environment=environment,
            )
        except DockerError as exc:
            raise engine_error(exc) from exc

    async def _drain(self, exec_obj: Any, stdin: bytes | None, stdout: bytearray, stderr: bytearray) -> int:
        stream = exec_obj.start(detach=False)
        async with stream:
            if stdin is not None:
                await stream.write_in(stdin)
                await half_close_stdin(stream)
            while True:
                message = await stream.read_out()
                if message is None:
                    break
                if message.stream == _STREAM_STDERR:
                    stderr.extend(message.data)
                else:
                    stdout.extend(message.data)
        return await read_exit_code(exec_obj)

    async def _kill_exec(self, exec_obj: Any) -> None:
        """Best-effort kill of a timed-out one-shot exec's process."""
        info = await _inspect_exec(exec_obj)
        pid = info.get("Pid") if info else None
        await kill_exec_process(self._container, int(pid) if pid else None)


def _tar_single_member(name: str, data: bytes) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        # Parent-directory entries first, so extracting a nested member creates its
        # intermediate dirs (``sub/`` for ``sub/nested.txt``).
        prefix = ""
        for part in name.split("/")[:-1]:
            if not part:
                continue
            prefix = f"{prefix}{part}/"
            directory = tarfile.TarInfo(name=prefix)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            tar.addfile(directory)
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
