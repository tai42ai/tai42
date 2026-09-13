"""The egress-firewall readiness gate on the session-create path.

The Docker provider proves, before it returns a session, that the engine's
inner-bridge egress firewall is in force — a real egress probe from a throwaway
container on the egress tier. These drive the gate against the in-memory engine
fake: the probe container's ``cat /etc/resolv.conf`` + two ``nc`` dials are scripted
with canned exit codes (see ``conftest`` ``exec_by_cmd``), and the deny/allow targets
are DERIVED (the configured engine control address, and the probe's own resolver), so
no target knob exists to mis-set. The live-engine half is the docker-gated e2e leg.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.sandbox import SandboxError, SandboxSessionSpec
from tai42_kit.sandbox import LABEL_DURABILITY, LABEL_SANDBOX, permissive_policy

from tai42_sandbox_docker import readiness
from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.readiness import (
    engine_control_address,
    resolve_ipv4,
    resolver_from_resolv_conf,
)
from tai42_sandbox_docker.sessions import DockerSandboxSession
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeDocker

SESSION_IMAGE = "img:1"
PROBE_IMAGE = "probe:nc"
ENGINE_HOST = "tcp://10.1.2.3:2376"
RESOLVER_ADDR = "10.0.0.53"


def _sandbox(fake_docker: FakeDocker, **overrides: Any) -> DockerSandbox:
    base: dict[str, Any] = {
        "host": ENGINE_HOST,
        "readiness_probe_enabled": True,
        "readiness_probe_image": PROBE_IMAGE,
    }
    base.update(overrides)
    sandbox = DockerSandbox(docker=fake_docker, settings=DockerSandboxSettings(**base))
    sandbox.bind_policy(permissive_policy())
    return sandbox


def _spec(**overrides: Any) -> SandboxSessionSpec:
    base: dict[str, Any] = {
        "image": SESSION_IMAGE,
        "workspace_key": "ws1",
        "durability": "ephemeral",
        "network": "egress",
        "ttl_seconds": 300,
    }
    base.update(overrides)
    return SandboxSessionSpec(**base)


def _seed(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image(SESSION_IMAGE)
    fake_docker.seed_image(PROBE_IMAGE)


def _probe_responder(
    *,
    resolv_conf: str = f"nameserver {RESOLVER_ADDR}\n",
    deny_exit: int = 1,
    allow_exit: int = 0,
):
    """Script the probe container's execs: ``cat /etc/resolv.conf`` yields
    ``resolv_conf``; a ``nc`` dial to :2376 (the derived deny target) returns
    ``deny_exit`` and any other dial (the resolver on :53) returns ``allow_exit``."""

    def responder(cmd: list[str]) -> tuple[list[tuple[int, bytes]], int]:
        if cmd[0] == "cat":
            return ([(1, resolv_conf.encode())], 0)
        return ([], deny_exit if cmd[-1] == "2376" else allow_exit)

    return responder


def _probes(fake_docker: FakeDocker) -> list[Any]:
    return [c for c in fake_docker.store_containers if c.config.get("Image") == PROBE_IMAGE]


def _sessions(fake_docker: FakeDocker) -> list[Any]:
    return [c for c in fake_docker.store_containers if c.config.get("Image") == SESSION_IMAGE]


# -- the gate on the create path -------------------------------------------------


async def test_gate_refuses_when_deny_target_connects(fake_docker: FakeDocker) -> None:
    """The deny target (the engine control address) connecting from a probe container
    means the firewall is NOT dropping it — refuse loudly, create no session, and
    force-delete the probe container."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=0)
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match="not in force"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False
    assert not _sessions(fake_docker)
    probes = _probes(fake_docker)
    assert len(probes) == 1
    assert probes[0].delete_calls == [{"force": True, "v": True}]


async def test_gate_refuses_when_allow_target_unreachable(fake_docker: FakeDocker) -> None:
    """The allow target (the session resolver) being unreachable means the firewall is
    over-blocking / half-installed — refuse loudly."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=1)
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match="over-blocking"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False
    assert not _sessions(fake_docker)


async def test_gate_passes_and_creates_when_deny_drops_and_allow_connects(fake_docker: FakeDocker) -> None:
    """Deny dropped + allow reachable ⇒ the real session is created and returned, the
    engine is marked verified, and the probe ran exactly once."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    sandbox = _sandbox(fake_docker)

    session = await sandbox.create_session(_spec())

    assert session is not None
    assert sandbox._engine_verified is True
    assert len(_sessions(fake_docker)) == 1
    probes = _probes(fake_docker)
    assert len(probes) == 1
    assert probes[0].deleted


