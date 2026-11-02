"""Bind a recording stub app to the ``tai42_app`` handle before the plugin is
imported, and provide the in-memory Docker engine fakes the unit suite drives.

The plugin registers its provider through ``tai42_app`` at import time; binding the
stub here (at collection, before any test imports the plugin) captures that
registration so tests can assert on it. No test in this suite needs a live Docker
engine — the spec→payload mapping, the idempotent adopt path, the timeout→kill path,
and the reap/destroy volume behaviour all run against the fakes below. The live-engine
conformance leg is gated behind the ``docker`` marker (see ``test_conformance``).
"""

from __future__ import annotations

import asyncio
import tarfile
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxStreamChunk

if TYPE_CHECKING:
    from tai42_sandbox_docker.attach import ExecAttachment

# -- The recording stub app ------------------------------------------------------


class StubSandboxes:
    def __init__(self) -> None:
        self.registered_cls: type | None = None

    def register_sandbox(self, cls: type) -> type:
        self.registered_cls = cls
        return cls


class StubApp:
    def __init__(self) -> None:
        self.sandboxes = StubSandboxes()


from tai42_contract.app import tai42_app  # noqa: E402

_stub_app = StubApp()
tai42_app.bind(_stub_app)

# Imported AFTER the bind so the import-time registration lands in the stub.
import tai42_sandbox_docker  # noqa: E402,F401


@pytest.fixture
def stub_app() -> StubApp:
    return _stub_app


# -- In-memory Docker engine fakes ----------------------------------------------


def _not_found(kind: str, ident: str) -> DockerError:
    return DockerError(404, f"no such {kind}: {ident}")


def is_kill_exec(cmd: list[str]) -> bool:
    """Whether ``cmd`` is the provider's sibling kill exec."""
    from tai42_sandbox_docker.exec_control import KILL_WRAPPER  # imported after the stub app binds

    return cmd[:3] == ["sh", "-c", KILL_WRAPPER]


def wrapped_exec_id(cmd: list[str]) -> str | None:
    """The exec id of a ``cmd`` run under the provider's exec wrapper, else ``None``."""
    from tai42_sandbox_docker.exec_control import EXEC_WRAPPER  # imported after the stub app binds

    return cmd[3] if cmd[:3] == ["sh", "-c", EXEC_WRAPPER] else None


class FakeExec:
    """A fake engine exec whose attach stream replays canned output frames.

    ``block`` makes the stream hang on read so a host-side ``timeout_seconds`` fires,
    exercising the timeout→kill path; closing the stream ends a hanging read.
    """

    def __init__(
        self, *, container: FakeContainer, exec_id: str, cmd: list[str], frames: list[tuple[int, bytes]], exit_code: int
    ):
        self.container = container
        self.id = exec_id
        self.cmd = cmd
        self._frames = frames
        self._exit_code = exit_code
        self.running = True
        self.started = False
        self.inspect_raises = False
        # When set, the engine never answers the exec's inspect.
        self.inspect_hangs = False
        self.inspect_calls = 0
        self.killed = False
        # The stream of an exec the provider attached to.
        self.attachment: FakeAttachment | None = None

    async def inspect(self) -> dict[str, Any]:
        self.inspect_calls += 1
        if self.inspect_hangs:
            await asyncio.Event().wait()
        return self._inspect()

    def _inspect(self) -> dict[str, Any]:
        if self.inspect_raises:
            raise DockerError(500, "exec inspect failed")
        if self.container.inspect_failures and wrapped_exec_id(self.cmd) is not None:
            self.container.inspect_failures -= 1
            raise DockerError(500, "exec inspect failed")
        if self.container.deleted:
            # The engine forgets every exec of a removed container.
            raise _not_found("exec instance", self.cmd[3] if len(self.cmd) > 3 else "")
        # The engine reports an exec it has not started as not running, with no exit code.
        running = self.started and self.running
        ended = self.started and not self.running
        return {"Running": running, "ExitCode": self._exit_code if ended else None}

    def _finish(self) -> None:
        self.running = False


