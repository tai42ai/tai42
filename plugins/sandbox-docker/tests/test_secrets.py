"""A ``spec.env`` secret is unwrapped only in the engine payload — never in a repr,
log, or error message."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import SandboxError, SandboxExecTimeoutError, SandboxSessionSpec
from tai42_kit.sandbox import permissive_policy

from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.settings import DockerSandboxSettings

from .conftest import FakeDocker

_SECRET = "super-secret-token-value"


def test_spec_env_is_masked_in_repr() -> None:
    spec = SandboxSessionSpec(
        image="img:1",
        workspace_key="ws1",
        durability="ephemeral",
        ttl_seconds=300,
        env={"TOKEN": SecretStr(_SECRET)},
    )
    assert _SECRET not in repr(spec)


def test_timeout_error_carries_lengths_not_content() -> None:
    err = SandboxExecTimeoutError(timeout_seconds=1.0, stdout_len=1234, stderr_len=5)
    message = str(err)
    assert "1234 bytes" in message
    assert "5 bytes" in message
    assert _SECRET not in message


async def test_connection_error_never_leaks_env(fake_docker: FakeDocker) -> None:
    fake_docker.seed_image("img:1")

    async def _refuse(config, *, name=None):
        raise OSError("connection reset")

    fake_docker.containers.create = _refuse  # type: ignore[method-assign]
    sandbox = DockerSandbox(docker=fake_docker, settings=DockerSandboxSettings(host="tcp://engine:2376"))
    sandbox.bind_policy(permissive_policy())

    spec = SandboxSessionSpec(
        image="img:1",
        workspace_key="wsSec",
        durability="ephemeral",
        network="egress",
        ttl_seconds=300,
        env={"TOKEN": SecretStr(_SECRET)},
    )
    with pytest.raises(SandboxError) as exc_info:
        await sandbox.create_session(spec)
    assert _SECRET not in str(exc_info.value)
