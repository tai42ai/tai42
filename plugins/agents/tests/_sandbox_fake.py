"""A self-contained fake sandbox provider for the agents tests.

Built to the contract :class:`~tai42_contract.sandbox.Sandbox` /
:class:`~tai42_contract.sandbox.SandboxSession` ABCs (through the kit
:class:`~tai42_kit.sandbox.ManagedSandbox` base), running each session's code as a
plain host subprocess in a temp workspace directory. The deep agent's
:class:`~tai42_agents.langchain_deep_agent.sandbox_backend.SandboxSessionBackend`
derives ls/read/glob/grep/edit/write from real ``sh``/``python3`` scripts run over
``exec``, so the fake must ACTUALLY execute — an in-memory stub could not exercise
those shell-derived file ops. Modeled on the direct-host provider but permissive
(it accepts every policy-resolved geometry) so a spec built from
``build_policied_spec`` always creates.

Deterministic and dependency-free: no docker, no real SDK. Persistent workspaces
survive a reap under a stable ``workspace_key`` (so a two-turn durability test can
reattach the same volume); ephemeral ones die with the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxPolicy,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox import ManagedSandbox, ManagedSandboxSession

_READ_CHUNK = 65536
# A fixed PATH base so a spawned subprocess never inherits the host environment.
_BASE_PATH = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def _returncode(proc: asyncio.subprocess.Process) -> int:
    code = proc.returncode
    if code is None:  # pragma: no cover - every caller awaits exit first
        raise SandboxError("sandbox exec exit code read before the process exited")
    return code


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


class FakeExecHandle(SandboxExecHandle):
    """An interactive host-subprocess handle over separate stdin/stdout/stderr pipes."""

    def __init__(self, proc: asyncio.subprocess.Process, *, timeout_seconds: float) -> None:
        self._proc = proc
        self._timeout_seconds = timeout_seconds
        self._write_lock = asyncio.Lock()
        self._output_iter: AsyncIterator[SandboxStreamChunk | SandboxStreamExit] | None = None
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
        _kill_group(self._proc)

    async def _stream_output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        queue: asyncio.Queue[SandboxStreamChunk | None] = asyncio.Queue()

        async def _reader(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:  # pragma: no cover
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
        await self.kill()
        with contextlib.suppress(ProcessLookupError):
            await self._proc.wait()
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver
        raise SandboxExecTimeoutError(
            timeout_seconds=self._timeout_seconds, stdout_len=self._stdout_len, stderr_len=self._stderr_len
        )


class FakeSandboxSession(ManagedSandboxSession):
    """One host-subprocess session over a temp workspace directory."""

    def __init__(
        self,
        *,
        sandbox: FakeSandbox,
        session_id: str,
        workspace_path: str,
        spec_env: dict[str, SecretStr],
    ) -> None:
        super().__init__(sandbox=sandbox, session_id=session_id)
        self._workspace_path = workspace_path
        self._spec_env = dict(spec_env)
        self._handles: list[FakeExecHandle] = []

    @property
    def workspace_path(self) -> str:
        return self._workspace_path

    def _build_env(self, env: dict[str, SecretStr] | None) -> dict[str, str]:
        merged: dict[str, str] = {"PATH": _BASE_PATH}
        for key, value in self._spec_env.items():
            merged[key] = value.get_secret_value()
        for key, value in (env or {}).items():
            merged[key] = value.get_secret_value()
        return merged

    def _resolve_cwd(self, cwd: str | None) -> str:
        if cwd is None:
            return self._workspace_path
        if os.path.isabs(cwd):
            return cwd
        return self._contained(cwd)

    def _contained(self, path: str) -> str:
        root = self._workspace_path
        resolved = os.path.realpath(os.path.join(root, path))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SandboxError(f"path {path!r} escapes the sandbox workspace")
        return resolved

    async def _spawn(
        self, argv: Sequence[str], *, cwd: str, env: dict[str, str], stdin: bool
    ) -> asyncio.subprocess.Process:
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
        proc = await self._spawn(argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env), stdin=stdin is not None)
        stdout_buf = bytearray()
        stderr_buf = bytearray()

        async def _pump(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
            if stream is None:  # pragma: no cover
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
            _kill_group(proc)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            raise SandboxExecTimeoutError(
                timeout_seconds=timeout_seconds, stdout_len=len(stdout_buf), stderr_len=len(stderr_buf)
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
        proc = await self._spawn(argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env), stdin=True)
        handle = FakeExecHandle(proc, timeout_seconds=timeout_seconds)
        self._handles.append(handle)
        return handle

    async def put_file(self, path: str, data: bytes) -> None:
        target = Path(self._contained(path))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise SandboxError(f"sandbox put_file failed for {path!r}: {exc}") from exc

    async def get_file(self, path: str) -> bytes:
        target = Path(self._contained(path))
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise SandboxError(f"sandbox get_file miss for {path!r}") from exc
        except OSError as exc:
            raise SandboxError(f"sandbox get_file failed for {path!r}: {exc}") from exc

    async def terminate_handles(self) -> None:
        for handle in self._handles:
            await handle.kill()


class FakeSandbox(ManagedSandbox):
    """Subprocess-backed fake provider for the agents tests.

    Permissive: it accepts every policy-resolved geometry, so a spec built through
    ``build_policied_spec`` always creates. A ``persistent`` workspace is keyed by
    ``workspace_key`` under a temp root and survives a reap; an ``ephemeral`` one gets
    a throwaway temp dir that dies with the session.
    """

    def __init__(self) -> None:
        super().__init__()
        self._root = tempfile.mkdtemp(prefix="tai-agents-fake-sbx-")

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        if spec.durability == "persistent":
            workspace = os.path.join(self._root, "ws", spec.workspace_key)
            os.makedirs(workspace, exist_ok=True)
        else:
            os.makedirs(os.path.join(self._root, "ephemeral"), exist_ok=True)
            workspace = tempfile.mkdtemp(dir=os.path.join(self._root, "ephemeral"))
        return FakeSandboxSession(
            sandbox=self,
            session_id=uuid.uuid4().hex,
            workspace_path=os.path.realpath(workspace),
            spec_env=dict(spec.env),
        )

    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        assert isinstance(session, FakeSandboxSession)
        await session.terminate_handles()
        record = self._ledger.get(session.id)
        durability = record.durability if record is not None else "ephemeral"
        if durability == "ephemeral" or remove_workspace:
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(session.workspace_path)

    async def _list_orphan_resources(self) -> list[str]:
        return []

    async def destroy_session_via_reap(self, session_id: str) -> None:
        """Force a session past its deadline and reap it — a persistent workspace
        survives (``remove_workspace=False``), an ephemeral one dies. Drives the kit
        ledger ``expires_at`` back so a two-turn durability test needs no wall-clock wait."""
        record = self._ledger[session_id]
        record.expires_at = record.created_at
        await self.reap()

    def dispose(self) -> None:
        """Remove the whole temp root — the fixture teardown hook."""
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self._root)


def permissive_sandbox_policy() -> SandboxPolicy:
    """The wide-open policy the fake binds so only a provider inability, never the
    chokepoint, could reject a spec: egress open, no isolation floor, persistent
    allowed, transcript scrub off."""
    return SandboxPolicy(egress="egress", isolation="none", scrub_transcript=False, durable=True)


def make_fake_sandbox() -> FakeSandbox:
    """A bound, ready-to-create fake provider (its policy already installed)."""
    sandbox = FakeSandbox()
    sandbox.bind_policy(permissive_sandbox_policy())
    return sandbox