class FakeAttachment:
    """The attached stream of an exec, as :class:`~tai42_sandbox_docker.attach.ExecAttachment` presents it.

    The output replays the exec's frames, then ends as the command exits. The stream ends at
    the engine's end of it (:meth:`engine_ends`, as when the command exited or the connection
    was lost; the output the engine sent before it is still read) or at the provider's
    :meth:`close`; a write after either reports the stream closed. The chunks in
    :attr:`received` are output the attachment already holds: they are read first, and a
    close keeps them, as the attachment's own close keeps whole frames already received.
    """

    def __init__(self, exec_obj: FakeExec) -> None:
        self._exec = exec_obj
        self._frames = list(exec_obj._frames)
        self.written: list[bytes] = []
        self.input_ended = False
        self.ended = False
        # Set by the provider's close of the stream.
        self.closed = asyncio.Event()
        self._ending = asyncio.Event()
        self.received: list[SandboxStreamChunk] = list(exec_obj.container.received)

    async def read(self) -> SandboxStreamChunk | None:
        if self.received:
            return self.received.pop(0)
        if self.closed.is_set():
            return None  # the provider closed the stream: no more output reaches it
        if self._exec.container.block_reads:
            await self._ending.wait()  # hang until the host timeout cancels us or the stream ends
            # Output the engine delivered before its end of the stream is still read.
            return self.received.pop(0) if self.received and not self.closed.is_set() else None
        if self._frames:
            stream_id, data = self._frames.pop(0)
            return SandboxStreamChunk(stream="stderr" if stream_id == 2 else "stdout", data=data)
        self._end()
        if not self._exec.container.attach_drops:
            self._exec._finish()
        # With ``attach_drops`` the attach connection is lost: the stream ends while the command runs on.
        return None

    async def write(self, data: bytes) -> bool:
        if self._exec.container.stdin_ends_stream:
            self.engine_ends()
        if self.ended or self.input_ended:
            return False
        self.written.append(data)
        return True

    def take_received(self) -> list[SandboxStreamChunk]:
        taken, self.received = self.received, []
        return taken

    def end_input(self) -> None:
        if self.ended:
            raise SandboxError("cannot close stdin: the sandbox exec stream is closed")
        self.input_ended = True

    def close(self) -> None:
        self.closed.set()
        self.engine_ends()

    def engine_ends(self) -> None:
        """End the stream from the engine's side: a waiting read ends, and nothing more is written.

        The output the engine sent before its end, the exec's frames, is still read.
        """
        self._end()
        self._ending.set()

    def _end(self) -> None:
        self.ended = True


def attach_fake(exec_obj: FakeExec) -> ExecAttachment:
    """Start ``exec_obj`` attached to a :class:`FakeAttachment`, typed as the attachment it stands in for."""
    exec_obj.started = True
    exec_obj.attachment = FakeAttachment(exec_obj)
    return cast("ExecAttachment", exec_obj.attachment)


class FakeKillAttachment:
    """The attached stream of a sibling kill exec: it replays the kill's output, then the kill exits.

    With the container's ``kill_stream_held`` the engine holds the kill's end once its output is
    read: the stream stays open until the provider closes it, and the kill's exit is never
    recorded. The faults a container models for its commands' streams never reach it.
    """

    def __init__(self, exec_obj: FakeExec) -> None:
        self._exec = exec_obj
        self._frames = list(exec_obj._frames)
        self._closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def read(self) -> SandboxStreamChunk | None:
        if self._frames:
            stream_id, data = self._frames.pop(0)
            return SandboxStreamChunk(stream="stderr" if stream_id == 2 else "stdout", data=data)
        if self._exec.container.kill_stream_held:
            await self._closed.wait()
            return None
        self._exec._finish()
        return None

    def close(self) -> None:
        self._closed.set()


