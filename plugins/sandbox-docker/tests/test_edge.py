"""Edge branches: defensive rejections, idempotent no-ops, and handle lifetime."""

from __future__ import annotations

import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec, SandboxSpecRejectedError
from tai42_kit.sandbox import LABEL_DURABILITY, LABEL_SANDBOX, LABEL_WORKSPACE, permissive_policy

from tai42_sandbox_docker.provider import DockerSandbox, volume_name
from tai42_sandbox_docker.provider import _network_mode as network_mode
from tai42_sandbox_docker.sessions import DockerSandboxExecHandle, DockerSandboxSession, kill_exec_process
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeContainer, FakeDocker, FakeExec


def _settings() -> DockerSandboxSettings:
    return DockerSandboxSettings(host="tcp://engine:2376")


def _sandbox(fake_docker: FakeDocker) -> DockerSandbox:
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    return sandbox


def _spec(**overrides) -> SandboxSessionSpec:
    base = {
        "image": "img:1",
        "workspace_key": "ws1",
        "durability": "ephemeral",
        "network": "egress",
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return SandboxSessionSpec(**base)


def _container() -> FakeContainer:
    return FakeContainer(container_id="c1", name=None, config={})


def _session(container: FakeContainer) -> DockerSandboxSession:
    sandbox = DockerSandbox(docker=object(), settings=_settings())
    return DockerSandboxSession(
        sandbox=sandbox,
        session_id=container.id,
        container=container,
        workspace_key="ws1",
        durability="ephemeral",
        base_env={},
    )


def _handle(container: FakeContainer, exec_obj: FakeExec) -> DockerSandboxExecHandle:
    stream = exec_obj.start(detach=False)
    return DockerSandboxExecHandle(container=container, exec_obj=exec_obj, stream=stream, timeout_seconds=5)


# -- defensive provider branches -------------------------------------------------


def test_network_mode_rejects_unknown_tier() -> None:
    with pytest.raises(SandboxSpecRejectedError, match="network tier"):
        network_mode("wormhole")


async def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("SANDBOX_DOCKER_HOST", "unix:///var/run/docker.sock")
    reset_all_settings()
    sandbox = DockerSandbox()  # no settings override -> the cached accessor
    assert sandbox._settings().host == "unix:///var/run/docker.sock"
    reset_all_settings()


async def test_internal_network_get_error_reraised(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "network subsystem down")

    fake_docker.networks.get = _boom  # type: ignore[method-assign]
    sandbox = _sandbox(fake_docker)
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_internal_network(fake_docker)


async def test_destroy_when_volume_already_gone(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec(workspace_key="wsGone", durability="persistent"))
    del fake_docker.store_volumes[volume_name("wsGone")]  # externally removed
    await sandbox.destroy_session(session.id)  # a 404 on the volume is a no-op


async def test_reconcile_swallows_missing_ephemeral_orphan(fake_docker: FakeDocker) -> None:
    orphan = FakeContainer(
        container_id="orphan-eph",
        name=None,  # no Names -> display name falls back to the id
        config={"Labels": {LABEL_SANDBOX: "1", LABEL_DURABILITY: "ephemeral", LABEL_WORKSPACE: "x"}},
    )
    orphan.delete_error = 404  # already gone when the sweep tries to remove it
    fake_docker.store_containers.append(orphan)
    sandbox = _sandbox(fake_docker)
    handled = await sandbox.recover_orphans()
    assert any("orphan-eph" in line for line in handled)


# -- exec handle lifetime --------------------------------------------------------


async def test_write_stdin_on_closed_transport_raises() -> None:
    container = _container()
    exec_obj = await container.exec(cmd=["cat"])
    handle = _handle(container, exec_obj)
    handle._stream.write_error = RuntimeError("Cannot write to closed transport")
    with pytest.raises(SandboxError, match="closed"):
        await handle.write_stdin(b"x")
    assert handle._finished is True


async def test_write_stdin_engine_error_is_typed() -> None:
    container = _container()
    exec_obj = await container.exec(cmd=["cat"])
    handle = _handle(container, exec_obj)
    handle._stream.write_error = DockerError(500, "attach failed")
    with pytest.raises(SandboxError, match="docker engine error"):
        await handle.write_stdin(b"x")


async def test_close_stdin_after_finished_is_noop() -> None:
    container = _container()
    exec_obj = await container.exec(cmd=["cat"])
    handle = _handle(container, exec_obj)
    handle._finished = True
    await handle.close_stdin()  # returns without touching the stream


async def test_kill_when_already_exited() -> None:
    container = _container()
    exec_obj = await container.exec(cmd=["cat"])
    exec_obj.running = False
    handle = _handle(container, exec_obj)
    await handle.kill()
    assert handle._finished is True
    assert container.killed_signals == []  # no signal needed; the exec is gone


async def test_kill_when_inspect_fails_falls_back_to_container() -> None:
    container = _container()
    exec_obj = await container.exec(cmd=["cat"])
    exec_obj.inspect_raises = True
    handle = _handle(container, exec_obj)
    await handle.kill()
    assert container.killed_signals == ["SIGKILL"]


async def test_kill_exec_process_reraises_engine_error() -> None:
    container = _container()
    container.exec_raises = True  # the sibling kill exec cannot be created
    container.kill_error = 500  # and the container kill itself errors
    with pytest.raises(SandboxError, match="docker engine error"):
        await kill_exec_process(container, 123)


async def test_kill_exec_process_swallows_gone_container() -> None:
    container = _container()
    container.exec_raises = True
    container.kill_error = 404  # a gone container is a no-op fallback
    await kill_exec_process(container, 123)


# -- file transfer misses --------------------------------------------------------


async def test_get_file_engine_error_is_typed() -> None:
    container = _container()
    container.get_archive_error = 500
    session = _session(container)
    with pytest.raises(SandboxError, match="docker engine error"):
        await session.get_file("note.txt")


async def test_get_file_on_directory_member_raises() -> None:
    container = _container()
    container.files["/workspace/adir"] = None  # a directory, not a readable file
    session = _session(container)
    with pytest.raises(SandboxError, match="not a readable file"):
        await session.get_file("adir")
