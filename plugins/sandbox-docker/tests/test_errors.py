"""Engine construction, connection loudness, and the idempotent/race edge paths."""

from __future__ import annotations

import ssl
from typing import Any

import aiohttp  # pyright: ignore[reportMissingImports]
import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxFileNotFoundError, SandboxSessionSpec
from tai42_kit.sandbox import LABEL_DURABILITY, permissive_policy

from tai42_sandbox_docker import provider as provider_module
from tai42_sandbox_docker.provider import INTERNAL_NETWORK, DockerSandbox, container_name, volume_name
from tai42_sandbox_docker.sessions import DockerSandboxSession
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeContainer, FakeDocker


def _settings(**overrides: Any) -> DockerSandboxSettings:
    base: dict[str, Any] = {"host": "tcp://engine:2376"}
    base.update(overrides)
    return DockerSandboxSettings(**base)


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


# -- engine construction ---------------------------------------------------------


async def test_engine_returns_injected_client_cached() -> None:
    client = object()
    sandbox = DockerSandbox(docker=client, settings=_settings())
    assert await sandbox._engine() is client
    assert await sandbox._engine() is client  # cached


@pytest.mark.parametrize("host", ["unix:///var/run/docker.sock", "/var/run/docker.sock"])
async def test_a_socket_host_is_dialed_through_a_unix_connector(host: str) -> None:
    sandbox = DockerSandbox(settings=_settings(host=host))
    client = await sandbox._engine()
    assert client.docker_host == "unix://localhost"
    assert isinstance(client.connector, aiohttp.UnixConnector)
    assert client.connector.path == "/var/run/docker.sock"
    await client.close()
    await client.connector.close()


async def test_an_mtls_host_is_dialed_over_https_through_a_tcp_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = DockerSandbox(settings=_settings(host="tcp://engine:2376", tls_verify=True))
    monkeypatch.setattr(sandbox, "_build_ssl_context", lambda settings: ssl.create_default_context())
    client = await sandbox._engine()
    # Under mTLS the tcp:// endpoint is normalized to https:// so aiodocker runs TLS
    # over the client identity (a bare tcp:// would dial plaintext against :2376).
    assert client.docker_host == "https://engine:2376"
    assert isinstance(client.connector, aiohttp.TCPConnector)
    await client.close()
    await client.connector.close()


@pytest.mark.parametrize("host", ["tcp://engine:2375", "https://engine:2376"])
async def test_a_network_host_without_verify_is_dialed_as_given_through_a_tcp_connector(host: str) -> None:
    sandbox = DockerSandbox(settings=_settings(host=host, tls_verify=False))
    client = await sandbox._engine()
    assert client.docker_host == host
    assert isinstance(client.connector, aiohttp.TCPConnector)
    await client.close()
    await client.connector.close()


async def test_engine_connection_failure_names_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_, **__):
        raise OSError("connection refused")

    monkeypatch.setattr(provider_module, "rest_connector", _boom)
    sandbox = DockerSandbox(settings=_settings(host="tcp://engine:2376"))
    # The mTLS identity loads cleanly; the engine client's construction ITSELF refuses the
    # connection, so the raised error names SANDBOX_DOCKER_HOST — not the cert paths.
    monkeypatch.setattr(sandbox, "_build_ssl_context", lambda settings: None)
    with pytest.raises(SandboxError, match="SANDBOX_DOCKER_HOST"):
        await sandbox._engine()


def test_build_ssl_context_missing_certs_raises() -> None:
    sandbox = DockerSandbox(settings=_settings())
    with pytest.raises(SandboxError, match="mTLS client identity"):
        sandbox._build_ssl_context(_settings(tls_ca_path="/nonexistent/ca.pem"))


# -- create / adopt race ---------------------------------------------------------


class _RaceContainers:
    """containers.get misses on the pre-check then adopts after a 409 create race."""

    def __init__(self, existing: FakeContainer) -> None:
        self.existing = existing
        self._get_calls = 0

    async def get(self, name: str) -> FakeContainer:
        self._get_calls += 1
        if self._get_calls == 1:
            raise DockerError(404, "no such container")
        return self.existing

    async def create(self, config, *, name=None):
        raise DockerError(409, "conflict")