class FakeContainer:
    def __init__(self, *, container_id: str, name: str | None, config: dict[str, Any]) -> None:
        self._id = container_id
        self.name = name
        self.config = config
        self.running = False
        self.deleted = False
        self.delete_calls: list[dict[str, bool]] = []
        self.block_reads = False
        # When set, the attach stream of every exec the provider attaches to ends after its
        # frames while the exec runs on, as when the engine connection is lost.
        self.attach_drops = False
        self.execs: list[FakeExec] = []
        self.files: dict[str, bytes | None] = {}
        self.exec_frames: list[tuple[int, bytes]] = [(1, b"")]
        # Output every exec's attachment already holds when it opens (see ``FakeAttachment.received``).
        self.received: list[SandboxStreamChunk] = []
        self.exec_exit_code = 0
        self.exec_raises = False
        # A custom engine status for an ``exec`` create failure (e.g. 900 = the daemon
        # went away), so the readiness reset-on-disconnect path can be exercised.
        self.exec_error: int | None = None
        # Per-command canned output: maps a command argv to ``(frames, exit_code)`` so
        # the readiness probe's ``cat /etc/resolv.conf`` + two ``nc`` dials each get
        # their own scripted result.
        self.exec_by_cmd: Callable[[list[str]], tuple[list[tuple[int, bytes]], int]] | None = None
        self.delete_error: int | None = None
        # When set, ``delete`` raises a connection-level ``OSError`` instead of an
        # engine ``DockerError`` — the daemon-went-away signal a teardown can observe.
        self.delete_conn_error = False
        # When set, every sibling kill exec writes this line on its stderr and exits 1 — a kill
        # that failed inside the container — instead of signalling its target.
        self.kill_failure: bytes | None = None
        # When set, every sibling kill exec writes these output frames and exits 0, in place of
        # the kill's own report.
        self.kill_output: list[tuple[int, bytes]] | None = None
        # When set, the engine holds the end of every sibling kill's stream and its exit code, as
        # while another reader of the container's output has paused with the engine's buffer full.
        self.kill_stream_held = False
        # When set, every sibling kill finds no pidfile or no group though the engine shows its
        # target running, as when the command has just exited or removed its own pidfile.
        self.kill_finds_nothing = False
        # When set, a process the command started still holds the exec's output once a sibling
        # kill is done: the kill reports it, and the stream of an exec it signalled stays open.
        self.kill_leaves_output_held = False
        # When set, the command exits by itself just as its kill signals the process group its
        # unreaped leader still holds: the kill reports it signalled, and the exec keeps its own
        # exit code.
        self.kill_races_exit = False
        # Output still on its way from an exec when a sibling kill signals it: it reaches the
        # exec's attachment before the engine ends the stream.
        self.output_at_kill: list[SandboxStreamChunk] = []
        # When set, the engine never records the exit code of an exec a kill signalled, as while
        # it holds the exec's end behind another reader of the container's output.
        self.killed_exit_unrecorded = False
        # When set, the stream of an exec a kill signalled stays open though the kill found
        # nothing holding its output, as when the engine is slow to end it.
        self.killed_stream_stays_open = False
        # When set, a sibling kill takes this many seconds to reach the engine before it acts, as
        # a real engine round trip does, so a cancellation can land inside it; the event is set
        # once a kill is on its way.
        self.kill_round_trip = 0.0
        self.kill_requested = asyncio.Event()
        self.get_archive_error: int | None = None
        # How many inspects of a wrapped exec fail with an engine error before they answer again.
        self.inspect_failures = 0
        # When set, the provider's attach to an exec raises this engine error.
        self.attach_error: DockerError | None = None
        # When set, the provider's attach runs this on the started exec's stream before it
        # returns: to hang there, fail there, or have the engine end the stream there.
        self.attach_hook: Callable[[FakeAttachment], Awaitable[None]] | None = None
        # When set, the engine ends every attached stream as a stdin write reaches it, as when
        # the command exits, or the connection is lost, while stdin is still buffered.
        self.stdin_ends_stream = False

    @property
    def id(self) -> str:
        return self._id

    def __contains__(self, key: str) -> bool:
        return key in ("Labels", "Names")

    def __getitem__(self, key: str) -> Any:
        if key == "Labels":
            return self.config.get("Labels", {})
        if key == "Names":
            return [f"/{self.name}"] if self.name else []
        raise KeyError(key)

    async def show(self) -> dict[str, Any]:
        return {
            "State": {"Running": self.running},
            "Config": {"Labels": self.config.get("Labels", {})},
            "Name": f"/{self.name}" if self.name else "",
        }

    async def start(self) -> None:
        self.running = True

    async def delete(self, *, force: bool = False, v: bool = False) -> None:
        if self.delete_conn_error:
            raise OSError("connection reset by the engine")
        if self.delete_error is not None:
            raise DockerError(self.delete_error, f"delete {self._id}: engine error")
        self.delete_calls.append({"force": force, "v": v})
        self.deleted = True

    async def exec(self, *, cmd: Any = None, **_: Any) -> FakeExec:
        if self.deleted:
            raise _not_found("container", self._id)
        if self.exec_error is not None:
            raise DockerError(self.exec_error, f"exec {self._id}: engine error")
        if self.exec_raises:
            raise DockerError(500, "exec create failed")
        cmd_list = list(cmd)
        if is_kill_exec(cmd_list):
            self.kill_requested.set()
            if self.kill_round_trip:
                await asyncio.sleep(self.kill_round_trip)
            # The kill acts once its attach starts it (see attach_exec).
            frames, exit_code = [], 0
        elif self.exec_by_cmd is not None:
            frames, exit_code = self.exec_by_cmd(cmd_list)
        else:
            frames, exit_code = list(self.exec_frames), self.exec_exit_code
        exec_obj = FakeExec(
            container=self,
            exec_id=f"{self._id}-exec-{len(self.execs) + 1}",
            cmd=cmd_list,
            frames=list(frames),
            exit_code=exit_code,
        )
        self.execs.append(exec_obj)
        return exec_obj

    async def attach_exec(self, exec_id: str) -> ExecAttachment:
        """The provider's exec attach: the engine starts exec ``exec_id`` as the attach reaches it."""
        exec_obj = next(e for e in self.execs if e.id == exec_id)
        if is_kill_exec(exec_obj.cmd):
            # The engine starts the sibling kill as its attach reaches it: the kill acts now.
            exec_obj._frames, exec_obj._exit_code = self._kill(exec_obj.cmd[3])
            exec_obj.started = True
            return cast("ExecAttachment", FakeKillAttachment(exec_obj))
        if self.attach_error is not None:
            raise self.attach_error
        attachment = attach_fake(exec_obj)
        if self.attach_hook is not None:
            try:
                await self.attach_hook(cast(FakeAttachment, attachment))
            except BaseException:
                attachment.close()  # an attach that fails or is cancelled closes its stream
                raise
        return attachment

    def _kill(self, exec_id: str) -> tuple[list[tuple[int, bytes]], int]:
        """Model the kill wrapper: its report on its stdout and exit 0, or its failure on its stderr and exit 1.

        A kill that signalled the group reports the status the command ended with as a line of
        its own before its finding, each its own frame.
        """
        if self.kill_failure is not None:
            return [(2, self.kill_failure)], 1
        if self.kill_output is not None:
            return list(self.kill_output), 0
        finding, status = self._kill_finding(exec_id)
        status_line = [] if status is None else [(1, f"{status}\n".encode())]
        return [*status_line, (1, finding.encode() + b"\n")], 0

    def _kill_finding(self, exec_id: str) -> tuple[str, int | None]:
        """Signal the running exec ``exec_id``, or find nothing to kill: the kill's finding, and its status line."""
        from tai42_kit.sandbox.exec_group_scripts import (
            KILL_NOTHING_RUNNING,
            KILL_NOTHING_RUNNING_OUTPUT_HELD,
            KILL_SIGNALLED,
            KILL_SIGNALLED_OUTPUT_HELD,
            KILLED_EXIT_CODE,
        )

        held = self.kill_leaves_output_held
        nothing_running = KILL_NOTHING_RUNNING_OUTPUT_HELD if held else KILL_NOTHING_RUNNING
        if self.kill_finds_nothing:
            return nothing_running, None
        for target in self.execs:
            if wrapped_exec_id(target.cmd) == exec_id and target.started and target.running:
                # The wrapper reaps the signalled command and records its status, which the kill
                # reports: the killed status, or the command's own when it exited by itself first.
                # The engine records the exec's end only once nothing holds its output.
                target.running = self.killed_exit_unrecorded or held
                target.killed = True
                if not self.kill_races_exit:
                    target._exit_code = KILLED_EXIT_CODE
                if target.attachment is not None:
                    target.attachment.received.extend(self.output_at_kill)
                if target.attachment is not None and not (held or self.killed_stream_stays_open):
                    # The signalled wrapper exits, and the engine ends its stream.
                    target.attachment.engine_ends()
                return (KILL_SIGNALLED_OUTPUT_HELD if held else KILL_SIGNALLED), target._exit_code
        return nothing_running, None

    async def put_archive(self, path: str, data: bytes) -> None:
        with tarfile.open(fileobj=BytesIO(data)) as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                content = extracted.read() if extracted is not None else b""
                self.files[f"{path.rstrip('/')}/{member.name}"] = content

    async def get_archive(self, path: str) -> tarfile.TarFile:
        if self.get_archive_error is not None:
            raise DockerError(self.get_archive_error, f"archive {path}: engine error")
        if path not in self.files:
            raise _not_found("file", path)
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            data = self.files[path]
            name = path.rsplit("/", 1)[-1]
            if data is None:
                # A directory member: ``extractfile`` returns None for a non-regular file.
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, BytesIO(data))
        buffer.seek(0)
        return tarfile.open(fileobj=buffer)


