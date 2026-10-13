"""Neutral fixtures for the MCP door: an identity provider that declares an authorization server.

``DeclaringTokenProvider`` declares one authorization server and accepts one static token —
deliberately not a token validator of any standard kind — and records the request resource
it saw on every validation, so a test proves both seams the door adds to the identity
contract: the declaration and the request resource.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from tai42_contract.access_control.context import get_request_resource
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider
from tai42_kit.access_control import registry
from tai42_kit.settings import reset_all_settings

ISSUER = "https://issuer.example/realm"
STATIC_TOKEN = "door-static-token"
STATIC_USER = "door-user"
PUBLIC_BASE_URL = "https://public.example"
PROVIDER_NAME = "door-fixture"


class DeclaringTokenProvider(IdentityProvider):
    """Declares ``ISSUER`` and accepts ``STATIC_TOKEN`` as ``STATIC_USER``; records each request resource it reads."""

    def __init__(self, *, accepted_resource: str | None = None) -> None:
        self.seen_resources: list[str | None] = []
        self._accepted_resource = accepted_resource

    async def validate_token(self, token: str) -> AuthIdentity | None:
        resource = get_request_resource()
        self.seen_resources.append(resource)
        if token != STATIC_TOKEN:
            return None
        if self._accepted_resource is not None and resource != self._accepted_resource:
            return None
        return AuthIdentity(user_id=STATIC_USER, claims={})

    def authorization_servers(self) -> tuple[str, ...]:
        return (ISSUER,)


def register_declaring_provider(provider: DeclaringTokenProvider) -> None:
    """Register ``provider`` (one shared instance) in the committed identity registry."""
    registry._PROVIDERS._generation.committed()[PROVIDER_NAME] = lambda _settings: provider


@contextmanager
def public_base_url(monkeypatch: pytest.MonkeyPatch, value: str | None) -> Iterator[None]:
    """Set (or unset) ``ACCESS_CONTROL_PUBLIC_BASE_URL`` for the block."""
    if value is None:
        monkeypatch.delenv("ACCESS_CONTROL_PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", value)
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


def fresh_serving_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the process app a serving core built now, so its access-control stack follows the current env.

    The adapter builds its middleware list and memoizes its provider chain once per serving
    core; a core built by an earlier test would keep that test's stack and providers.
    """
    from tai42_skeleton.app.instance import app

    monkeypatch.setattr(app, "_building", app._build_serving_core())


def install_declaring_provider(monkeypatch: pytest.MonkeyPatch, provider: DeclaringTokenProvider) -> None:
    """Register ``provider`` for a full boot: re-registered after every identity-registry reset the boot runs."""
    import tai42_skeleton.app.lifecycle as lifecycle

    reset = lifecycle.reset_identity_registry

    def _reset_then_register() -> None:
        reset()
        if PROVIDER_NAME not in registry.iter_identity_provider_names_staged():
            registry.register_identity_provider(PROVIDER_NAME, lambda _settings: provider)

    monkeypatch.setattr(lifecycle, "reset_identity_registry", _reset_then_register)
    register_declaring_provider(provider)
