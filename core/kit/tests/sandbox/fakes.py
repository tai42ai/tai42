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

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
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

from tai42_kit.sandbox import (
    ManagedSandbox,
    ManagedSandboxSession,
    SandboxConformanceConfig,
    SandboxDispatchSettings,
    conformance_deadline,
    conformance_exec,
    conformance_group_stop,
    permissive_policy,
)
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE

# The cases of the group-stop checks, each with its own exec and markers.
_GROUP_STOP_CASES = ("one-shot", "interactive", "kill")


class FakeExecHandle(SandboxExecHandle):
    """A trivial interactive exec: it echoes whatever stdin it was given as one
    stdout frame, then exits. Enough to exercise the interactive seam's write /
    output / exit / kill / write-after-exit / deadline contract.

    An exec given ``output`` has written it as it starts. A ``sleep N`` exec ends ``N`` seconds
    after it starts; an exec given a ``late`` effect ``(delay, effect)`` runs ``effect``
    ``delay`` seconds after it starts, then ends; any other exec ends when its stdin is closed.
    One still running at its ``timeout_seconds`` is killed then, whether or not its output is
    read, and its output raises
    :class:`SandboxExecTimeoutError`; once :meth:`kill` has returned, the output ends with its
    exit item instead, past the deadline too. An exec given ``held`` is one whose output a
    process it started still holds: it runs on until its deadline, whose timeout notes
    :data:`~tai42_kit.sandbox.exec_kill.OUTPUT_HELD_NOTE`.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        late: tuple[float, Callable[[], None]] | None = None,
        output: bytes = b"",
        held: bool = False,
    ) -> None:
        self._buffer = bytearray(output)
        self._held = held
        self._timeout_seconds = timeout_seconds
        self._ended = asyncio.Event()
        self._timed_out = False
        loop = asyncio.get_running_loop()
        self._timers = [loop.call_later(timeout_seconds, self._on_deadline)]
        if argv[0] == "sleep":
            self._timers.append(loop.call_later(float(argv[1]), self._end))
        if late is not None:
            delay, effect = late
            self._timers.append(loop.call_later(delay, self._run_late, effect))

    def _run_late(self, effect: Callable[[], None]) -> None:
        effect()
        self._end()

    def _end(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._ended.set()

    def _on_deadline(self) -> None:
        self._timed_out = True
        self._end()

    async def write_stdin(self, data: bytes) -> None:
        if self._ended.is_set():
            raise SandboxError("interactive exec has already exited")
        self._buffer.extend(data)

    async def close_stdin(self) -> None:
        self._end()

    @property
    def output(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
        yield SandboxStreamChunk(stream="stdout", data=bytes(self._buffer))
        await self._ended.wait()
        if self._timed_out:
            timeout = SandboxExecTimeoutError(
                timeout_seconds=self._timeout_seconds, stdout_len=len(self._buffer), stderr_len=0
            )
            if self._held:
                timeout.add_note(OUTPUT_HELD_NOTE)
            raise timeout
        yield SandboxStreamExit(exit_code=0)

    async def kill(self) -> None:
        self._timed_out = False
        self._end()


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
        if cmd[0] == "cat":
            return ExecResult(exit_code=0, stdout=(stdin or b"").decode(), stderr="")
        if cmd[:2] == ["head", "-c"]:
            return ExecResult(exit_code=0, stdout=(stdin or b"")[: int(cmd[2])].decode(), stderr="")
        if tuple(cmd) == conformance_deadline._UNREAD_PROBE_ARGV:
            return self._marker_probe(
                conformance_deadline._UNREAD_STARTED_MARKER, conformance_deadline._UNREAD_LATE_MARKER
            )
        for case in _GROUP_STOP_CASES:
            if cmd == conformance_group_stop._left_in_group_probe_argv(case):
                return self._marker_probe(*conformance_group_stop._markers(case))
        if cmd == conformance_group_stop._left_in_group_argv("one-shot"):
            # The one-shot group-stop case's exec: its process left in its group holds its output
            # until the deadline's stop kills it, before its late marker.
            self._files[self._resolve(conformance_group_stop._markers("one-shot")[0])] = b""
            raise SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=0, stderr_len=0)
        if tuple(cmd) == conformance_deadline._RECORDED_WRITER_ARGV:
            # The one-shot timeout-count case's writer: two chunks recorded, a third cut by the kill.
            written = 2 * conformance_deadline._WRITTEN_CHUNK_LEN
            self._files[self._resolve(conformance_deadline._WRITTEN_RECORD)] = str(written).encode()
            raise SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=written + 100, stderr_len=0)
        if tuple(cmd) == conformance_deadline._HELD_TO_END_ARGV:
            # The read-to-end case's exec: it runs while its writer holds its output.
            await asyncio.sleep(conformance_deadline._HELD_TO_END_SECONDS)
            lines = "".join(f"{i}\n" for i in range(1, conformance_deadline._HELD_TO_END_LINES + 1))
            return ExecResult(exit_code=conformance_deadline._HELD_TO_END_EXIT_CODE, stdout=lines, stderr="")
        if cmd[0] == "sleep":
            if float(cmd[1]) > timeout_seconds:
                raise SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=0, stderr_len=0)
            return ExecResult(exit_code=0, stdout="", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    def _marker_probe(self, started: str, late: str) -> ExecResult:
        """A marker probe's result: exit 3 without the ``started`` marker, 4 with the ``late`` one, else 0."""
        if self._resolve(started) not in self._files:
            return ExecResult(exit_code=3, stdout="", stderr="")
        if self._resolve(late) in self._files:
            return ExecResult(exit_code=4, stdout="", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> SandboxExecHandle:
        if tuple(argv) == conformance_deadline._LARGE_OUTPUT_ARGV:
            # The large-output late-read case's exec: its output written, it ends at once.
            output = bytes(conformance_deadline._LARGE_OUTPUT_LEN)
            return self.handle_cls(argv, timeout_seconds=timeout_seconds, late=(0.0, lambda: None), output=output)
        if tuple(argv) == conformance_deadline._LARGE_OUTPUT_THEN_SLEEP_ARGV:
            # The large-output timeout-count case's exec: its output written, it runs on.
            output = bytes(conformance_deadline._LARGE_OUTPUT_LEN)
            return self.handle_cls(argv, timeout_seconds=timeout_seconds, output=output)
        if list(argv) in (conformance_deadline._unread_exec_argv(), conformance_deadline._output_closing_exec_argv()):
            # An unread-exec case's exec: its start marker now, its late marker after its sleep.
            self._files[self._resolve(conformance_deadline._UNREAD_STARTED_MARKER)] = b""
            late_marker = self._resolve(conformance_deadline._UNREAD_LATE_MARKER)
            return self.handle_cls(
                argv,
                timeout_seconds=timeout_seconds,
                late=(
                    conformance_deadline._UNREAD_EXEC_SLEEP_SECONDS,
                    lambda: self._files.__setitem__(late_marker, b""),
                ),
            )
        for case in ("interactive", "kill"):
            if list(argv) == conformance_group_stop._left_in_group_argv(case):
                # An interactive group-stop case's exec: its start marker and line now, its late
                # marker after its left process's sleep, which a stop cuts short.
                started, late = (self._resolve(marker) for marker in conformance_group_stop._markers(case))
                self._files[started] = b""
                return self.handle_cls(
                    argv,
                    timeout_seconds=timeout_seconds,
                    output=conformance_group_stop._LEFT_IN_GROUP_READY,
                    late=(
                        conformance_group_stop._LEFT_IN_GROUP_SLEEP_SECONDS,
                        lambda late=late: self._files.__setitem__(late, b""),
                    ),
                )
        if tuple(argv) in (conformance_exec._BEFORE_KILL_ARGV, conformance_exec._BEFORE_KILL_HELD_ARGV):
            # A kill-keeps-output case's exec: its output written, it runs on until its kill.
            return self.handle_cls(argv, timeout_seconds=timeout_seconds, output=conformance_exec._BEFORE_KILL_OUTPUT)
        if tuple(argv) in (
            conformance_deadline._OUTPUT_HELD_AFTER_EXIT_ARGV,
            conformance_deadline._OUTPUT_HELD_WHILE_RUNNING_ARGV,
        ):
            # An output-holder case's exec: its writer's first bytes written, its output held.
            return self.handle_cls(argv, timeout_seconds=timeout_seconds, output=bytes(4096), held=True)
        return self.handle_cls(argv, timeout_seconds=timeout_seconds)

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

    def dispatch_settings(self) -> SandboxDispatchSettings:
        """The kit-declared dispatch defaults."""
        return SandboxDispatchSettings()

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


# The config the kit's tests run a conformance check under against a fake.
FAKE_CONFORMANCE_CONFIG = SandboxConformanceConfig(image="fake:image")


def with_permissive_policy(sandbox: FakeSandbox) -> FakeSandbox:
    """``sandbox`` with the permissive policy bound, as a conformance check drives a provider."""
    sandbox.bind_policy(permissive_policy())
    return sandbox
