"""Edge branches: defensive rejections, idempotent no-ops, and handle lifetime."""

from __future__ import annotations

import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec, SandboxSpecRejectedError
from tai42_kit.sandbox import LABEL_DURABILITY, LABEL_SANDBOX, LABEL_WORKSPACE, ExecDeadline, permissive_policy

from tai42_sandbox_docker.exec_control import EXEC_WRAPPER
from tai42_sandbox_docker.provider import DockerSandbox, volume_name
from tai42_sandbox_docker.provider import _network_mode as network_mode
from tai42_sandbox_docker.readiness import ENGINE_UNREACHABLE_STATUS
from tai42_sandbox_docker.sessions import DockerSandboxExecHandle, DockerSandboxSession, engine_error
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeContainer, FakeDocker, FakeExec, attach_fake


def _settings() -> DockerSandboxSettings:
    return DockerSandboxSettings(host="tcp://engine:2376")


def _sandbox(fake_docker: FakeDocker) -> DockerSandbox:
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
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
    sandbox = DockerSandbox(docker=object(), attach=container.attach_exec, settings=_settings())
    return DockerSandboxSession(
        sandbox=sandbox,
        session_id=container.id,
        container=container,
        workspace_key="ws1",
        durability="ephemeral",
        base_env={},
    )


async def _wrapped_cat(container: FakeContainer) -> FakeExec:
    return await container.exec(cmd=["sh", "-c", EXEC_WRAPPER, "x1", "cat"])


def _handle(container: FakeContainer, exec_obj: FakeExec) -> DockerSandboxExecHandle:
    return DockerSandboxExecHandle(
        container=container,
        exec_obj=exec_obj,
        exec_id="x1",
        attachment=attach_fake(exec_obj),
        deadline=ExecDeadline.from_now(5),
        retype=engine_error,
        attach=container.attach_exec,
    )


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


async def test_write_stdin_on_a_closed_stream_raises() -> None:
    container = _container()
    exec_obj = await _wrapped_cat(container)
    handle = _handle(container, exec_obj)
    assert exec_obj.attachment is not None
    exec_obj.attachment.engine_ends()  # the connection is lost under the handle
    with pytest.raises(SandboxError, match="closed"):
        await handle.write_stdin(b"x")
    with pytest.raises(SandboxError, match="stream is closed"):
        await handle.write_stdin(b"y")
    assert exec_obj.attachment.written == []


async def test_a_kill_that_loses_the_engine_makes_the_next_create_re_prove_the_firewall() -> None:
    container = _container()
    session = _session(container)
    session._sandbox._engine_verified = True
    handle = await session.exec_start(["cat"], timeout_seconds=5)
    container.exec_error = ENGINE_UNREACHABLE_STATUS
    container.execs[0].inspect_raises = True
    with pytest.raises(SandboxError, match="docker engine error"):
        await handle.kill()
    assert session._sandbox._engine_verified is False


async def test_close_stdin_after_finished_is_noop() -> None:
    container = _container()
    exec_obj = await _wrapped_cat(container)
    handle = _handle(container, exec_obj)
    handle._finished = True
    await handle.close_stdin()  # returns without touching the stream


async def test_kill_when_already_exited() -> None:
    container = _container()
    exec_obj = await _wrapped_cat(container)
    exec_obj.running = False
    handle = _handle(container, exec_obj)
    await handle.kill()
    # Nothing was left to kill: the stream, stdin included, stays as it was until it ends.
    assert handle._finished is False
    assert handle._killed_status is None
    assert exec_obj.attachment is not None
    assert not exec_obj.attachment.closed.is_set()


async def test_kill_after_the_session_is_destroyed_is_a_no_op(fake_docker: FakeDocker) -> None:
    # The container is gone, and every process in it with it: the exec has certainly exited.
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec())
    handle = await session.exec_start(["sleep", "9"], timeout_seconds=30)
    await session.destroy()
    await handle.kill()
    await handle.kill()


async def test_kill_engine_error_is_typed() -> None:
    container = _container()
    exec_obj = await _wrapped_cat(container)
    exec_obj.started = True  # the engine runs it, as once its attach is made
    handle = _handle(container, exec_obj)
    container.exec_raises = True  # the sibling kill exec cannot be created
    with pytest.raises(SandboxError, match="docker engine error"):
        await handle.kill()
    assert handle._stopped is False


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
