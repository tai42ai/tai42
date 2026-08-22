"""A process-based FAKE sandbox provider the deterministic e2e sandbox suites ride.

Built on :class:`~tai42_kit.sandbox.ManagedSandbox` / :class:`ManagedSandboxSession`
exactly as a real provider is — it implements ONLY the three runtime primitives and a
session subclass, so the shared kit ledger, TTL/reap bookkeeping, orphan recovery, and
the session-create POLICY CHOKEPOINT all exercise the SHIPPED base, and the importable
kit conformance suite green-lights it unchanged. Each session runs its code as a REAL
host subprocess in a temp workspace directory (no docker, no vendor SDK), so a consumer's
``exec`` / ``exec_start`` / file transfer travels the genuine spawn + stream + teardown
path a container provider would.

REGISTERED at import via ``tai42_app.sandboxes.register_sandbox`` (the skeleton binds the
operator-resolved policy onto the instance), so a manifest activates it by naming this
module in the scalar ``sandbox_module`` slot.

RUNNER-SELECTION SEAM (keeps the ``claude_code`` adapter byte-identical to production):
the adapter always drives ``exec_start(["python", "-m", "tai_runner"], ...)``. This
provider recognises that argv and routes it by the ``SANDBOX_FAKE_RUNNER`` env var —
``stub:<script>`` runs the scripted :mod:`tai42_e2e_fixtures.claude_runner_stub` (the pure
JSONL emulator, no vendor import), while UNSET / ``real`` runs the actual materialized
``tai_runner`` payload under this interpreter (the real-SDK smoke leg). The interception
lives entirely here, so the adapter code under test never learns it is faked.

SECURITY INVARIANTS (mirroring the real host provider): a subprocess is spawned with a
CLEAN env (a fixed ``PATH`` base plus the session's ``spec.env`` and any per-exec overlay,
never the host ``os.environ``); a ``SecretStr`` value is unwrapped ONLY at the spawn call;
teardown ``os.killpg``s the child's process group (spawned ``start_new_session=True``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_kit.sandbox import ManagedSandbox, ManagedSandboxSession

# Read granularity for draining a pipe — a fixed chunk keeps memory bounded while a reader
# keeps pace with a fast producer (the host-provider constant).
_READ_CHUNK = 65536

# A fixed PATH base so a spawned subprocess never inherits the host environment; the
# session's ``spec.env`` and any per-exec overlay are layered on top of it.
_BASE_PATH = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

# The runner-selection env var. ``stub:<script>`` routes ``tai_runner`` to the scripted
# emulator with ``<script>`` named in the child env; UNSET or ``real`` runs the actual
# materialized payload under this interpreter.
_RUNNER_ENV = "SANDBOX_FAKE_RUNNER"

# The env var the scripted stub reads its script name off (set by this provider from the
# ``stub:<script>`` selector).
_RUNNER_SCRIPT_ENV = "SANDBOX_FAKE_RUNNER_SCRIPT"

# The env var the scripted stub reads the pinned SDK version off (set by this provider,
# resolved from the plugin pin at spawn time — the stub itself imports nothing vendor).
_SDK_VERSION_ENV = "CLAUDE_AGENT_SDK_VERSION"

# When truthy the provider is single-tier: a ``persistent`` request is REJECTED loudly, so
# a suite can drive the conformance spec-reject / policy-refusal persistent path against a
# provider that genuinely cannot honor durability.
_EPHEMERAL_ONLY_ENV = "SANDBOX_FAKE_EPHEMERAL_ONLY"

# The argv the ``claude_code`` adapter always drives its in-session runner with.
_RUNNER_ARGV = ["python", "-m", "tai_runner"]

# The scripted stub script beside this module.
_STUB_PATH = str(Path(__file__).with_name("claude_runner_stub.py"))


def _returncode(proc: asyncio.subprocess.Process) -> int:
    """The exit code of an already-exited process (every caller here awaits exit first)."""
    code = proc.returncode
    if code is None:  # pragma: no cover - defensive; every caller awaits exit first
        raise SandboxError("sandbox exec exit code read before the process exited")
    return code


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group, reaping grandchildren too. Idempotent."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _runner_launch(env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Resolve the ``tai_runner`` invocation against ``SANDBOX_FAKE_RUNNER``.

    Returns the argv to actually spawn and the extra env the child needs. ``stub:<script>``
    routes to the scripted emulator (the script name + the pinned SDK version injected);
    UNSET / ``real`` runs the materialized payload under this interpreter."""
    selector = os.environ.get(_RUNNER_ENV)
    if selector is None or selector == "real":
        return [sys.executable, "-m", "tai_runner"], {}
    if not selector.startswith("stub:"):
        raise SandboxError(
            f"{_RUNNER_ENV}={selector!r} is not a valid runner selector (expected 'real' or 'stub:<script>')"
        )
    script = selector[len("stub:") :]
    if not script:
        raise SandboxError(f"{_RUNNER_ENV}={selector!r} names no stub script after 'stub:'")
    # The stub imports nothing vendor; the provider resolves the pinned SDK version here (only
    # on a stack whose claude_code plugin is installed) and hands it over via the child env.
    from tai42_agents.claude_code.protocol import CLAUDE_AGENT_SDK_VERSION

    extra = {_RUNNER_SCRIPT_ENV: script, _SDK_VERSION_ENV: CLAUDE_AGENT_SDK_VERSION}
    return [sys.executable, _STUB_PATH], extra