async def test_probe_runs_once_for_concurrent_creates(fake_docker: FakeDocker) -> None:
    """A burst of concurrent creates arriving with the flag False funnels through the
    lock: exactly ONE probe container is created; the rest observe its result."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    sandbox = _sandbox(fake_docker)

    await asyncio.gather(*(sandbox.create_session(_spec(workspace_key=f"c{i}")) for i in range(5)))

    assert len(_probes(fake_docker)) == 1
    assert len(_sessions(fake_docker)) == 5
    assert sandbox._engine_verified is True


async def test_first_create_after_disconnect_reprobes(fake_docker: FakeDocker) -> None:
    """A session exec that observes the daemon gone (a connection-level engine error)
    resets the verified flag, so the next create re-proves readiness with a fresh
    probe."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    sandbox = _sandbox(fake_docker)

    session = await sandbox.create_session(_spec(workspace_key="s1"))
    assert isinstance(session, DockerSandboxSession)
    assert sandbox._engine_verified is True
    assert len(_probes(fake_docker)) == 1

    # The daemon goes away: an exec surfaces aiodocker's connection-failure status.
    session._container.exec_error = readiness.ENGINE_UNREACHABLE_STATUS
    with pytest.raises(SandboxError):
        await session.exec(["true"], timeout_seconds=5)
    assert sandbox._engine_verified is False

    await sandbox.create_session(_spec(workspace_key="s2"))
    assert len(_probes(fake_docker)) == 2


async def test_gate_refuses_when_engine_host_is_a_local_socket(fake_docker: FakeDocker) -> None:
    """Enabled against a ``unix://`` engine, the deny target cannot be derived (a socket
    has no network address) — refuse loudly and never reach a probe container."""
    _seed(fake_docker)
    sandbox = _sandbox(fake_docker, host="unix:///var/run/docker.sock")

    with pytest.raises(SandboxError, match="local socket"):
        await sandbox.create_session(_spec())

    assert not _probes(fake_docker)
    assert sandbox._engine_verified is False


