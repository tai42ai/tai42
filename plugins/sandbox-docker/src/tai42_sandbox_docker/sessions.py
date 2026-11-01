"""The Docker session and its interactive exec handle — the provider's runtime I/O.

A :class:`DockerSandboxSession` runs ``exec`` / ``exec_start`` and file transfers
against ONE engine container; the kit :class:`~tai42_kit.sandbox.ManagedSandboxSession`
base it extends owns ``id`` / ``info()`` / ``touch()`` / ``destroy()`` (all routed
through the owning sandbox's ledger), so this module carries only the container I/O.

Every ``timeout_seconds`` is enforced HOST-SIDE (an ``asyncio`` deadline around the
attach/drain), never by trusting the engine: on expiry the exec's command is killed
and a :class:`SandboxExecTimeoutError` is raised carrying the partial-output LENGTHS
only. Every exec runs under the wrapper of :mod:`~tai42_sandbox_docker.exec_control`,
which runs the command in a process group of its own and records it, so a sibling exec can
kill that group. A ``spec.env`` value is a :class:`~pydantic.SecretStr` unwrapped ONLY at
the engine call and never placed in a repr, log, or error message.
"""

from __future__ import annotations

import asyncio
import functools
import posixpath
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from pydantic import SecretStr
from tai42_contract.sandbox import (
    ExecResult,
    SandboxDurability,
    SandboxError,
    SandboxExecTimeoutError,
    SandboxFileNotFoundError,
    SandboxStreamChunk,
)
from tai42_kit.sandbox import ExecDeadline, ManagedSandboxSession, read_while_feeding, resolve_workspace_path
from tai42_kit.sandbox.exec_kill import KillResult, kill_on_failure
from tai42_kit.sandbox.sibling_kill_handle import SiblingKillExecHandle
from tai42_kit.sandbox.sibling_kill_one_shot import OneShotOutput, run_one_shot

from tai42_sandbox_docker.exec_control import kill_exec_group, wrap_command
from tai42_sandbox_docker.exec_status import EngineErrorMapper, read_exit_code

if TYPE_CHECKING:
    from tai42_kit.sandbox import ManagedSandbox

    from tai42_sandbox_docker.attach import ExecAttacher, ExecAttachment
    from tai42_sandbox_docker.provider import DockerSandbox

# The absolute container path the session workspace mounts at. A workspace-RELATIVE
# ``cwd`` / ``path`` resolves under here; an absolute value is passed to the engine
# as given (the caller's own responsibility, per the contract path rule).
WORKSPACE_PATH = "/workspace"

# How long the engine keeps the stream of an exited exec open, at most, before it closes it.
_ENGINE_STREAM_CLOSE_SECONDS = 2.0
# How long an exec's stream may take to end once the exec is over or killed: the engine's close of
# an exited exec's stream, with as much again for the close to reach the attach. A stream still
# open after that, the exec over, is the engine failing to end it.
_STREAM_END_GRACE_SECONDS = 2 * _ENGINE_STREAM_CLOSE_SECONDS


def engine_error(exc: DockerError) -> SandboxError:
    """Wrap an engine :class:`DockerError` as a typed :class:`SandboxError`.

    Carries the engine's own status and message (never any ``spec.env`` value — the
    engine message describes the API failure, not the credential channel).
    """
    return SandboxError(f"docker engine error [{exc.status}]: {exc.message}")


def merge_env(base: Mapping[str, SecretStr], overlay: Mapping[str, SecretStr] | None) -> dict[str, str]:
    """Merge the session's base ``spec.env`` with a per-exec overlay, secrets unwrapped only here.

    Each secret is unwrapped ONLY here at the engine call. Per-exec keys override the base
    on collision.
    """
    merged = {key: value.get_secret_value() for key, value in base.items()}
    if overlay:
        merged.update({key: value.get_secret_value() for key, value in overlay.items()})
    return merged


