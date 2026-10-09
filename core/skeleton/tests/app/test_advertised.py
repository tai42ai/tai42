"""The advertised authorization servers, the declaration check at build, and the public-URL setting.

The door is on only when an active identity provider declares an authorization server AND
``ACCESS_CONTROL_PUBLIC_BASE_URL`` is set; ``advertised_authorization_servers`` is the one
definition of that switch. A bad declaration fails the build naming the provider; a
declaring provider with no public URL logs one warning. The setting is validated and
stored normalised at load.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider
from tai42_contract.app import tai42_app
from tai42_kit.access_control import registry
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.access_control.advertised import (
    advertised_authorization_servers,
    mcp_metadata_url,
    mcp_resource_url,
)
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.startup import probe_identity_provider
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate

PUBLIC_URL = "https://mcp.example"


class _DeclaringProvider(IdentityProvider):
    """A neutral provider that declares a fixed list of authorization servers."""

    def __init__(self, servers: tuple[str, ...] = ()) -> None:
        self._servers = servers

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def authorization_servers(self) -> tuple[str, ...]:
        return self._servers


class _DefaultProvider(IdentityProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None


@pytest.fixture(autouse=True)
def _bound_app() -> Iterator[None]:
    with tai42_app.bound(app):
        yield
    reset_all_settings()


def _chain(monkeypatch: pytest.MonkeyPatch, providers: dict[str, IdentityProvider], *, public_url: str | None) -> None:
    for name, provider in providers.items():
        registry._PROVIDERS._generation.committed()[name] = lambda _settings, provider=provider: provider
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", json.dumps(list(providers)))
    if public_url is None:
        monkeypatch.delenv("ACCESS_CONTROL_PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", public_url)
    reset_all_settings()


async def test_the_set_is_ordered_and_de_duplicated_over_two_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _DeclaringProvider(("https://b.example", "https://a.example"))
    second = _DeclaringProvider(("https://a.example", "https://c.example"))
    _chain(monkeypatch, {"first": first, "second": second}, public_url=PUBLIC_URL)
    await probe_identity_provider()
    assert advertised_authorization_servers() == ("https://b.example", "https://a.example", "https://c.example")


async def test_a_provider_with_the_default_contributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _chain(
        monkeypatch,
        {"plain": _DefaultProvider(), "declaring": _DeclaringProvider(("https://a.example",))},
        public_url=PUBLIC_URL,
    )
    await probe_identity_provider()
    assert advertised_authorization_servers() == ("https://a.example",)


async def test_no_declaring_provider_advertises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _chain(monkeypatch, {"plain": _DefaultProvider()}, public_url=PUBLIC_URL)
    await probe_identity_provider()
    assert advertised_authorization_servers() == ()


async def test_access_control_off_advertises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _chain(monkeypatch, {"declaring": _DeclaringProvider(("https://a.example",))}, public_url=PUBLIC_URL)
    await probe_identity_provider()
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    reset_all_settings()
    assert advertised_authorization_servers() == ()


@pytest.mark.parametrize(
    "value",
    [
        "http://issuer.example",
        "https://issuer.example?x=1",
        "https://issuer.example#f",
        "https://user@issuer.example",
        "issuer.example",
        "/relative",
        "",
    ],
)
async def test_an_invalid_declared_value_fails_the_build_naming_the_provider(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _chain(monkeypatch, {"bad-issuer": _DeclaringProvider((value,))}, public_url=PUBLIC_URL)
    with pytest.raises(RuntimeError, match="'bad-issuer'") as raised:
        await probe_identity_provider()
    assert repr(value) in str(raised.value)


async def test_a_loopback_http_issuer_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _chain(monkeypatch, {"local": _DeclaringProvider(("http://127.0.0.1:8080/realm",))}, public_url=PUBLIC_URL)
    await probe_identity_provider()
    assert advertised_authorization_servers() == ("http://127.0.0.1:8080/realm",)


async def test_a_declared_server_without_a_public_url_boots_warns_once_and_advertises_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _chain(monkeypatch, {"declaring": _DeclaringProvider(("https://a.example",))}, public_url=None)
    with caplog.at_level("WARNING", logger="tai42_skeleton.access_control.advertised"):
        await probe_identity_provider()
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "'declaring'" in warnings[0].getMessage()
    assert "ACCESS_CONTROL_PUBLIC_BASE_URL" in warnings[0].getMessage()
    assert advertised_authorization_servers() == ()


async def test_a_provider_missing_outside_a_reload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _chain(monkeypatch, {"declaring": _DeclaringProvider(("https://a.example",))}, public_url=PUBLIC_URL)
    await probe_identity_provider()
    app._serving_core.active_auth_providers.pop("declaring")
    with pytest.raises(RuntimeError, match="'declaring'"):
        advertised_authorization_servers()


async def test_a_provider_missing_during_a_reload_contributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _chain(monkeypatch, {"declaring": _DeclaringProvider(("https://a.example",))}, public_url=PUBLIC_URL)
    await probe_identity_provider()
    app._serving_core.active_auth_providers.pop("declaring")
    monkeypatch.setattr(type(reload_gate), "locked", property(lambda _self: True))
    assert advertised_authorization_servers() == ()


def test_the_door_urls_are_composed_from_the_public_url_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", "https://edge.example/tenant")
    reset_all_settings()
    assert mcp_resource_url("/mcp") == "https://edge.example/tenant/mcp"
    assert mcp_metadata_url("/app/svc/sse") == (
        "https://edge.example/tenant/.well-known/oauth-protected-resource/app/svc/sse"
    )


# -- the setting --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("HTTPS://Host.Example:443/x/", "https://host.example/x"),
        ("https://host.example/", "https://host.example"),
        ("https://host.example", "https://host.example"),
        ("https://host.example:8443", "https://host.example:8443"),
        ("https://host.example/Prefix/Path", "https://host.example/Prefix/Path"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://127.0.0.1:80/p", "http://127.0.0.1/p"),
        ("http://[::1]:9000", "http://[::1]:9000"),
    ],
)
def test_the_public_url_is_stored_normalised(value: str, stored: str) -> None:
    assert AccessControlSettings(public_base_url=value).public_base_url == stored


@pytest.mark.parametrize(
    "value",
    [
        "http://host.example",
        "ftp://host.example",
        "https://host.example?x=1",
        "https://host.example/?",
        "https://host.example#f",
        "https://user:pw@host.example",
        "https://hôst.example",
        "https://host.example/x/../y",
        "https://host.example/x/./y",
        "https://host.example/x//y",
        "https://host.example/a b",
        "https://host.example/a%20b",
        "https://host.example:99999",
        "host.example",
    ],
)
def test_a_public_url_a_client_would_rewrite_is_refused_naming_the_setting(value: str) -> None:
    with pytest.raises(ValidationError, match="ACCESS_CONTROL_PUBLIC_BASE_URL"):
        AccessControlSettings(public_base_url=value)


def test_the_public_url_is_unset_by_default() -> None:
    assert AccessControlSettings().public_base_url is None