async def test_gate_refuses_when_engine_host_unresolvable(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine host that resolves to no address cannot yield a deny target — refuse
    loudly before any probe container is created."""
    _seed(fake_docker)

    def _no_dns(*_: Any, **__: Any) -> Any:
        raise OSError("no such host")

    monkeypatch.setattr(readiness.socket, "getaddrinfo", _no_dns)
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match="cannot resolve"):
        await sandbox.create_session(_spec())

    assert not _probes(fake_docker)
    assert sandbox._engine_verified is False


async def test_gate_refuses_when_probe_resolv_conf_has_no_nameserver(fake_docker: FakeDocker) -> None:
    """A probe container whose ``/etc/resolv.conf`` names no resolver cannot yield an
    allow target — refuse loudly, and still force-delete the probe container."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(resolv_conf="# managed elsewhere\nsearch example\n")
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match="no nameserver"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False
    probes = _probes(fake_docker)
    assert len(probes) == 1
    assert probes[0].deleted


async def test_probe_image_absent_under_never_refuses(fake_docker: FakeDocker) -> None:
    """The probe image absent under ``pull_policy='never'`` raises the airgapped error
    before any probe container is created — never a session on an unproven engine."""
    fake_docker.seed_image(SESSION_IMAGE)  # session image present; probe image absent
    sandbox = _sandbox(fake_docker, pull_policy="never")

    with pytest.raises(SandboxError, match="airgapped"):
        await sandbox.create_session(_spec())

    assert not _probes(fake_docker)
    assert sandbox._engine_verified is False


async def test_disabled_probe_creates_session_without_probing(fake_docker: FakeDocker) -> None:
    """With the probe disabled the create runs unchanged: a session is created with
    ZERO probe containers and the verified flag is never touched (the fake-provider and
    non-firewalled-engine invariant, at the Docker layer)."""
    _seed(fake_docker)
    sandbox = _sandbox(fake_docker, readiness_probe_enabled=False)

    session = await sandbox.create_session(_spec())

    assert session is not None
    assert not _probes(fake_docker)
    assert len(_sessions(fake_docker)) == 1
    assert sandbox._engine_verified is False


async def test_probe_container_is_hardened_egress_tier_and_labelled(fake_docker: FakeDocker) -> None:
    """The probe container's create payload is unprivileged, egress-tier, sandbox-
    labelled, anonymous, mounts no workspace, and it is force-deleted."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    sandbox = _sandbox(fake_docker)

    await sandbox.create_session(_spec())

    probe = _probes(fake_docker)[0]
    config = probe.config
    host_config = config["HostConfig"]
    assert host_config["NetworkMode"] == "bridge"
    assert host_config["SecurityOpt"] == ["no-new-privileges"]
    assert host_config["CapDrop"] == ["ALL"]
    assert host_config["Privileged"] is False
    assert "Mounts" not in host_config
    assert config["Labels"][LABEL_SANDBOX] == "1"
    assert config["Labels"][LABEL_DURABILITY] == "ephemeral"
    assert probe.name is None
    assert probe.delete_calls == [{"force": True, "v": True}]


async def test_gate_refuses_when_probe_times_out(fake_docker: FakeDocker) -> None:
    """The whole probe is bounded: a probe container that never answers trips the
    timeout, refusing the create loudly and force-deleting the probe container."""
    _seed(fake_docker)
    fake_docker.default_block_reads = True  # the probe's first exec never returns
    sandbox = _sandbox(fake_docker, readiness_probe_timeout_seconds=0.05)

    with pytest.raises(SandboxError, match="did not complete"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False
    probes = _probes(fake_docker)
    assert len(probes) == 1
    assert probes[0].deleted


async def test_gate_refuses_when_probe_container_create_fails(fake_docker: FakeDocker) -> None:
    """An engine error creating the probe container is surfaced loudly (the probe
    container is the first create on the gated path); no session is returned."""
    _seed(fake_docker)
    fake_docker.create_error = 500
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match=r"\[500\]"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False


async def test_gate_refuses_when_probe_container_create_disconnects(fake_docker: FakeDocker) -> None:
    """A connection-level failure creating the probe container is surfaced loudly as an
    engine-unreachable error; no session is returned."""
    _seed(fake_docker)
    fake_docker.create_conn_error = True
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match="cannot reach the Docker engine"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False


async def test_probe_teardown_engine_error_is_surfaced(fake_docker: FakeDocker) -> None:
    """A non-404 engine error force-deleting the probe container is surfaced loudly
    rather than swallowed — the engine is not left silently unproven."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    fake_docker.default_delete_error = 500
    sandbox = _sandbox(fake_docker)

    with pytest.raises(SandboxError, match=r"\[500\]"):
        await sandbox.create_session(_spec())

    assert sandbox._engine_verified is False


async def test_probe_container_already_gone_at_teardown_is_swallowed(fake_docker: FakeDocker) -> None:
    """A 404 force-deleting the probe container (already reclaimed) is swallowed — the
    probe still passes and the session is created."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    fake_docker.default_delete_error = 404
    sandbox = _sandbox(fake_docker)

    session = await sandbox.create_session(_spec())

    assert session is not None
    assert sandbox._engine_verified is True


async def test_destroy_disconnect_surfaces_and_resets_readiness(fake_docker: FakeDocker) -> None:
    """A connection-level failure tearing a session down is surfaced loudly AND resets
    the verified flag, so the next create re-proves the egress firewall."""
    _seed(fake_docker)
    fake_docker.default_exec_by_cmd = _probe_responder(deny_exit=1, allow_exit=0)
    sandbox = _sandbox(fake_docker)
    session = await sandbox.create_session(_spec())
    assert isinstance(session, DockerSandboxSession)
    assert sandbox._engine_verified is True

    session._container.delete_conn_error = True
    with pytest.raises(SandboxError, match="cannot reach the Docker engine"):
        await sandbox.destroy_session(session.id)

    assert sandbox._engine_verified is False


# -- the derivation/predicate helpers --------------------------------------------


def test_engine_control_address_parses_host_and_port() -> None:
    assert engine_control_address("tcp://10.1.2.3:2376") == ("10.1.2.3", 2376)


def test_engine_control_address_defaults_control_port() -> None:
    assert engine_control_address("tcp://engine") == ("engine", 2376)


def test_engine_control_address_rejects_local_socket() -> None:
    with pytest.raises(SandboxError, match="local socket"):
        engine_control_address("unix:///var/run/docker.sock")


def test_engine_control_address_rejects_no_host() -> None:
    with pytest.raises(SandboxError, match="names no host"):
        engine_control_address("tcp://:2376")


def test_resolver_from_resolv_conf_takes_first_nameserver() -> None:
    resolv = "search corp\nnameserver 10.0.0.2\nnameserver 10.0.0.3\n"
    assert resolver_from_resolv_conf(resolv) == "10.0.0.2"


def test_resolver_from_resolv_conf_without_nameserver_raises() -> None:
    with pytest.raises(SandboxError, match="no nameserver"):
        resolver_from_resolv_conf("search corp\noptions ndots:2\n")


def test_resolve_ipv4_returns_literal_address() -> None:
    assert resolve_ipv4("127.0.0.1") == "127.0.0.1"


def test_resolve_ipv4_resolution_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_dns(*_: Any, **__: Any) -> Any:
        raise OSError("no such host")

    monkeypatch.setattr(readiness.socket, "getaddrinfo", _no_dns)
    with pytest.raises(SandboxError, match="cannot resolve"):
        resolve_ipv4("engine")


def test_resolve_ipv4_no_address_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness.socket, "getaddrinfo", lambda *_a, **_k: [])
    with pytest.raises(SandboxError, match="resolved to no address"):
        resolve_ipv4("engine")
