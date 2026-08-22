"""Import-time registration: the provider class is registered on ``tai42_app``."""

from __future__ import annotations

from tai42_sandbox_docker.provider import DockerSandbox


def test_provider_class_registered(stub_app) -> None:
    assert stub_app.sandboxes.registered_cls is DockerSandbox


def test_registration_keeps_concrete_class() -> None:
    # The registration is a plain call, not a decorator, so the class type is intact.
    assert isinstance(DockerSandbox, type)
    assert DockerSandbox.__name__ == "DockerSandbox"
