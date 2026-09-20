"""Session lifecycle against the in-memory engine fake: create/adopt, image and
network provisioning, teardown/reap volume behaviour, and orphan recovery."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.sandbox import (
    SandboxError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)
from tai42_kit.sandbox import (
    LABEL_DURABILITY,
    LABEL_SANDBOX,
    LABEL_WORKSPACE,
    permissive_policy,
)

from tai42_sandbox_docker.provider import (
    INTERNAL_NETWORK,
    DockerSandbox,
    container_name,
    volume_name,
)
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeContainer, FakeDocker, FakeVolume


def _settings(**overrides: Any) -> DockerSandboxSettings:
    base: dict[str, Any] = {"host": "tcp://engine:2376"}
    base.update(overrides)
    return DockerSandboxSettings(**base)


def _sandbox(fake_docker: FakeDocker, **settings_overrides) -> DockerSandbox:
    sandbox = DockerSandbox(docker=fake_docker, settings=_settings(**settings_overrides))
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


async def test_create_ephemeral_session(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec(workspace_key="wsE"))

    assert session.workspace_path == "/workspace"
    assert len(fake_docker.store_containers) == 1
    container = fake_docker.store_containers[0]
    assert container.name is None  # no tai-sbx-* name for an ephemeral session
    assert container.running


async def test_missing_image_is_pulled_under_missing_policy(fake_docker: FakeDocker) -> None:
    sandbox = _sandbox(fake_docker, pull_policy="missing")
    await sandbox.create_session(_spec(workspace_key="wsP"))
    assert fake_docker.pulled == ["img:1"]


async def test_missing_image_under_never_policy_raises(fake_docker: FakeDocker) -> None:
    sandbox = _sandbox(fake_docker, pull_policy="never")
    with pytest.raises(SandboxError, match="airgapped"):
        await sandbox.create_session(_spec(workspace_key="wsN"))
    assert fake_docker.pulled == []


async def test_internal_network_is_provisioned(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    await sandbox.create_session(_spec(workspace_key="wsI", network="internal"))
    assert INTERNAL_NETWORK in fake_docker.store_networks
    created = fake_docker.network_configs[0]
    assert created["Internal"] is True


async def test_persistent_session_creates_named_container_and_volume(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    await sandbox.create_session(_spec(workspace_key="wsK", durability="persistent"))

    assert container_name("wsK") in fake_docker.containers_by_name
    assert volume_name("wsK") in fake_docker.store_volumes


async def test_persistent_adopts_existing_container(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    name = container_name("wsA")
    existing = await fake_docker.containers.create({"Labels": {}}, name=name)
    sandbox = _sandbox(fake_docker)

    session = await sandbox.create_session(_spec(workspace_key="wsA", durability="persistent"))

    # Adopted, not double-created, and ensured running.
    assert len(fake_docker.store_containers) == 1
    assert session.id == existing.id
    assert existing.running


async def test_persistent_volume_rejected_when_engine_cannot_create(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    fake_docker.volume_create_fails = True
    sandbox = _sandbox(fake_docker)
    with pytest.raises(SandboxSpecRejectedError, match="persistent"):
        await sandbox.create_session(_spec(workspace_key="wsV", durability="persistent"))


async def test_reap_keeps_persistent_volume(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec(workspace_key="wsR", durability="persistent"))

    record = sandbox._ledger[session.id]
    record.expires_at = record.created_at
    reaped = await sandbox.reap()

    assert session.id in reaped
    container = fake_docker.store_containers[0]
    assert container.deleted
    # The durable volume survives the reap.
    assert not fake_docker.store_volumes[volume_name("wsR")].deleted


async def test_destroy_removes_persistent_volume_unforced(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec(workspace_key="wsD", durability="persistent"))

    await sandbox.destroy_session(session.id)

    volume = fake_docker.store_volumes[volume_name("wsD")]
    assert volume.deleted
    assert volume.delete_forced is False  # never forced


async def test_destroy_surfaces_volume_in_use(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec(workspace_key="wsU", durability="persistent"))
    fake_docker.store_volumes[volume_name("wsU")].in_use = True

    with pytest.raises(SandboxError, match="in use"):
        await sandbox.destroy_session(session.id)
    # The referencing worker's volume is left in place, never force-removed.
    assert not fake_docker.store_volumes[volume_name("wsU")].deleted


async def test_destroy_is_idempotent_on_missing_container(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec(workspace_key="wsG"))
    fake_docker.store_containers[0].delete_error = 404  # externally removed

    # A NotFound on the container is swallowed to a no-op on the explicit teardown.
    await sandbox.destroy_session(session.id)


async def test_recover_orphans_destroys_ephemeral_keeps_persistent(fake_docker: FakeDocker) -> None:
    ephemeral = FakeContainer(
        container_id="orphan-eph",
        name="tai-sbx-old-eph",
        config={"Labels": {LABEL_SANDBOX: "1", LABEL_DURABILITY: "ephemeral", LABEL_WORKSPACE: "old-eph"}},
    )
    persistent = FakeContainer(
        container_id="orphan-per",
        name="tai-sbx-old-per",
        config={"Labels": {LABEL_SANDBOX: "1", LABEL_DURABILITY: "persistent", LABEL_WORKSPACE: "old-per"}},
    )
    fake_docker.store_containers.extend([ephemeral, persistent])
    fake_docker.store_volumes[volume_name("old-per")] = FakeVolume(volume_name("old-per"))

    sandbox = _sandbox(fake_docker)
    handled = await sandbox.recover_orphans()

    assert ephemeral.deleted
    assert not persistent.deleted
    assert any("destroyed orphan ephemeral container" in line for line in handled)
    assert any("retained orphan persistent container" in line for line in handled)
    assert any("retained orphan persistent workspace volume" in line for line in handled)
