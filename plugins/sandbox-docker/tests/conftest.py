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

import tarfile
from io import BytesIO
from typing import Any

import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]

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


class FakeExec:
    """A fake engine exec whose attach stream replays canned output frames.

    ``block`` makes the stream hang on read so a host-side ``timeout_seconds`` fires,
    exercising the timeout→kill path.
    """

    def __init__(self, *, container: FakeContainer, cmd: list[str], frames: list[tuple[int, bytes]], exit_code: int):
        self.container = container
        self.cmd = cmd
        self._frames = frames
        self._exit_code = exit_code
        self.pid = 4242
        self.running = True
        self.started = False
        self.inspect_raises = False
        self.stream: FakeStream | None = None

    def start(self, *, detach: bool = False, **_: Any) -> FakeStream:
        self.started = True
        self.stream = FakeStream(self)
        return self.stream

    async def inspect(self) -> dict[str, Any]:
        if self.inspect_raises:
            raise DockerError(500, "exec inspect failed")
        return {"Running": self.running, "Pid": self.pid, "ExitCode": None if self.running else self._exit_code}

    def _finish(self) -> None:
        self.running = False


class FakeStream:
    def __init__(self, exec_obj: FakeExec) -> None:
        self._exec = exec_obj
        self._frames = list(exec_obj._frames)
        self._resp = None
        self.written: list[bytes] = []
        self.write_error: Exception | None = None

    async def _init(self) -> None:
        return None

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def read_out(self) -> Any:
        # A sibling `kill` exec must always drain, even while the primary exec blocks.
        if self._exec.container.block_reads and self._exec.cmd[:1] != ["kill"]:
            import asyncio

            await asyncio.Event().wait()  # hang until the host timeout cancels us
        if self._frames:
            stream_id, data = self._frames.pop(0)
            return _Message(stream_id, data)
        self._exec._finish()
        return None

    async def write_in(self, data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written.append(data)


class _Message:
    def __init__(self, stream: int, data: bytes) -> None:
        self.stream = stream
        self.data = data


class FakeContainer:
    def __init__(self, *, container_id: str, name: str | None, config: dict[str, Any]) -> None:
        self._id = container_id
        self.name = name
        self.config = config
        self.running = False
        self.deleted = False
        self.delete_calls: list[dict[str, bool]] = []
        self.killed_signals: list[str] = []
        self.block_reads = False
        self.execs: list[FakeExec] = []
        self.files: dict[str, bytes | None] = {}
        self.exec_frames: list[tuple[int, bytes]] = [(1, b"")]
        self.exec_exit_code = 0
        self.exec_raises = False
        self.delete_error: int | None = None
        self.kill_error: int | None = None
        self.get_archive_error: int | None = None

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
        if self.delete_error is not None:
            raise DockerError(self.delete_error, f"delete {self._id}: engine error")
        self.delete_calls.append({"force": force, "v": v})
        self.deleted = True

    async def kill(self, *, signal: str | None = None) -> None:
        if self.kill_error is not None:
            raise DockerError(self.kill_error, f"kill {self._id}: engine error")
        self.killed_signals.append(signal or "SIGKILL")

    async def exec(self, *, cmd: Any = None, **_: Any) -> FakeExec:
        if self.exec_raises:
            raise DockerError(500, "exec create failed")
        exec_obj = FakeExec(
            container=self,
            cmd=list(cmd),
            frames=list(self.exec_frames),
            exit_code=self.exec_exit_code,
        )
        self.execs.append(exec_obj)
        return exec_obj

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
        if name is not None and name in self._docker.containers_by_name:
            raise DockerError(409, f"conflict: container name {name!r} is already in use")
        container = FakeContainer(container_id=self._docker.next_id(), name=name, config=config)
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

    async def list(self, *, all: Any = None, filters: Any = None) -> list[FakeContainer]:
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
        self._id_counter = 0

    def next_id(self) -> str:
        self._id_counter += 1
        return f"container-{self._id_counter}"

    def seed_image(self, image: str) -> None:
        self.store_images.add(image)


@pytest.fixture
def fake_docker() -> FakeDocker:
    return FakeDocker()
