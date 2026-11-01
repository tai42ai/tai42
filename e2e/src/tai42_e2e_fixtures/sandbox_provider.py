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
teardown kills each interactive exec's process group through the kit's :class:`HostProcess`, which
signals a group only while its own anchor holds it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
    SandboxFileNotFoundError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)
from tai42_kit.sandbox import (
    ExecDeadline,
    HostExecHandle,
    HostProcess,
    ManagedSandbox,
    ManagedSandboxSession,
    SandboxDispatchSettings,
    run_host_process,
)

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
        self._handles: list[HostExecHandle] = []

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

    async def _spawn(self, argv: Sequence[str], *, cwd: str, env: dict[str, str], stdin: bool) -> HostProcess:
        """Spawn a subprocess in its own session/process-group, anchored by the kit.

        The anchor lets a later kill reach what the command leaves in its group. A spawn
        failure (a missing binary, an unwritable cwd, an env the host cannot hold) raises a
        typed error NAMING the binary, never any env value.
        """
        launch, extra = _resolve_launch(argv, env)
        if not launch:
            raise SandboxError("sandbox exec requires a non-empty argv")
        if extra:
            env = {**env, **extra}
        try:
            return await HostProcess.spawn(launch, cwd=cwd, env=env, stdin=stdin)
        except (OSError, ValueError) as exc:
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
        process = await self._spawn(
            argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env, {}), stdin=stdin is not None
        )
        return await run_host_process(process, stdin=stdin, timeout_seconds=timeout_seconds)

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        deadline = ExecDeadline.from_now(timeout_seconds)
        process = await self._spawn(argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env, {}), stdin=True)
        handle = HostExecHandle(process, deadline=deadline)
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
            raise SandboxFileNotFoundError(path) from exc
        except OSError as exc:
            raise SandboxError(f"sandbox get_file failed for {path!r}: {exc}") from exc

    # -- teardown ---------------------------------------------------------------------------

    async def terminate_handles(self) -> None:
        """Kill every interactive subprocess this session started, and whatever each left running in its process group.

        Idempotent on an already-killed handle. Called by the provider teardown primitive.
        """
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

    def dispatch_settings(self) -> SandboxDispatchSettings:
        """The kit-declared dispatch defaults."""
        return SandboxDispatchSettings()

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