def _resolve_launch(argv: Sequence[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Map the requested ``argv`` onto the interpreter subprocess to spawn.

    The ``tai_runner`` argv routes through the runner-selection seam; a bare ``python`` /
    ``python3`` argv runs under this interpreter (so the child is the SUT venv's Python,
    never a PATH-resolved one); everything else spawns as given (a real host binary — the
    conformance vocabulary of ``printenv`` / ``pwd`` / ``sleep`` / ``cat`` is coreutils)."""
    cmd = list(argv)
    if cmd == _RUNNER_ARGV:
        return _runner_launch(env)
    if cmd and cmd[0] in {"python", "python3"}:
        return [sys.executable, *cmd[1:]], {}
    return cmd, {}


class FakeExecHandle(SandboxExecHandle):
    """An interactive host-subprocess handle over separate stdin/stdout/stderr pipes.

    Separate pipes mean a write never interleaves with a read, so the concurrency contract
    is met without demuxing one shared attach stream; a ``write_stdin`` lock still serializes
    concurrent writers so a single call's bytes stay intact and in order. A total
    ``timeout_seconds`` deadline bounds the whole interactive exec; on expiry the process
    group is killed and the iterator raises :class:`SandboxExecTimeoutError`.
    """

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


class FakeSandboxSession(ManagedSandboxSession):
    """One host-subprocess session over a temp workspace directory.

    Implements only the runtime I/O (``workspace_path`` / ``exec`` / ``exec_start`` /
    ``put_file`` / ``get_file``); ``id`` / ``info`` / ``touch`` / ``destroy`` are inherited
    from the kit session base. The ``tai_runner`` argv is intercepted by the shared
    runner-selection seam; every other argv spawns as a genuine subprocess.
    """

    def __init__(
        self,
        *,
        sandbox: FakeSandbox,
        session_id: str,
        workspace_path: str,
        spec_env: dict[str, SecretStr],
    ) -> None:
        super().__init__(sandbox=sandbox, session_id=session_id)
        # The ABSOLUTE realpath workspace root (the child's default cwd and the anchor for
        # workspace-relative path resolution), exactly as the docker/local providers expose.
        self._workspace_path = workspace_path
        self._spec_env = dict(spec_env)
        self._handles: list[FakeExecHandle] = []

    @property
    def workspace_path(self) -> str:
        return self._workspace_path

    # -- env + path resolution --------------------------------------------------------------

    def _build_env(self, env: dict[str, SecretStr] | None, extra: dict[str, str]) -> dict[str, str]:
        """The CLEAN subprocess env: the fixed ``PATH`` base, overlaid by ``spec.env``, the
        per-exec ``env`` overlay (per-exec keys win — the credential channel), and any
        provider-supplied ``extra`` (the runner-stub selector/version). ``SecretStr`` values
        are unwrapped ONLY here, never logged."""
        merged: dict[str, str] = {"PATH": _BASE_PATH}
        for key, value in self._spec_env.items():
            merged[key] = value.get_secret_value()
        for key, value in (env or {}).items():
            merged[key] = value.get_secret_value()
        merged.update(extra)
        return merged

    def _resolve_cwd(self, cwd: str | None) -> str:
        if cwd is None:
            return self._workspace_path
        if os.path.isabs(cwd):
            return cwd
        return self._contained(cwd)

    def _contained(self, path: str) -> str:
        """Resolve a workspace-relative ``path`` against the root and REJECT one that escapes
        it (realpath containment), so a ``..`` component cannot walk out of the workspace."""
        root = self._workspace_path
        resolved = os.path.realpath(os.path.join(root, path))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SandboxError(f"path {path!r} escapes the sandbox workspace")
        return resolved

    async def _spawn(
        self, argv: Sequence[str], *, cwd: str, env: dict[str, str], stdin: bool
    ) -> asyncio.subprocess.Process:
        """Spawn a subprocess in its own session/process-group. A spawn failure raises a typed
        error naming the binary, never any env value."""
        launch, extra = _resolve_launch(argv, env)
        if not launch:
            raise SandboxError("sandbox exec requires a non-empty argv")
        if extra:
            env = {**env, **extra}
        try:
            return await asyncio.create_subprocess_exec(
                *launch,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxError(f"sandbox exec could not spawn {launch[0]!r}: {exc}") from exc

    # -- exec -------------------------------------------------------------------------------

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
            argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env, {}), stdin=stdin is not None
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
        proc = await self._spawn(argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env, {}), stdin=True)
        handle = FakeExecHandle(proc, timeout_seconds=timeout_seconds)
        self._handles.append(handle)
        return handle

    # -- file transfer ----------------------------------------------------------------------

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

    # -- teardown ---------------------------------------------------------------------------

    async def terminate_handles(self) -> None:
        """Kill every live interactive subprocess this session started (idempotent on an
        already-exited process). Called by the provider teardown primitive."""
        for handle in self._handles:
            await handle.kill()


class FakeSandbox(ManagedSandbox):
    """Process-based fake provider registered into the scalar sandbox slot.

    Implements only the three runtime primitives; the kit base owns the ledger, TTL, reap,
    orphan recovery, and the policy chokepoint. A ``persistent`` workspace is keyed by
    ``workspace_key`` under a stable temp root and SURVIVES a reap (a later ``create_session``
    on the same key re-attaches the bytes); an ``ephemeral`` one gets a throwaway temp dir
    that dies with the session on every teardown path.
    """

    def __init__(self) -> None:
        super().__init__()
        self._root = tempfile.mkdtemp(prefix="tai-e2e-fake-sbx-")
        # A single-tier provider rejects persistent loudly, so the conformance / policy
        # spec-reject persistent path is real rather than stubbed. Read once at construction
        # (recycle re-imports this module and rebuilds the provider on a settings flip).
        self._ephemeral_only = _truthy(os.environ.get(_EPHEMERAL_ONLY_ENV))

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        # Provider-capability rejections (the kit already enforced the operator policy floor/
        # ceiling before this runs): an unenforceable cap, and — when single-tier — durability.
        if spec.cpu is not None or spec.memory_mb is not None:
            raise SandboxSpecRejectedError(
                "provider cannot enforce: a cpu/memory_mb cap — the process-based fake has no "
                "cgroup/rlimit machinery and never runs a capped request uncapped"
            )
        if self._ephemeral_only and spec.durability == "persistent":
            raise SandboxSpecRejectedError(
                "provider cannot enforce: a persistent workspace — this fake is env-configured "
                f"single-tier ({_EPHEMERAL_ONLY_ENV}) and never downgrades a durable request to scratch"
            )

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
        if not isinstance(session, FakeSandboxSession):  # pragma: no cover - the base only holds our sessions
            raise SandboxError("sandbox teardown received a foreign session type")
        await session.terminate_handles()
        # The ledger record still exists here (the base forgets it AFTER this primitive), so
        # the durability tier is read off it. Ephemeral scratch dies on BOTH paths; a persistent
        # workspace is removed only on an explicit destroy (``remove_workspace=True``), never a reap.
        record = self._ledger.get(session.id)
        durability = record.durability if record is not None else "ephemeral"
        if durability == "ephemeral" or remove_workspace:
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(session.workspace_path)

    async def _list_orphan_resources(self) -> list[str]:
        # The fake holds no cross-process residue to reconcile — every workspace lives under a
        # per-process temp root reaped on shutdown.
        return []

    def dispose(self) -> None:
        """Remove the whole temp root — the shutdown hook, so the fixture leaks no temp dir."""
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self._root)


# Plain call (not a decorator) so ``FakeSandbox`` keeps its concrete class type; the skeleton
# registry instantiates it and binds the operator-resolved policy onto the instance.
tai42_app.sandboxes.register_sandbox(FakeSandbox)


@tai42_app.lifecycle.on_shutdown
def _dispose_fake_sandbox() -> None:
    """Reap the registered fake provider's temp root on process shutdown."""
    sandbox = tai42_app.sandboxes.sandbox
    if isinstance(sandbox, FakeSandbox):
        sandbox.dispose()
