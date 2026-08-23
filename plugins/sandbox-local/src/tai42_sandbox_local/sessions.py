"""The host-subprocess session and its interactive exec handle.

``LocalSandboxSession`` implements only the runtime I/O the contract asks of a
:class:`~tai42_contract.sandbox.SandboxSession` — ``workspace_path``, ``exec``,
``exec_start``, ``put_file``, ``get_file`` — over a plain host subprocess and the
host filesystem; the ledger/TTL bookkeeping (``id`` / ``info`` / ``touch`` /
``destroy``) is inherited from :class:`~tai42_kit.sandbox.ManagedSandboxSession`.

SECURITY INVARIANTS held here:

- CLEAN env — a subprocess is spawned with a fixed ``PATH`` base plus the session's
  ``spec.env`` and any per-exec ``env`` overlay, NEVER the host ``os.environ``. A
  ``SecretStr`` value is unwrapped ONLY at the spawn call, never into a repr/log/error.
- REALPATH CONTAINMENT — a workspace-relative ``cwd`` / ``put_file`` / ``get_file``
  path resolves against ``workspace_path`` and a resolved path escaping the workspace
  root raises loudly, so a ``..`` component cannot walk out of the workspace.
- LOUD failure — every host error surfaces as a typed
  :class:`~tai42_contract.sandbox.SandboxError`; the only swallowed cases are the
  contract's two idempotent no-ops (already-exited process on kill, already-gone
  directory on destroy).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxDurability,
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox import ManagedSandboxSession

if TYPE_CHECKING:
    from tai42_sandbox_local.provider import LocalSandbox

# Read granularity for draining a pipe. A fixed chunk keeps memory bounded while
# a reader keeps pace with a fast producer.
_READ_CHUNK = 65536


def _returncode(proc: asyncio.subprocess.Process) -> int:
    """The exit code of an already-exited process (an unfinished process is a
    programming error at every call site here, which all await the process first)."""
    code = proc.returncode
    if code is None:  # pragma: no cover - defensive; every caller awaits exit first
        raise SandboxError("sandbox exec exit code read before the process exited")
    return code


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group, reaping grandchildren too.

    The child was spawned with ``start_new_session=True``, so its process-group id
    equals its pid. Idempotent: an already-exited process is a no-op, and a racing
    reap surfaces as :class:`ProcessLookupError`, swallowed."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


class LocalSandboxExecHandle(SandboxExecHandle):
    """A live interactive host subprocess started by :meth:`LocalSandboxSession.exec_start`.

    A host subprocess exposes SEPARATE stdin/stdout/stderr pipes, so a write never
    interleaves with a read and the concurrency contract is met without demuxing one
    shared attach stream. Two reader tasks drain stdout and stderr concurrently into
    one queue; ``output`` yields each frame and one terminal
    :class:`SandboxStreamExit`. A total ``timeout_seconds`` deadline bounds the whole
    interactive exec; on expiry the process group is killed and the iterator raises
    :class:`SandboxExecTimeoutError`.
    """

    def __init__(self, proc: asyncio.subprocess.Process, *, timeout_seconds: float) -> None:
        self._proc = proc
        self._timeout_seconds = timeout_seconds
        # Serializes concurrent write_stdin calls so a single call's bytes are never
        # interleaved with another's; the provider does no framing beyond this.
        self._write_lock = asyncio.Lock()
        self._output_iter: AsyncIterator[SandboxStreamChunk | SandboxStreamExit] | None = None
        # Cumulative bytes seen per stream, for the timeout error's LENGTHS (never content).
        self._stdout_len = 0
        self._stderr_len = 0

    async def write_stdin(self, data: bytes) -> None:
        async with self._write_lock:
            stdin = self._proc.stdin
            if stdin is None or self._proc.returncode is not None or stdin.is_closing():
                raise SandboxError("sandbox exec stdin is closed or the exec has exited; cannot write")
            try:
                stdin.write(data)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError, ProcessLookupError) as exc:
                raise SandboxError(f"sandbox exec stdin write failed: {exc}") from exc

    async def close_stdin(self) -> None:
        stdin = self._proc.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()

    @property
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        if self._output_iter is None:
            self._output_iter = self._stream_output()
        return self._output_iter

    async def kill(self) -> None:
        _kill_process_group(self._proc)

    async def _stream_output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        queue: asyncio.Queue[SandboxStreamChunk | None] = asyncio.Queue()

        async def _reader(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:  # pragma: no cover - exec_start always opens both pipes
                return
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                if name == "stdout":
                    self._stdout_len += len(chunk)
                else:
                    self._stderr_len += len(chunk)
                await queue.put(SandboxStreamChunk(stream="stdout" if name == "stdout" else "stderr", data=chunk))

        async def _drive() -> None:
            await asyncio.gather(_reader(self._proc.stdout, "stdout"), _reader(self._proc.stderr, "stderr"))
            await self._proc.wait()
            await queue.put(None)

        driver = asyncio.ensure_future(_drive())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._timeout(driver)
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    await self._timeout(driver)
                if item is None:
                    break
                yield item
            yield SandboxStreamExit(exit_code=_returncode(self._proc))
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

    async def _timeout(self, driver: asyncio.Future[None]) -> None:
        """Kill the process group and raise the timeout error carrying LENGTHS only."""
        await self.kill()
        with contextlib.suppress(ProcessLookupError):
            await self._proc.wait()
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver
        raise SandboxExecTimeoutError(
            timeout_seconds=self._timeout_seconds,
            stdout_len=self._stdout_len,
            stderr_len=self._stderr_len,
        )


class LocalSandboxSession(ManagedSandboxSession):
    """One host-subprocess session over a workspace directory on the host filesystem."""

    def __init__(
        self,
        *,
        sandbox: LocalSandbox,
        session_id: str,
        workspace_path: str,
        durability: SandboxDurability,
        base_path: str,
        spec_env: dict[str, SecretStr],
        teardown_dir: str,
        sidecar_path: str | None,
    ) -> None:
        super().__init__(sandbox=sandbox, session_id=session_id)
        # The ABSOLUTE, realpath-resolved workspace root (equal to the child's cwd for
        # an unset cwd, and the anchor for workspace-relative path resolution).
        self._workspace_path = workspace_path
        self._durability: SandboxDurability = durability
        self._base_path = base_path
        self._spec_env = dict(spec_env)
        # The directory to remove on teardown (the workspace for ephemeral, the named
        # persistent dir otherwise) and the sidecar to remove alongside a persistent one.
        self._teardown_dir = teardown_dir
        self._sidecar_path = sidecar_path
        self._handles: list[LocalSandboxExecHandle] = []

    @property
    def workspace_path(self) -> str:
        return self._workspace_path

    @property
    def durability(self) -> SandboxDurability:
        return self._durability

    @property
    def teardown_dir(self) -> str:
        return self._teardown_dir

    @property
    def sidecar_path(self) -> str | None:
        return self._sidecar_path

    # -- env + path resolution ----------------------------------------------------

    def _build_env(self, env: dict[str, SecretStr] | None) -> dict[str, str]:
        """The CLEAN subprocess env: the fixed ``PATH`` base, overlaid by ``spec.env``,
        overlaid by the per-exec ``env`` (per-exec keys win, a supplied ``PATH`` wins
        over the base). SecretStr values are unwrapped ONLY here, never logged."""
        merged: dict[str, str] = {"PATH": self._base_path}
        for key, value in self._spec_env.items():
            merged[key] = value.get_secret_value()
        if env:
            for key, value in env.items():
                merged[key] = value.get_secret_value()
        return merged

    def _resolve_cwd(self, cwd: str | None) -> str:
        """The child's working directory. Unset defaults to the workspace root; a
        relative value resolves WITHIN the workspace with realpath containment; an
        ABSOLUTE value is allowed as given — this provider is isolation=none, so the
        subprocess already has full host reach and containing an absolute cwd adds no
        security."""
        if cwd is None:
            return self._workspace_path
        if os.path.isabs(cwd):
            return cwd
        return self._contained_path(cwd)

    def _contained_path(self, path: str) -> str:
        """Resolve a workspace-relative ``path`` against the workspace root and REJECT
        a resolved path that escapes it (realpath containment)."""
        root = self._workspace_path
        resolved = os.path.realpath(os.path.join(root, path))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SandboxError(f"path {path!r} escapes the sandbox workspace")
        return resolved

    # -- exec ---------------------------------------------------------------------

    async def _spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdin: bool,
    ) -> asyncio.subprocess.Process:
        """Spawn a host subprocess in its own session/process-group. A spawn failure
        (a missing binary, an unwritable cwd) raises a typed error NAMING the binary,
        never any env value."""
        if not argv:
            raise SandboxError("sandbox exec requires a non-empty argv")
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxError(f"sandbox exec could not spawn {argv[0]!r}: {exc}") from exc

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        proc = await self._spawn(
            argv,
            cwd=self._resolve_cwd(cwd),
            env=self._build_env(env),
            stdin=stdin is not None,
        )
        stdout_buf = bytearray()
        stderr_buf = bytearray()

        async def _pump(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
            if stream is None:  # pragma: no cover - both pipes are always opened
                return
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                buf.extend(chunk)

        async def _communicate() -> None:
            pumps = asyncio.gather(_pump(proc.stdout, stdout_buf), _pump(proc.stderr, stderr_buf))
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            await pumps
            await proc.wait()

        try:
            await asyncio.wait_for(_communicate(), timeout=timeout_seconds)
        except TimeoutError:
            _kill_process_group(proc)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            raise SandboxExecTimeoutError(
                timeout_seconds=timeout_seconds,
                stdout_len=len(stdout_buf),
                stderr_len=len(stderr_buf),
            ) from None
        return ExecResult(
            exit_code=_returncode(proc),
            stdout=stdout_buf.decode("utf-8", "replace"),
            stderr=stderr_buf.decode("utf-8", "replace"),
        )

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        proc = await self._spawn(
            argv,
            cwd=self._resolve_cwd(cwd),
            env=self._build_env(env),
            stdin=True,
        )
        handle = LocalSandboxExecHandle(proc, timeout_seconds=timeout_seconds)
        self._handles.append(handle)
        return handle

    # -- file transfer ------------------------------------------------------------

    async def put_file(self, path: str, data: bytes) -> None:
        """Write ``data`` under the workspace. UNLIKE ``cwd``, file transfer is I/O the
        PROVIDER performs on the host, so realpath containment is enforced
        unconditionally: an absolute ``path`` built from ``workspace_path`` passes, but
        an absolute ``path`` resolving OUTSIDE the workspace is refused loudly because on
        a host provider it would be a real host write (a permitted tightening — see the
        contract path invariant)."""
        target = Path(self._contained_path(path))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise SandboxError(f"sandbox put_file failed for {path!r}: {exc}") from exc

    async def get_file(self, path: str) -> bytes:
        """Read ``path`` from the workspace. Containment is enforced unconditionally for
        the same reason as :meth:`put_file`: this provider performs a real host read, so
        an absolute ``path`` built from ``workspace_path`` passes while one resolving
        OUTSIDE the workspace is refused loudly."""
        target = Path(self._contained_path(path))
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise SandboxError(f"sandbox get_file miss for {path!r}") from exc
        except OSError as exc:
            raise SandboxError(f"sandbox get_file failed for {path!r}: {exc}") from exc

    # -- teardown -----------------------------------------------------------------

    async def terminate_handles(self) -> None:
        """Kill every live interactive subprocess this session started (idempotent on
        an already-exited process). Called by the provider teardown primitive."""
        for handle in self._handles:
            await handle.kill()