async def test_create_or_adopt_loses_name_race(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    existing = FakeContainer(container_id="race", name=container_name("wsR"), config={})
    existing.running = True  # already running: no re-start needed
    fake_docker.containers = _RaceContainers(existing)  # type: ignore[assignment]

    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsR", durability="persistent"))
    assert session.id == "race"


async def test_create_or_adopt_reraises_non_409_create_error(fake_docker: FakeDocker) -> None:
    async def _fail(config, *, name=None):
        raise DockerError(500, "daemon error creating container")

    fake_docker.containers.create = _fail  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    # No existing container of the name: the create is attempted and a non-409 engine
    # failure is surfaced loudly, not adopted.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._create_or_adopt(fake_docker, {}, "wsAdopt")


async def test_get_container_reraises_non_404(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "engine down")

    fake_docker.containers.get = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._get_container(fake_docker, "any")


# -- ensure helpers --------------------------------------------------------------


async def test_ensure_volume_adopts_existing(fake_docker: FakeDocker) -> None:
    from .conftest import FakeVolume

    fake_docker.store_volumes[volume_name("wsX")] = FakeVolume(volume_name("wsX"))
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    await sandbox._ensure_volume(fake_docker, _spec(workspace_key="wsX", durability="persistent"))
    # No second volume created; the existing one is adopted.
    assert list(fake_docker.store_volumes) == [volume_name("wsX")]


async def test_ensure_volume_reraises_non_404(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "volume backend down")

    fake_docker.volumes.get = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_volume(fake_docker, _spec(workspace_key="wsVe", durability="persistent"))


async def test_ensure_image_reraises_non_404(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "registry error")

    fake_docker.images.inspect = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_image(fake_docker, "img:1", "missing")


async def test_ensure_image_pull_error_is_loud(fake_docker: FakeDocker) -> None:
    async def _boom(*, from_image, **_):
        raise DockerError(500, "registry unreachable")

    fake_docker.images.pull = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    # Image absent and pull_policy allows pulling: a pull engine error is surfaced loudly.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_image(fake_docker, "absent:1", "missing")


async def test_ensure_internal_network_adopts_existing(fake_docker: FakeDocker) -> None:
    fake_docker.store_networks.add(INTERNAL_NETWORK)
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    await sandbox._ensure_internal_network(fake_docker)
    assert fake_docker.network_configs == []  # nothing created


async def test_ensure_internal_network_loses_create_race(fake_docker: FakeDocker) -> None:
    async def _conflict(config):
        raise DockerError(409, "network already exists")

    fake_docker.networks.create = _conflict  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    await sandbox._ensure_internal_network(fake_docker)  # adopts, no raise


async def test_ensure_internal_network_create_error_is_loud(fake_docker: FakeDocker) -> None:
    async def _boom(config):
        raise DockerError(500, "network driver failure")

    fake_docker.networks.create = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    # A non-409 create failure is a genuine engine error, not a lost race: surfaced loudly.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_internal_network(fake_docker)


# -- connection loudness on the primitives --------------------------------------


async def test_create_session_connection_failure_is_loud(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")

    async def _refuse(config, *, name=None):
        raise OSError("connection reset")

    fake_docker.containers.create = _refuse  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    with pytest.raises(SandboxError, match="SANDBOX_DOCKER_HOST"):
        await sandbox.create_session(_spec(workspace_key="wsC"))


async def test_create_session_engine_error_is_typed(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")

    async def _fail(config, *, name=None):
        raise DockerError(500, "daemon error creating container")

    fake_docker.containers.create = _fail  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox.create_session(_spec(workspace_key="wsErr"))


async def test_orphan_sweep_connection_failure_is_loud(fake_docker: FakeDocker) -> None:
    async def _refuse(**_):
        raise OSError("connection reset")

    fake_docker.containers.list = _refuse  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    with pytest.raises(SandboxError, match="SANDBOX_DOCKER_HOST"):
        await sandbox.recover_orphans()


# -- teardown error paths --------------------------------------------------------


async def test_reap_does_not_swallow_missing_container(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsRp"))
    fake_docker.store_containers[0].delete_error = 404

    record = sandbox._ledger[session.id]
    record.expires_at = record.created_at
    # A reap (remove_workspace=False) surfaces a genuine engine error rather than
    # swallowing it the way an explicit destroy does.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox.reap()


async def test_destroy_reraises_non_404_container_error(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsDe"))
    fake_docker.store_containers[0].delete_error = 500
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox.destroy_session(session.id)


async def test_destroy_reraises_non_404_volume_get_error(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsVg", durability="persistent"))

    async def _boom(name):
        raise DockerError(500, "volume lookup failed")

    # The container tears down cleanly; a non-404 failure looking up the durable volume
    # for removal is surfaced loudly, never swallowed.
    fake_docker.volumes.get = _boom  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox.destroy_session(session.id)


# -- orphan reconciliation -------------------------------------------------------


async def test_reconcile_orphans_skips_ledgered_and_sweeps_orphan(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsLive"))
    ledgered = fake_docker.store_containers[0]
    assert session.id in sandbox._ledger

    orphan = FakeContainer(
        container_id="orphan-1",
        name="tai-sbx-stray",
        config={"Labels": {LABEL_DURABILITY: "ephemeral"}},
    )
    fake_docker.store_containers.append(orphan)

    handled = await sandbox._reconcile_orphans(fake_docker)

    # The already-ledgered live session is skipped; only the unknown ephemeral is swept.
    assert ledgered.deleted is False
    assert orphan.deleted is True
    assert handled == ["destroyed orphan ephemeral container tai-sbx-stray"]


async def test_reconcile_orphans_delete_error_is_loud(fake_docker: FakeDocker) -> None:
    sandbox = DockerSandbox(docker=fake_docker, attach=fake_docker.attach_exec, settings=_settings())
    orphan = FakeContainer(
        container_id="orphan-2",
        name="tai-sbx-bad",
        config={"Labels": {LABEL_DURABILITY: "ephemeral"}},
    )
    orphan.delete_error = 500
    fake_docker.store_containers.append(orphan)
    # A non-404 failure destroying an orphan is a genuine engine error: surfaced loudly.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._reconcile_orphans(fake_docker)


# -- get_file miss typing --------------------------------------------------------


async def test_get_file_404_raises_typed_not_found() -> None:
    container = FakeContainer(container_id="cGf", name=None, config={})
    session = _session(container)
    # The workspace has no such path: the engine's archive endpoint 404s, which is a
    # genuine miss and surfaces as the typed subclass carrying the requested path.
    with pytest.raises(SandboxFileNotFoundError) as excinfo:
        await session.get_file("absent.txt")
    assert excinfo.value.path == "absent.txt"
    assert str(excinfo.value) == "sandbox file 'absent.txt' not found"


async def test_get_file_non_404_raises_base_sandbox_error() -> None:
    container = FakeContainer(container_id="cGe", name=None, config={})
    container.get_archive_error = 500
    session = _session(container)
    # A non-404 engine error is a real fault, not a miss: it stays the base error and is
    # never mistaken for the not-found subclass.
    with pytest.raises(SandboxError) as excinfo:
        await session.get_file("note.txt")
    assert not isinstance(excinfo.value, SandboxFileNotFoundError)
