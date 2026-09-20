"""An in-memory fake sandbox provider the kit's own tests drive.

It builds on :class:`ManagedSandbox` / :class:`ManagedSandboxSession` exactly as a
real provider does — implementing only the three runtime primitives and a session
subclass — so the ledger, TTL, reap, orphan recovery, policy chokepoint, and the
conformance suite all exercise the SHIPPED base, not a reimplementation. Its
clock is settable so TTL and reap are driven without wall-clock waits, and a few
knobs (``reject_caps``, ``supports_persistent``, ``orphans``) let a test steer the
provider-specific rejection and recovery paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import SecretStr
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


class FakeExecHandle(SandboxExecHandle):
    """A trivial interactive exec: it echoes whatever stdin it was given as one
    stdout frame, then exits. Enough to exercise the interactive seam's write /
    output / exit / kill / write-after-exit contract."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._exited = False

    async def write_stdin(self, data: bytes) -> None:
        if self._exited:
            raise SandboxError("interactive exec has already exited")
        self._buffer.extend(data)

    async def close_stdin(self) -> None:
        return None

    @property
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        self._exited = True
        yield SandboxStreamExit(exit_code=0)

    async def kill(self) -> None:
        self._exited = True


class FakeSandboxSession(ManagedSandboxSession):
    """A session whose workspace is an in-memory file map and whose ``exec``
    interprets the handful of argv the conformance suite drives."""

    # Seam so a non-conformant variant can inject a misbehaving handle without
    # duplicating construction.
    handle_cls: ClassVar[type[FakeExecHandle]] = FakeExecHandle

    def __init__(
        self,
        *,
        sandbox: ManagedSandbox,
        session_id: str,
        workspace_key: str,
        durability: str,
        env: dict[str, str],
        files: dict[str, bytes],
    ) -> None:
        super().__init__(sandbox=sandbox, session_id=session_id)
        self.workspace_key = workspace_key
        self.durability = durability
        self._env = env
        self._files = files

    @property
    def workspace_path(self) -> str:
        return f"/ws/{self.workspace_key}"

    def _resolve(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.workspace_path}/{path}"

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        merged = dict(self._env)
        if env:
            merged.update({key: value.get_secret_value() for key, value in env.items()})
        cmd = list(argv)
        if cmd[0] == "printenv":
            name = cmd[1]
            if name not in merged:
                return ExecResult(exit_code=1, stdout="", stderr="")
            return ExecResult(exit_code=0, stdout=f"{merged[name]}\n", stderr="")
        if cmd[0] == "pwd":
            resolved = self.workspace_path if cwd is None else self._resolve(cwd)
            return ExecResult(exit_code=0, stdout=f"{resolved}\n", stderr="")
        if cmd[0] == "sleep":
            if float(cmd[1]) > timeout_seconds:
                raise SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=0, stderr_len=0)
            return ExecResult(exit_code=0, stdout="", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        return self.handle_cls()

    async def put_file(self, path: str, data: bytes) -> None:
        self._files[self._resolve(path)] = data

    async def get_file(self, path: str) -> bytes:
        resolved = self._resolve(path)
        if resolved not in self._files:
            raise SandboxError(f"no file at {path!r}")
        return self._files[resolved]


class FakeSandbox(ManagedSandbox):
    """The in-memory provider. Records the effective spec each create received so a
    test can assert the chokepoint's isolation/label resolution."""

    # Seam so a non-conformant variant can inject a misbehaving session.
    session_cls: ClassVar[type[FakeSandboxSession]] = FakeSandboxSession

    def __init__(
        self,
        *,
        reject_caps: bool = True,
        supports_persistent: bool = True,
        ephemeral_persists: bool = False,
        orphans: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self.reject_caps = reject_caps
        self.supports_persistent = supports_persistent
        self.ephemeral_persists = ephemeral_persists
        self.orphans = list(orphans)
        self.clock = datetime(2020, 1, 1, tzinfo=UTC)
        self.created_specs: list[SandboxSessionSpec] = []
        self.destroyed: list[tuple[str, bool]] = []
        self._persistent: dict[str, dict[str, bytes]] = {}
        self._counter = 0

    def _now(self) -> datetime:
        return self.clock

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        if self.reject_caps and (spec.cpu is not None or spec.memory_mb is not None):
            raise SandboxSpecRejectedError("provider cannot enforce resource caps")
        if not self.supports_persistent and spec.durability == "persistent":
            raise SandboxSpecRejectedError("provider has no durable storage")
        self.created_specs.append(spec)
        session_id = f"sess-{self._counter}"
        self._counter += 1
        env = {key: value.get_secret_value() for key, value in spec.env.items()}
        # A persistent workspace shares one file map per key (it survives reap); an
        # ephemeral one gets a fresh map that dies with the session. A broken
        # variant may share the map for ephemeral too, so the suite catches it.
        durable_map = spec.durability == "persistent" or self.ephemeral_persists
        files = self._persistent.setdefault(spec.workspace_key, {}) if durable_map else {}
        return self.session_cls(
            sandbox=self,
            session_id=session_id,
            workspace_key=spec.workspace_key,
            durability=spec.durability,
            env=env,
            files=files,
        )

    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        assert isinstance(session, FakeSandboxSession)
        self.destroyed.append((session.id, remove_workspace))
        if remove_workspace and session.durability == "persistent":
            self._persistent.pop(session.workspace_key, None)

    async def _list_orphan_resources(self) -> list[str]:
        return list(self.orphans)