class FakeVolume:
    def __init__(self, name: str, *, in_use: bool = False) -> None:
        self.name = name
        self.in_use = in_use
        self.deleted = False
        self.delete_forced: bool | None = None

    async def delete(self, force: bool = False) -> None:
        if self.in_use and not force:
            raise DockerError(409, f"remove {self.name}: volume is in use")
        self.delete_forced = force
        self.deleted = True


class _Containers:
    def __init__(self, docker: FakeDocker) -> None:
        self._docker = docker

    async def create(self, config: dict[str, Any], *, name: str | None = None) -> FakeContainer:
        if self._docker.create_conn_error:
            raise OSError("connection reset by the engine")
        if self._docker.create_error is not None:
            raise DockerError(self._docker.create_error, "container create failed")
        if name is not None and name in self._docker.containers_by_name:
            raise DockerError(409, f"conflict: container name {name!r} is already in use")
        container = FakeContainer(container_id=self._docker.next_id(), name=name, config=config)
        # A newly-created container (a session OR the readiness-probe container the
        # provider creates internally) inherits the engine's configured exec behaviour.
        container.exec_by_cmd = self._docker.default_exec_by_cmd
        container.block_reads = self._docker.default_block_reads
        container.delete_error = self._docker.default_delete_error
        self._docker.store_containers.append(container)
        if name is not None:
            self._docker.containers_by_name[name] = container
        return container

    async def get(self, ident: str) -> FakeContainer:
        if ident in self._docker.containers_by_name:
            return self._docker.containers_by_name[ident]
        for container in self._docker.store_containers:
            if container.id == ident:
                return container
        raise _not_found("container", ident)

    # ``all`` mirrors the docker-py containers.list(all, filters) signature this fake stands in for.
    async def list(self, *, all: Any = None, filters: Any = None) -> list[FakeContainer]:  # noqa: A002
        return [c for c in self._docker.store_containers if not c.deleted]


