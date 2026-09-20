"""The ``SANDBOX_DOCKER_`` settings group."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tai42_kit.sandbox import SandboxDispatchSettings

from tai42_sandbox_docker.settings import DockerSandboxSettings, docker_sandbox_settings


def test_env_prefix_and_required_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_DOCKER_HOST", "tcp://engine:2376")
    settings = DockerSandboxSettings()  # pyright: ignore[reportCallIssue]  # host from env
    assert settings.host == "tcp://engine:2376"


def test_host_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_DOCKER_HOST", raising=False)
    with pytest.raises(ValidationError):
        DockerSandboxSettings()  # pyright: ignore[reportCallIssue]  # required host is unset


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_DOCKER_HOST", "unix:///var/run/docker.sock")
    settings = DockerSandboxSettings()  # pyright: ignore[reportCallIssue]  # host from env
    assert settings.tls_verify is True
    assert settings.tls_cert_path == "/certs/client/cert.pem"
    assert settings.tls_key_path == "/certs/client/key.pem"
    assert settings.tls_ca_path == "/certs/client/ca.pem"
    assert settings.default_cpu is None
    assert settings.default_memory_mb is None
    assert settings.pull_policy == "missing"
    # Inherited lifecycle knobs from the kit dispatch mixin.
    assert settings.default_ttl_seconds == 3600
    assert settings.reap_interval_seconds == 300
    assert settings.exec_default_timeout_seconds == 300


def test_mixes_in_dispatch_settings() -> None:
    assert issubclass(DockerSandboxSettings, SandboxDispatchSettings)


def test_settings_accessor_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_DOCKER_HOST", "tcp://engine:2376")
    from tai42_kit.settings import reset_all_settings

    reset_all_settings()
    first = docker_sandbox_settings()
    second = docker_sandbox_settings()
    assert first is second
    reset_all_settings()