class DockerSandboxExecHandle(SiblingKillExecHandle):
    """A live interactive exec attached over one demultiplexed engine stream.

    Reads and writes ride ONE :class:`~tai42_sandbox_docker.attach.ExecAttachment`; the kit
    :class:`~tai42_kit.sandbox.SiblingKillExecHandle` it extends holds the deadline, the stops
    and the grace after a caller's kill. The exec's wrapper ends only once nothing holds the
    exec's output, so an exec the engine records as ended has nothing holding it. An exec its
    kill signalled is over by its kill, whether or not a process the command started still
    holds its output: its stream is closed, and its output ends at once with the status the
    kill reported for its command — the killed status, or the command's own when it ended by
    itself before the kill landed. A kill that found nothing left to kill while a process the
    command started still holds the exec's output closes its stream and ends the output at once
    with a :class:`SandboxError`: the exec's status cannot be known, since the kill read none
    and the engine records no exit code for the exec while its output is held. A kill that found
    nothing running, the exec not started, or the exec ended, as when its container stopped,
    with no process holding the exec's output, leaves the stream open for the exec's own end:
    the output ends with the exec's exit code (the killed status for an exec that never
    started). A stream still open past the grace then is a :class:`SandboxError`.
    """

    _status_grace_seconds: ClassVar[float] = _STREAM_END_GRACE_SECONDS

    def __init__(
        self,
        *,
        container: Any,
        exec_obj: Any,
        exec_id: str,
        attachment: ExecAttachment,
        deadline: ExecDeadline,
        retype: EngineErrorMapper,
        attach: ExecAttacher,
    ) -> None:
        """Bind the handle to the started exec ``exec_id``'s ``attachment`` on ``container``.

        The exec is bounded by its ``deadline``; ``retype`` re-types every engine error the handle
        meets, and ``attach`` opens the stream of its sibling kill.
        """
        super().__init__(exec_id=exec_id, deadline=deadline)
        self._container = container
        self._engine_error = retype
        self._attach = attach
        self._exec = exec_obj
        self._attachment = attachment
        # The status a kill that signalled the exec's group reported for its command: the output
        # ends with it.
        self._killed_status: int | None = None
        # Set once a kill that found nothing left to kill has closed the stream of an exec whose
        # output a process the command started still held: the output then ends with a
        # :class:`SandboxError`.
        self._held_at_kill = False

    async def _write(self, data: bytes) -> None:
        if not await self._attachment.write(data):
            raise self._stdin_lost()

    async def _end_input(self) -> None:
        self._attachment.end_input()

    async def _receive(self) -> SandboxStreamChunk | None:
        return await self._attachment.read()

    def _take_buffered(self) -> list[SandboxStreamChunk]:
        return self._attachment.take_received()

    def _ended_in_stream(self) -> bool:
        # The attach stream carries no exit status: only the engine's inspect shows the exec ended.
        return False

    def _demux(self, message: SandboxStreamChunk) -> SandboxStreamChunk:
        return message

    async def _close_stream(self) -> None:
        self._attachment.close()

    async def _exit_status(self) -> int:
        if self._killed_status is not None:
            return self._killed_status
        if self._held_at_kill:
            raise SandboxError(
                "sandbox exec output ended by the kill while a process the command started still holds it, "
                "and the exec's exit status cannot be known: the kill found nothing of the exec left to kill, "
                "so it read no status, and the engine records no exit code for the exec until nothing holds "
                "its output"
            )
        return await read_exit_code(self._exec, self._engine_error)

    def _grace_error(self) -> SandboxError:
        # The exec is over, its process exited with nothing holding its output, and the engine
        # reports no exit code for an exec whose stream it has not ended: none is invented.
        return SandboxError(
            f"sandbox exec stream did not end within {self._status_grace_seconds:g}s of the exec's end: "
            "the engine did not close the stream of an exec that has exited with nothing holding its output"
        )

    async def _kill_exec(self) -> KillResult:
        return await kill_exec_group(self._container, self._exec, self._exec_id, self._engine_error, self._attach)

    async def _end_killed(self, result: KillResult) -> None:
        self._killed_status = result.exit_code
        self._held_at_kill = result.output_held and result.exit_code is None
        self._attachment.close()


