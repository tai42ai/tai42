"""The neutral spec → engine ContainerCreate payload mapping (no engine)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import SandboxSessionSpec, SandboxSpecRejectedError

from tai42_sandbox_docker.provider import (
    INTERNAL_NETWORK,
    build_container_config,
    volume_name,
)


def _spec(**overrides) -> SandboxSessionSpec:
    base = {
        "image": "registry.example/app@sha256:" + "0" * 64,
        "workspace_key": "ws1",
        "durability": "ephemeral",
        "network": "egress",
        "isolation": "container",
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return SandboxSessionSpec(**base)


@pytest.mark.parametrize(
    ("network", "expected_mode"),
    [("none", "none"), ("egress", "bridge"), ("internal", INTERNAL_NETWORK)],
)
def test_network_tier_maps_to_mode(network: str, expected_mode: str) -> None:
    config = build_container_config(_spec(network=network))
    assert config["HostConfig"]["NetworkMode"] == expected_mode


def test_cpu_and_memory_caps() -> None:
    config = build_container_config(_spec(cpu=2.0, memory_mb=512))
    assert config["HostConfig"]["NanoCpus"] == 2_000_000_000
    assert config["HostConfig"]["Memory"] == 512 * 1024 * 1024


def test_caps_fall_back_to_defaults_only_when_unset() -> None:
    config = build_container_config(_spec(), default_cpu=1.0, default_memory_mb=256)
    assert config["HostConfig"]["NanoCpus"] == 1_000_000_000
    assert config["HostConfig"]["Memory"] == 256 * 1024 * 1024

    # A spec cap always wins over the default fallback.
    config = build_container_config(_spec(cpu=4.0), default_cpu=1.0)
    assert config["HostConfig"]["NanoCpus"] == 4_000_000_000


def test_no_caps_when_unset_and_no_default() -> None:
    config = build_container_config(_spec())
    assert "NanoCpus" not in config["HostConfig"]
    assert "Memory" not in config["HostConfig"]


def test_ephemeral_uses_anonymous_volume() -> None:
    config = build_container_config(_spec(durability="ephemeral", workspace_key="ws9"))
    mounts = config["HostConfig"]["Mounts"]
    assert len(mounts) == 1
    assert mounts[0] == {"Target": "/workspace", "Source": "", "Type": "volume", "ReadOnly": False}


def test_persistent_uses_named_volume() -> None:
    config = build_container_config(_spec(durability="persistent", workspace_key="ws9"))
    assert config["HostConfig"]["Mounts"][0]["Source"] == volume_name("ws9") == "tai-sbx-ws9"


def test_labels_round_trip_verbatim() -> None:
    labels = {"team": "conf", "tai42.sandbox": "1", "tai42.sandbox.workspace": "ws1"}
    config = build_container_config(_spec(labels={"team": "conf"}))
    # build_container_config copies exactly the spec labels it is handed.
    assert config["Labels"] == {"team": "conf"}
    config = build_container_config(_spec(labels=labels))
    assert config["Labels"] == labels


def test_hardening_and_single_mount_invariant() -> None:
    config = build_container_config(_spec())
    host = config["HostConfig"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["CapDrop"] == ["ALL"]
    assert host["Privileged"] is False
    assert host["ReadonlyRootfs"] is False
    # No host bind mount of any kind, and exactly one workspace mount.
    assert "Binds" not in host
    assert len(host["Mounts"]) == 1
    assert host["Mounts"][0]["Type"] == "volume"
    assert config["WorkingDir"] == "/workspace"
    assert config["Cmd"] == ["sleep", "infinity"]


def test_env_secrets_unwrapped_only_in_payload() -> None:
    config = build_container_config(_spec(env={"TOKEN": SecretStr("s3cr3t")}))
    assert "TOKEN=s3cr3t" in config["Env"]


def test_vm_isolation_rejected() -> None:
    with pytest.raises(SandboxSpecRejectedError, match="vm"):
        build_container_config(_spec(isolation="vm"))


def test_non_positive_cpu_rejected() -> None:
    with pytest.raises(SandboxSpecRejectedError, match="cpu cap"):
        build_container_config(_spec(cpu=0))


def test_non_positive_memory_rejected() -> None:
    with pytest.raises(SandboxSpecRejectedError, match="memory_mb cap"):
        build_container_config(_spec(memory_mb=0))
