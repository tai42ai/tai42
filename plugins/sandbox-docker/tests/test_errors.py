"""Engine construction, connection loudness, and the idempotent/race edge paths."""

from __future__ import annotations

from typing import Any

import pytest
from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec
from tai42_kit.sandbox import LABEL_DURABILITY, permissive_policy

from tai42_sandbox_docker import provider as provider_module
from tai42_sandbox_docker.provider import INTERNAL_NETWORK, DockerSandbox, container_name, volume_name
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


class _DockerRecorder:
    def __init__(self, *, url=None, ssl_context=None) -> None:
        self.url = url
        self.ssl_context = ssl_context


# -- engine construction ---------------------------------------------------------


async def test_engine_returns_injected_client_cached() -> None:
    client = object()
    sandbox = DockerSandbox(docker=client, settings=_settings())
    assert await sandbox._engine() is client
    assert await sandbox._engine() is client  # cached


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [
        ("unix:///var/run/docker.sock", "unix:///var/run/docker.sock"),
        ("/var/run/docker.sock", "unix:///var/run/docker.sock"),
    ],
)
async def test_create_engine_socket_hosts(monkeypatch: pytest.MonkeyPatch, host: str, expected_url: str) -> None:
    monkeypatch.setattr(provider_module, "Docker", _DockerRecorder)
    sandbox = DockerSandbox(settings=_settings(host=host))
    client = await sandbox._engine()
    assert client.url == expected_url
    assert client.ssl_context is None


async def test_create_engine_tcp_with_mtls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "Docker", _DockerRecorder)
    sandbox = DockerSandbox(settings=_settings(host="tcp://engine:2376", tls_verify=True))
    monkeypatch.setattr(sandbox, "_build_ssl_context", lambda settings: "SSL-CONTEXT")
    client = await sandbox._engine()
    assert client.url == "tcp://engine:2376"
    assert client.ssl_context == "SSL-CONTEXT"


async def test_create_engine_tcp_without_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "Docker", _DockerRecorder)
    sandbox = DockerSandbox(settings=_settings(host="tcp://engine:2376", tls_verify=False))
    client = await sandbox._engine()
    assert client.ssl_context is None


async def test_engine_connection_failure_names_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_):
        raise OSError("connection refused")

    monkeypatch.setattr(provider_module, "Docker", _boom)
    sandbox = DockerSandbox(settings=_settings(host="tcp://engine:2376"))
    # The mTLS identity loads cleanly; the engine construction ITSELF refuses the
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

    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsR", durability="persistent"))
    assert session.id == "race"


async def test_create_or_adopt_reraises_non_409_create_error(fake_docker: FakeDocker) -> None:
    async def _fail(config, *, name=None):
        raise DockerError(500, "daemon error creating container")

    fake_docker.containers.create = _fail  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    # No existing container of the name: the create is attempted and a non-409 engine
    # failure is surfaced loudly, not adopted.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._create_or_adopt(fake_docker, {}, "wsAdopt")


async def test_get_container_reraises_non_404(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "engine down")

    fake_docker.containers.get = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._get_container(fake_docker, "any")


# -- ensure helpers --------------------------------------------------------------


async def test_ensure_volume_adopts_existing(fake_docker: FakeDocker) -> None:
    from .conftest import FakeVolume

    fake_docker.store_volumes[volume_name("wsX")] = FakeVolume(volume_name("wsX"))
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    await sandbox._ensure_volume(fake_docker, _spec(workspace_key="wsX", durability="persistent"))
    # No second volume created; the existing one is adopted.
    assert list(fake_docker.store_volumes) == [volume_name("wsX")]


async def test_ensure_volume_reraises_non_404(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "volume backend down")

    fake_docker.volumes.get = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_volume(fake_docker, _spec(workspace_key="wsVe", durability="persistent"))


async def test_ensure_image_reraises_non_404(fake_docker: FakeDocker) -> None:
    async def _boom(name):
        raise DockerError(500, "registry error")

    fake_docker.images.inspect = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_image(fake_docker, "img:1", "missing")


async def test_ensure_image_pull_error_is_loud(fake_docker: FakeDocker) -> None:
    async def _boom(*, from_image, **_):
        raise DockerError(500, "registry unreachable")

    fake_docker.images.pull = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    # Image absent and pull_policy allows pulling: a pull engine error is surfaced loudly.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_image(fake_docker, "absent:1", "missing")


async def test_ensure_internal_network_adopts_existing(fake_docker: FakeDocker) -> None:
    fake_docker.store_networks.add(INTERNAL_NETWORK)
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    await sandbox._ensure_internal_network(fake_docker)
    assert fake_docker.network_configs == []  # nothing created


async def test_ensure_internal_network_loses_create_race(fake_docker: FakeDocker) -> None:
    async def _conflict(config):
        raise DockerError(409, "network already exists")

    fake_docker.networks.create = _conflict  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    await sandbox._ensure_internal_network(fake_docker)  # adopts, no raise


async def test_ensure_internal_network_create_error_is_loud(fake_docker: FakeDocker) -> None:
    async def _boom(config):
        raise DockerError(500, "network driver failure")

    fake_docker.networks.create = _boom  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    # A non-409 create failure is a genuine engine error, not a lost race: surfaced loudly.
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox._ensure_internal_network(fake_docker)


# -- connection loudness on the primitives --------------------------------------


async def test_create_session_connection_failure_is_loud(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")

    async def _refuse(config, *, name=None):
        raise OSError("connection reset")

    fake_docker.containers.create = _refuse  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    with pytest.raises(SandboxError, match="SANDBOX_DOCKER_HOST"):
        await sandbox.create_session(_spec(workspace_key="wsC"))


async def test_create_session_engine_error_is_typed(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")

    async def _fail(config, *, name=None):
        raise DockerError(500, "daemon error creating container")

    fake_docker.containers.create = _fail  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox.create_session(_spec(workspace_key="wsErr"))


async def test_orphan_sweep_connection_failure_is_loud(fake_docker: FakeDocker) -> None:
    async def _refuse(**_):
        raise OSError("connection reset")

    fake_docker.containers.list = _refuse  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    with pytest.raises(SandboxError, match="SANDBOX_DOCKER_HOST"):
        await sandbox.recover_orphans()


# -- teardown error paths --------------------------------------------------------


async def test_reap_does_not_swallow_missing_container(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
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
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
    sandbox.bind_policy(permissive_policy())
    session = await sandbox.create_session(_spec(workspace_key="wsDe"))
    fake_docker.store_containers[0].delete_error = 500
    with pytest.raises(SandboxError, match="docker engine error"):
        await sandbox.destroy_session(session.id)


async def test_destroy_reraises_non_404_volume_get_error(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
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
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
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
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings())
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