class DockerSandboxSession(ManagedSandboxSession):
    """One live session bound to a single engine container."""

    # A Docker session's owner is always the Docker provider; the narrowed type lets a
    # session route an engine disconnect back through it (resetting the readiness flag).
    _sandbox: DockerSandbox

    def __init__(
        self,
        *,
        sandbox: ManagedSandbox,
        session_id: str,
        container: Any,
        workspace_key: str,
        durability: SandboxDurability,
        base_env: Mapping[str, SecretStr],
    ) -> None:
        """Bind the session to its engine ``container`` and workspace, carrying the base ``spec.env`` and durability."""
        super().__init__(sandbox=sandbox, session_id=session_id)
        self._container = container
        self._workspace_key = workspace_key
        self._durability: SandboxDurability = durability
        self._base_env = dict(base_env)

    @property
    def workspace_path(self) -> str:
        """The absolute container path the session workspace mounts at."""
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
        """Run ``argv`` to completion and return its :class:`ExecResult`.

        A host-side timeout kills the exec and raises :class:`SandboxExecTimeoutError`, its
        counts taken as the kit's :func:`~tai42_kit.sandbox.sibling_kill_one_shot.run_one_shot`
        states; any other failure (an engine error, a stdin it cannot close, a cancelled call)
        kills it and propagates. A kill that fails, or finds a process still holding the exec's
        output, then is logged and noted on the raised error.
        """
        exec_id, exec_obj = await self._make_exec(argv, cwd=cwd, env=env, stdin=stdin is not None)
        output = OneShotOutput()
        exit_code = await run_one_shot(
            self._drain(exec_obj, stdin, output),
            output,
            timeout_seconds=timeout_seconds,
            kill=lambda: kill_exec_group(
                self._container, exec_obj, exec_id, self._sandbox._engine_error, self._sandbox._attach_exec
            ),
            exec_id=exec_id,
            grace_seconds=_STREAM_END_GRACE_SECONDS,
            timeout_error=lambda: SandboxExecTimeoutError(
                timeout_seconds=timeout_seconds, stdout_len=output.stdout_len, stderr_len=output.stderr_len
            ),
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=bytes(output.stdout).decode("utf-8", "replace"),
            stderr=bytes(output.stderr).decode("utf-8", "replace"),
        )

    async def exec_start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        timeout_seconds: float,
    ) -> DockerSandboxExecHandle:
        """Start ``argv`` as a live interactive exec and return its :class:`DockerSandboxExecHandle`.

        Attaching starts the exec, so an attach that fails or is cancelled kills it, aborts the
        attach connection and propagates its own error, an engine error as :class:`SandboxError`.
        The exec's one deadline runs from the attach: an attach still unanswered when it passes
        kills the exec and raises :class:`SandboxExecTimeoutError`, and the handle's output
        is bounded by what is left of it. A kill that fails, or finds a process still holding the exec's output, then is
        logged and noted on the raised error.
        """
        exec_id, exec_obj = await self._make_exec(argv, cwd=cwd, env=env, stdin=True)
        # Attaching starts the exec, so it runs, and its deadline with it, from here on.
        deadline = ExecDeadline.from_now(timeout_seconds)

        def failure_of(exc: BaseException) -> BaseException:
            # The only TimeoutError the block can raise is the passed deadline's: _attach_exec
            # re-types every error of the attach, a TimeoutError (an OSError) included, as
            # SandboxError. A caller's cancel landing in the deadline's own tick leaves the
            # deadline's scope as the CancelledError it is, so it stays the caller's cancellation.
            if isinstance(exc, TimeoutError):
                return SandboxExecTimeoutError(timeout_seconds=timeout_seconds, stdout_len=0, stderr_len=0)
            return exc

        async with (
            kill_on_failure(
                lambda: kill_exec_group(
                    self._container, exec_obj, exec_id, self._sandbox._engine_error, self._sandbox._attach_exec
                ),
                exec_id,
                failure_of=failure_of,
            ),
            asyncio.timeout_at(deadline.at),
        ):
            attachment = await self._sandbox._attach_exec(exec_obj.id)
        return DockerSandboxExecHandle(
            container=self._container,
            exec_obj=exec_obj,
            exec_id=exec_id,
            attachment=attachment,
            deadline=deadline,
            retype=self._sandbox._engine_error,
            attach=self._sandbox._attach_exec,
        )

    async def put_file(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` in the workspace, creating intermediate directories."""
        target = resolve_workspace_path(path, root=WORKSPACE_PATH)
        # docker's put_archive extracts a tar at ``base`` and requires that dir to
        # exist. For a workspace path we extract at the workspace ROOT with the member
        # path RELATIVE to it, and the tar carries its parent-dir entries, so a nested
        # ``sub/nested.txt`` creates ``sub/`` instead of 404-ing on the missing dir.
        if target == WORKSPACE_PATH or target.startswith(WORKSPACE_PATH + "/"):
            base = WORKSPACE_PATH
            member = target[len(WORKSPACE_PATH) :].lstrip("/")
        else:
            # An absolute path outside the workspace: its parent is the caller's own
            # responsibility (the contract path rule).
            base, member = posixpath.split(target)
            base = base or "/"
        archive = _tar_single_member(member, data)
        try:
            await self._container.put_archive(base, archive)
        except DockerError as exc:
            raise self._sandbox._engine_error(exc) from exc

    async def get_file(self, path: str) -> bytes:
        """Read and return the bytes of ``path`` in the workspace; a missing file is a loud :class:`SandboxError`."""
        target = resolve_workspace_path(path, root=WORKSPACE_PATH)
        try:
            tar = await self._container.get_archive(target)
        except DockerError as exc:
            if exc.status == 404:
                raise SandboxFileNotFoundError(path) from exc
            raise self._sandbox._engine_error(exc) from exc
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
    ) -> tuple[str, Any]:
        """Create the engine exec running ``argv`` under the exec wrapper; returns its id and the exec."""
        workdir = resolve_workspace_path(cwd, root=WORKSPACE_PATH) if cwd is not None else WORKSPACE_PATH
        environment = merge_env(self._base_env, env)
        exec_id = uuid.uuid4().hex
        try:
            exec_obj = await self._container.exec(
                cmd=wrap_command(exec_id, argv),
                stdin=stdin,
                stdout=True,
                stderr=True,
                tty=False,
                workdir=workdir,
                environment=environment,
            )
        except DockerError as exc:
            raise self._sandbox._engine_error(exc) from exc
        return exec_id, exec_obj

    async def _drain(self, exec_obj: Any, stdin: bytes | None, output: OneShotOutput) -> int:
        """Attach to ``exec_obj``, feed it ``stdin`` while reading its output into ``output``; returns its exit code.

        The stdin is written, then ended, while the output is read, so a command that echoes
        its input never blocks the write, and the engine's end of the output aborts the attach
        connection, so stdin the command never took does not hold the output open. A read that
        ends early, cancelled included, adds the output the attachment had already received
        before it closes. An engine error, from the attach to the exit code, raises as
        :class:`SandboxError`.
        """
        attachment = await self._sandbox._attach_exec(exec_obj.id)

        async def read() -> None:
            while (chunk := await attachment.read()) is not None:
                output.add(chunk)

        feed = functools.partial(_feed_stdin, attachment, stdin) if stdin is not None else None
        try:
            await read_while_feeding(read, feed)
        finally:
            for chunk in attachment.take_received():
                output.add(chunk)
            attachment.close()
        return await read_exit_code(exec_obj, self._sandbox._engine_error)


async def _feed_stdin(attachment: ExecAttachment, data: bytes) -> None:
    """Write a one-shot exec's stdin ``data`` to its ``attachment``, then end it.

    A stream that closes before it took all of ``data`` stops the feed, as ``communicate`` stops
    at a child's closed stdin: the engine closes it at the end of the exec's output, when the
    command exited without reading the rest, and the exit code the engine records then tells how
    the command ended. A connection lost while the command runs on leaves that exit code
    unrecorded, which raises.
    """
    if await attachment.write(data):
        attachment.end_input()


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