class _Volumes:
    def __init__(self, docker: FakeDocker) -> None:
        self._docker = docker

    async def get(self, name: str) -> FakeVolume:
        if name in self._docker.store_volumes:
            return self._docker.store_volumes[name]
        raise _not_found("volume", name)

    async def create(self, config: dict[str, Any]) -> FakeVolume:
        if self._docker.volume_create_fails:
            raise DockerError(500, "volume driver unavailable")
        volume = FakeVolume(config["Name"])
        self._docker.store_volumes[config["Name"]] = volume
        return volume

    async def list(self, *, filters: Any = None) -> dict[str, Any]:
        return {"Volumes": [{"Name": name} for name in self._docker.store_volumes]}


class _Images:
    def __init__(self, docker: FakeDocker) -> None:
        self._docker = docker

    async def inspect(self, name: str) -> dict[str, Any]:
        if name in self._docker.store_images:
            return {"Id": name}
        raise _not_found("image", name)

    async def pull(self, *, from_image: str, **_: Any) -> list[dict[str, Any]]:
        self._docker.pulled.append(from_image)
        self._docker.store_images.add(from_image)
        return []


class _Networks:
    def __init__(self, docker: FakeDocker) -> None:
        self._docker = docker

    async def get(self, name: str) -> Any:
        if name in self._docker.store_networks:
            return object()
        raise _not_found("network", name)

    async def create(self, config: dict[str, Any]) -> Any:
        self._docker.store_networks.add(config["Name"])
        self._docker.network_configs.append(config)
        return object()


class FakeDocker:
    """An in-memory stand-in for ``aiodocker.Docker`` covering the surface the
    provider drives."""

    def __init__(self) -> None:
        self.containers = _Containers(self)
        self.volumes = _Volumes(self)
        self.images = _Images(self)
        self.networks = _Networks(self)
        self.store_containers: list[FakeContainer] = []
        self.containers_by_name: dict[str, FakeContainer] = {}
        self.store_volumes: dict[str, FakeVolume] = {}
        self.store_images: set[str] = set()
        self.store_networks: set[str] = set()
        self.network_configs: list[dict[str, Any]] = []
        self.pulled: list[str] = []
        self.volume_create_fails = False
        # Applied to every container this engine creates (sessions and the internal
        # readiness-probe container alike).
        self.default_exec_by_cmd: Callable[[list[str]], tuple[list[tuple[int, bytes]], int]] | None = None
        self.default_block_reads = False
        # When set, the next ``containers.create`` raises ``DockerError(create_error)``
        # (the readiness probe container is the FIRST create on a gated path).
        self.create_error: int | None = None
        # When set, the next ``containers.create`` raises a connection-level ``OSError``.
        self.create_conn_error = False
        # Applied as ``delete_error`` to every container this engine creates.
        self.default_delete_error: int | None = None
        self._id_counter = 0

    def next_id(self) -> str:
        self._id_counter += 1
        return f"container-{self._id_counter}"

    async def attach_exec(self, exec_id: str) -> ExecAttachment:
        """The provider's exec attach, to an exec of any container this engine holds."""
        container = next(c for c in self.store_containers if any(e.id == exec_id for e in c.execs))
        return await container.attach_exec(exec_id)

    def seed_image(self, image: str) -> None:
        self.store_images.add(image)


@pytest.fixture
def fake_docker() -> FakeDocker:
    return FakeDocker()
