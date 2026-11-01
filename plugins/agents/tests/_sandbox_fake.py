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

import contextlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecHandle,
    SandboxFileNotFoundError,
    SandboxPolicy,
    SandboxSessionSpec,
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

# A fixed PATH base so a spawned subprocess never inherits the host environment.
_BASE_PATH = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


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
        self._handles: list[HostExecHandle] = []

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

    async def _spawn(self, argv: Sequence[str], *, cwd: str, env: dict[str, str], stdin: bool) -> HostProcess:
        if not argv:
            raise SandboxError("sandbox exec requires a non-empty argv")
        try:
            return await HostProcess.spawn(argv, cwd=cwd, env=env, stdin=stdin)
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
        process = await self._spawn(argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env), stdin=stdin is not None)
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
        process = await self._spawn(argv, cwd=self._resolve_cwd(cwd), env=self._build_env(env), stdin=True)
        handle = HostExecHandle(process, deadline=deadline)
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
            raise SandboxFileNotFoundError(path) from exc
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
        # The dispatch knobs a test overrides (e.g. the default short-exec timeout).
        self.dispatch = SandboxDispatchSettings()

    def dispatch_settings(self) -> SandboxDispatchSettings:
        """The settable dispatch knobs."""
        return self.dispatch

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
